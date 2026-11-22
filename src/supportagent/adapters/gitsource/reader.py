"""Repository files for onboarding: GitHub (REST, read-only) or local checkouts.

A source is ``github:<owner>/<name glob>`` or ``file:<directory glob>``, plus
path globs (``**/pom.xml``, ``**/helm/**``). Only matching files are read,
never whole repositories, and within budget: at most ``max_files`` files of
at most ``max_file_bytes`` each. A file over the size cap is skipped and
reported, never truncated silently. Keys are ``<repo>/<path>``, the layout
the onboarding lab and detection read.

Whatever the globs say, two things are never read (ADR-0023):

- **dependencies and build output**: virtualenvs (a directory holding
  ``pyvenv.cfg`` or ``conda-meta``), ``site-packages``, ``node_modules``,
  ``.git`` and tool caches. They are not the system's code, and would spend
  the file budget;
- **files that hold secrets by convention**: ``.env`` files, private keys and
  keystores, and files other than source code named for tokens, passwords,
  secrets or credentials (``git_token.txt``; ``token_utils.py`` is still read).
"""

from __future__ import annotations

import base64
import fnmatch
import os
from collections.abc import Sequence
from pathlib import Path

import httpx

from supportagent.domain.errors import ConfigError, ExternalServiceError
from supportagent.domain.onboarding import IngestedFiles

_API = "https://api.github.com"
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


#: Directories that are never the system's own code.
_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "site-packages",
        "dist-packages",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".gradle",
        ".idea",
    }
)
#: A directory holding one of these is an environment, not code.
_ENV_MARKERS = ("pyvenv.cfg", "conda-meta")
#: File names that hold secrets by convention (case-insensitive globs).
_SECRET_NAMES = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".pgpass",
)
#: Words that name a secret-holding file, unless it is source code (``token_utils.py``).
_SECRET_WORDS = ("*token*", "*password*", "*secret*", "*credential*")
_CODE_SUFFIXES = frozenset(
    {".py", ".java", ".kt", ".scala", ".js", ".ts", ".tsx", ".go", ".rb", ".rs", ".cs", ".sql"}
)


def never_read(path: str) -> bool:
    """A dependency, cache or secret-holding file (see the module docstring)."""
    parts = path.split("/")
    if any(part in _SKIP_DIRS for part in parts[:-1]):
        return True
    name = parts[-1].lower()
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _SECRET_NAMES):
        return True
    code = Path(name).suffix in _CODE_SUFFIXES
    return not code and any(fnmatch.fnmatchcase(name, word) for word in _SECRET_WORDS)


def path_matches(path: str, globs: Sequence[str]) -> bool:
    """``fnmatch`` where a leading ``**/`` also matches the repository root."""
    for glob in globs:
        if fnmatch.fnmatchcase(path, glob):
            return True
        if glob.startswith("**/") and fnmatch.fnmatchcase(path, glob[3:]):
            return True
    return False


def excluded(path: str, exclude: Sequence[str]) -> bool:
    """``path`` (a file, or a directory) is one the manifest's ``exclude`` leaves out."""
    if path_matches(path, exclude):
        return True
    return any(g.endswith("/**") and path_matches(path, [g[:-3]]) for g in exclude)


def read_local(  # noqa: PLR0913 — the source and its limits
    spec: str,
    globs: Sequence[str],
    *,
    max_files: int,
    max_file_bytes: int,
    into: IngestedFiles,
    exclude: Sequence[str] = (),
) -> None:
    """``file:<directory glob>``: each matching directory is one repository."""
    pattern = Path(spec).expanduser()
    roots = sorted(p for p in pattern.parent.glob(pattern.name) if p.is_dir())
    if not roots:
        raise ConfigError(f"no directory matches {spec!r}")  # noqa: TRY003
    for root in roots:
        into.repos.append(root.name)
        for path in _walk(root, into, root.name, exclude):
            rel = path.relative_to(root).as_posix()
            if never_read(rel) or not path_matches(rel, globs) or excluded(rel, exclude):
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                into.skipped[f"{root.name}/{rel}"] = f"unreadable ({exc.strerror})"
                continue
            _add(into, f"{root.name}/{rel}", raw, max_files, max_file_bytes)


def _walk(root: Path, into: IngestedFiles, repo: str, exclude: Sequence[str] = ()) -> list[Path]:
    """Every file under ``root``, not descending into dependencies or environments.

    A directory the reader may not open is skipped and reported, never fatal:
    a checkout mounted read-only often holds a few private directories.
    """
    found: list[Path] = []

    def unreadable(exc: OSError) -> None:
        where = Path(exc.filename).relative_to(root).as_posix() if exc.filename else "?"
        into.skipped[f"{repo}/{where}/"] = f"unreadable ({exc.strerror})"

    for here, dirs, files in os.walk(root, onerror=unreadable):
        base = Path(here)
        dirs[:] = sorted(
            d
            for d in dirs
            if not excluded((base / d).relative_to(root).as_posix(), exclude)
            and _descend(base / d, d, into, root, repo)
        )
        found.extend(base / f for f in sorted(files) if _is_file(base / f))
    return sorted(found)


def _descend(path: Path, name: str, into: IngestedFiles, root: Path, repo: str) -> bool:
    if name in _SKIP_DIRS:
        return False
    try:
        if path.is_symlink():
            return False
        return not any((path / marker).exists() for marker in _ENV_MARKERS)
    except OSError as exc:
        into.skipped[f"{repo}/{path.relative_to(root).as_posix()}/"] = (
            f"unreadable ({exc.strerror})"
        )
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


class GitHubReader:
    """``github:<owner>/<name glob>`` through the REST API, read-only."""

    def __init__(self, *, token: str | None, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=_API, headers=headers, timeout=_TIMEOUT, transport=transport
        )

    def read(  # noqa: PLR0913 — the source and its limits
        self,
        spec: str,
        globs: Sequence[str],
        *,
        max_files: int,
        max_file_bytes: int,
        into: IngestedFiles,
        exclude: Sequence[str] = (),
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
                if (
                    item.get("type") != "blob"
                    or never_read(path)
                    or not path_matches(path, globs)
                    or excluded(path, exclude)
                ):
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
