"""An onboarding workspace on disk (ADR-0015 P1, WP-O2): the lab's layout, for a real system.

::

    <workspace>/
      system.yaml                       the manifest ingest ran from
      ingest.json                       per-source state: done, counts, when (resume)
      sources/confluence/<SPACE>/<page_id>.md   front matter + Markdown
      sources/servicenow/{incidents,problems,changes}.yaml, cmdb.yaml
      sources/git/<repo>/<path>
      sources/live/discovery.yaml       filled once access is granted (WP-O5)

Everything written here is already redacted by the caller. Repository keys
are relative paths; one that is absolute or climbs out with ``..`` is refused.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from supportagent.adapters.files._yaml import dump_yaml
from supportagent.domain.errors import ConfigError
from supportagent.domain.onboarding import IngestedPage

_STATE = "ingest.json"


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.sources = root / "sources"

    def prepare(self, manifest_text: str) -> None:
        for sub in ("confluence", "servicenow", "git", "live"):
            (self.sources / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "system.yaml").write_text(manifest_text, encoding="utf-8")
        live = self.sources / "live" / "discovery.yaml"
        if not live.exists():
            live.write_text("# Read-only discovery results land here (WP-O5).\n{}\n", "utf-8")

    # -- state (resume) -------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        path = self.root / _STATE
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def mark(self, source: str, record: Mapping[str, object]) -> None:
        state = self.state()
        state[source] = dict(record)
        (self.root / _STATE).write_text(json.dumps(state, indent=2, sort_keys=True), "utf-8")

    def forget(self, source: str) -> None:
        state = self.state()
        state.pop(source, None)
        (self.root / _STATE).write_text(json.dumps(state, indent=2, sort_keys=True), "utf-8")

    # -- writers -------------------------------------------------------------------------

    def write_records(
        self, name: str, rows: Sequence[Mapping[str, Any]] | Mapping[str, Any]
    ) -> None:
        text = dump_yaml(rows)
        header = "# Written by `supportagent onboard ingest` (redacted at ingest).\n"
        (self.sources / "servicenow" / f"{name}.yaml").write_text(header + text, "utf-8")

    def replace_space(self, space: str, pages: Sequence[IngestedPage]) -> None:
        folder = self.sources / "confluence" / space
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        for page in pages:
            front = {
                "title": page.title,
                "page_id": page.page_id,
                "space": space,
                "version": page.version,
                "last_updated": page.last_updated.isoformat() if page.last_updated else None,
                "labels": list(page.labels),
                "url": page.url,
            }
            body = "---\n" + dump_yaml(front) + "---\n"
            (folder / f"{page.page_id}.md").write_text(body + page.markdown, encoding="utf-8")

    def replace_repos(self, repos: Sequence[str], files: Mapping[str, str]) -> None:
        git = self.sources / "git"
        for repo in repos:
            target = git / _safe(repo)
            if target.exists():
                shutil.rmtree(target)
        for key, text in files.items():
            path = git / _safe(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


def _safe(key: str) -> Path:
    rel = PurePosixPath(key)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ConfigError(f"refusing to write repository path {key!r} outside the workspace")  # noqa: TRY003
    return Path(*rel.parts)


__all__ = ["Workspace"]
