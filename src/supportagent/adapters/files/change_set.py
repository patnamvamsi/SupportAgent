"""A change set over a reference system (WP-O8): the system as it is some time later.

``evals/onboarding/drift/<system>.yaml`` says, relative to the system's
directory, what to ``remove``, which ``files`` to write, which text to
``replace`` (each ``old`` must occur), and which records to ``append`` to a
YAML list (``path`` for a top-level list, ``path#key`` for a list under a
key). :func:`apply_change_set` writes the changed system to a new directory;
the reference system itself is never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from supportagent.domain.errors import ConfigError


def apply_change_set(system: Path, changes: dict[str, Any], out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(system, out)
    for rel in changes.get("remove") or []:
        target = _inside(out, rel)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            raise ConfigError(f"change set: nothing to remove at {rel}")  # noqa: TRY003
    for rel, text in (changes.get("files") or {}).items():
        target = _inside(out, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(text), encoding="utf-8")
    for edit in changes.get("replace") or []:
        target = _inside(out, str(edit["file"]))
        text = target.read_text(encoding="utf-8")
        old, new = str(edit["old"]), str(edit["new"])
        if old not in text:
            raise ConfigError(f"change set: {edit['file']} has no {old!r}")  # noqa: TRY003
        target.write_text(text.replace(old, new), encoding="utf-8")
    for ref, records in (changes.get("append") or {}).items():
        rel, _, key = str(ref).partition("#")
        target = _inside(out, rel)
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
        rows = doc.setdefault(key, []) if key else doc
        if not isinstance(rows, list):
            raise ConfigError(f"change set: {ref} is not a list")  # noqa: TRY003
        rows.extend(records)
        target.write_text(yaml.safe_dump(doc, sort_keys=False, width=100), encoding="utf-8")
    return out


def _inside(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ConfigError(f"change set: {rel} is outside the system")  # noqa: TRY003
    return target


__all__ = ["apply_change_set"]
