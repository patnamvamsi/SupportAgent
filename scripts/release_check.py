"""Check that a release tag may be released (06 §6), and describe it for the workflow.

    python scripts/release_check.py v0.1.0            # human-readable
    python scripts/release_check.py v0.1.0 --github-output "$GITHUB_OUTPUT"

A tag is releasable when:

- it is ``v`` plus the version in ``pyproject.toml``: ``X.Y.Z`` or a release
  candidate ``X.Y.ZrcN``;
- ``docs/releases/X.Y.Z.md`` exists and names this build's head migration
  revision (06 §2: every release records its head);
- for a final release, the notes aren't marked draft.

It prints the version, the floating ``X.Y`` tag and whether it's a pre-release.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_VERSION = re.compile(r"^(?P<base>(?P<minor>\d+\.\d+)\.\d+)(?P<rc>rc\d+)?$")
_REVISION = re.compile(r'^revision\s*(?::\s*str\s*)?=\s*["\']([^"\']+)["\']', re.MULTILINE)
_DRAFT = re.compile(r"\bdraft\b", re.IGNORECASE)


@dataclass(frozen=True)
class Release:
    tag: str
    version: str
    minor: str
    prerelease: bool
    notes: Path
    head: str


class NotReleasableError(Exception):
    pass


def project_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def head_revision(root: Path) -> str:
    """The newest migration: the one no other migration revises."""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in (root / "migrations" / "versions").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        found = _REVISION.search(text)
        if found is None:
            continue
        revisions.add(found.group(1))
        down = re.search(r'^down_revision\s*(?::[^=]+)?=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if down is not None:
            parents.add(down.group(1))
    heads = sorted(revisions - parents)
    if len(heads) != 1:
        raise NotReleasableError(f"migrations must have exactly one head, found {heads or 'none'}")  # noqa: TRY003
    return heads[0]


def check(tag: str, root: Path = ROOT) -> Release:
    version = project_version(root)
    if tag != f"v{version}":
        raise NotReleasableError(f"tag {tag} doesn't match pyproject.toml's version {version}")  # noqa: TRY003
    match = _VERSION.match(version)
    if match is None:
        raise NotReleasableError(f"version {version} must be X.Y.Z or X.Y.ZrcN")  # noqa: TRY003
    notes = root / "docs" / "releases" / f"{match['base']}.md"
    if not notes.is_file():
        raise NotReleasableError(f"release notes {notes.relative_to(root)} are missing")  # noqa: TRY003
    text = notes.read_text(encoding="utf-8")
    head = head_revision(root)
    if f"`{head}`" not in text:
        raise NotReleasableError(f"{notes.relative_to(root)} must name the head revision `{head}`")  # noqa: TRY003
    prerelease = match["rc"] is not None
    if not prerelease and _DRAFT.search(text.split("\n## ", 1)[0]):
        raise NotReleasableError(f"{notes.relative_to(root)} is still marked draft")  # noqa: TRY003
    return Release(tag, version, match["minor"], prerelease, notes.relative_to(root), head)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag")
    parser.add_argument("--github-output", type=Path, help="append the outputs to this file")
    args = parser.parse_args(argv)
    try:
        release = check(args.tag)
    except NotReleasableError as exc:
        print(f"not releasable: {exc}", file=sys.stderr)
        return 1
    outputs = {
        "version": release.version,
        "minor": release.minor,
        "prerelease": str(release.prerelease).lower(),
        "notes": str(release.notes),
        "head": release.head,
    }
    for key, value in outputs.items():
        print(f"{key}={value}")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as out:
            out.writelines(f"{key}={value}\n" for key, value in outputs.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
