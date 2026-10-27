"""The Git-markdown knowledge source (ADR-0011 §4, WP-M08).

Every ``*.md`` under the source folder is one doc. An optional ``---`` YAML
front-matter block gives its title, provenance, visibility and scope
(``DocFrontMatter``); without one, the title is the first heading and the doc
is ``internal``. The doc id is ``<space>:<relative path>``; the version
defaults to the first 12 hex digits of the file's hash. Text is redacted
before chunking and hashing (INV-06).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import ValidationError

from supportagent.domain.errors import ConfigError
from supportagent.domain.knowledge import DocChunk, chunk_document
from supportagent.domain.model.knowledge import DocFrontMatter

_FENCE = "---"


def load_markdown_source(root: Path, *, space: str, redact: Callable[[str], str]) -> list[DocChunk]:
    if not root.is_dir():
        raise ConfigError(f"knowledge source {space!r}: {root} is not a directory")  # noqa: TRY003
    chunks: list[DocChunk] = []
    for path in sorted(root.rglob("*.md")):
        raw = path.read_text(encoding="utf-8")
        front, body = split_front_matter(raw, path)
        rel = path.relative_to(root).as_posix()
        if "title" not in front:
            front["title"] = _first_heading(body) or path.stem
        try:
            matter = DocFrontMatter.model_validate(front)
        except ValidationError as exc:
            raise ConfigError(f"{path}: front matter: {exc}") from exc  # noqa: TRY003
        version = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        meta = matter.meta(doc_id=f"{space}:{rel}", space=space, version=version)
        chunks.extend(chunk_document(meta, body, redact=redact))
    return chunks


def split_front_matter(raw: str, path: Path) -> tuple[dict[str, object], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return {}, raw
    for end in range(1, len(lines)):
        if lines[end].strip() == _FENCE:
            loaded = yaml.safe_load("\n".join(lines[1:end])) or {}
            if not isinstance(loaded, dict):
                raise ConfigError(f"{path}: front matter must be a mapping")  # noqa: TRY003
            return {str(k): v for k, v in loaded.items()}, "\n".join(lines[end + 1 :])
    raise ConfigError(f"{path}: unterminated front matter")  # noqa: TRY003


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
    return None


__all__ = ["load_markdown_source", "split_front_matter"]
