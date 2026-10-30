"""Repository files for onboarding: GitHub (REST, read-only) or local checkouts.

A source is ``github:<owner>/<name glob>`` or ``file:<directory glob>``, plus
path globs (``**/pom.xml``, ``**/helm/**``). Only matching files are read,
never whole repositories, and within budget: at most ``max_files`` files of
at most ``max_file_bytes`` each. A file over the size cap is skipped and
reported, never truncated silently. Keys are ``<repo>/<path>``, the layout
the onboarding lab and detection read.
"""

from __future__ import annotations

import base64
import fnmatch
from collections.abc import Sequence
from pathlib import Path

import httpx

from supportagent.domain.errors import ConfigError, ExternalServiceError
from supportagent.domain.onboarding import IngestedFiles

_API = "https://api.github.com"
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def path_matches(path: str, globs: Sequence[str]) -> bool:
    """``fnmatch`` where a leading ``**/`` also matches the repository root."""
    for glob in globs:
        if fnmatch.fnmatchcase(path, glob):
            return True
        if glob.startswith("**/") and fnmatch.fnmatchcase(path, glob[3:]):
            return True
    return False


def read_local(
    spec: str, globs: Sequence[str], *, max_files: int, max_file_bytes: int, into: IngestedFiles
) -> None:
    """``file:<directory glob>``: each matching directory is one repository."""
    pattern = Path(spec).expanduser()
    roots = sorted(p for p in pattern.parent.glob(pattern.name) if p.is_dir())
    if not roots:
        raise ConfigError(f"no directory matches {spec!r}")  # noqa: TRY003
    for root in roots:
        into.repos.append(root.name)
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if not path.is_file() or ".git/" in f"{rel}/" or not path_matches(rel, globs):
                continue
            _add(into, f"{root.name}/{rel}", path.read_bytes(), max_files, max_file_bytes)


class GitHubReader:
    """``github:<owner>/<name glob>`` through the REST API, read-only."""

    def __init__(self, *, token: str | None, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=_API, headers=headers, timeout=_TIMEOUT, transport=transport
        )

    def read(
        self,
        spec: str,
        globs: Sequence[str],
        *,
        max_files: int,
        max_file_bytes: int,
        into: IngestedFiles,
    ) -> None:
        owner, _, name_glob = spec.partition("/")
        for repo in self._repos(owner, name_glob):
            into.repos.append(repo["name"])
            branch = repo.get("default_branch") or "main"
            tree = self._get(
                f"/repos/{owner}/{repo['name']}/git/trees/{branch}", {"recursive": "1"}
            )
            entries = tree.get("tree")
            for item in entries if isinstance(entries, list) else []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", ""))
                if item.get("type") != "blob" or not path_matches(path, globs):
                    continue
                key = f"{repo['name']}/{path}"
                if int(item.get("size") or 0) > max_file_bytes:
                    into.skipped[key] = f"larger than {max_file_bytes} bytes"
                    continue
                if len(into.files) >= max_files:
                    into.skipped[key] = "file budget reached"
                    continue
                blob = self._get(f"/repos/{owner}/{repo['name']}/contents/{path}", {"ref": branch})
                raw = base64.b64decode(str(blob.get("content", "")))
                _add(into, key, raw, max_files, max_file_bytes)

    def close(self) -> None:
        self._http.close()

    def _repos(self, owner: str, name_glob: str) -> list[dict[str, str]]:
        repos: list[dict[str, str]] = []
        for kind in ("orgs", "users"):
            page = 1
            try:
                while True:
                    batch = self._get_list(
                        f"/{kind}/{owner}/repos", {"per_page": "100", "page": str(page)}
                    )
                    repos += [
                        r for r in batch if fnmatch.fnmatchcase(str(r.get("name")), name_glob)
                    ]
                    if len(batch) < 100:  # noqa: PLR2004 — the page size asked for
                        break
                    page += 1
            except _NotFoundError:
                continue  # an owner is a user or an org; try the other
            if not repos:
                raise ConfigError(f"no repository of {owner!r} matches {name_glob!r}")  # noqa: TRY003
            return sorted(repos, key=lambda r: str(r.get("name")))
        raise ConfigError(f"GitHub owner {owner!r} not found (or the token can't see it)")  # noqa: TRY003

    def _get(self, path: str, params: dict[str, str]) -> dict[str, object]:
        data = self._request(path, params)
        if not isinstance(data, dict):
            raise ExternalServiceError(f"GitHub {path}: expected an object", retryable=False)  # noqa: TRY003
        return data

    def _get_list(self, path: str, params: dict[str, str]) -> list[dict[str, str]]:
        data = self._request(path, params)
        if not isinstance(data, list):
            raise ExternalServiceError(f"GitHub {path}: expected a list", retryable=False)  # noqa: TRY003
        return [d for d in data if isinstance(d, dict)]

    def _request(self, path: str, params: dict[str, str]) -> object:
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"GitHub unreachable: {exc}", retryable=True) from exc  # noqa: TRY003
        if resp.status_code == httpx.codes.NOT_FOUND:
            raise _NotFoundError(path)
        if resp.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            raise ConfigError(f"GitHub refused the token for {path} (HTTP {resp.status_code})")  # noqa: TRY003
        if resp.status_code != httpx.codes.OK:
            raise ExternalServiceError(f"GitHub {path}: HTTP {resp.status_code}", retryable=True)  # noqa: TRY003
        return resp.json()


class _NotFoundError(Exception):
    pass


def _add(into: IngestedFiles, key: str, raw: bytes, max_files: int, max_file_bytes: int) -> None:
    if len(raw) > max_file_bytes:
        into.skipped[key] = f"larger than {max_file_bytes} bytes"
    elif len(into.files) >= max_files:
        into.skipped[key] = "file budget reached"
    elif b"\0" in raw[:4096]:
        into.skipped[key] = "binary"
    else:
        into.files[key] = raw.decode("utf-8", errors="replace")


__all__ = ["GitHubReader", "path_matches", "read_local"]
