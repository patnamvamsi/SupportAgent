"""WP-000: the review history's spec anchors resolve (10-wp L37).

The spec is frozen for v0.0, so this test is the guard that keeps
``docs/CHANGELOG.md`` honest: its "where it landed" cells cite spec sections
(``ARCH §4.1``, ``CAP §2``, ``ROADMAP §5``). Per the anchor rule in the
CHANGELOG header, a cited section that does not exist fails CI.

Resolution rules:
- A citation is a ``§N`` or ``§N.M`` whose **nearest preceding doc token** on
  the same line names the document: ``ARCH``/``ARCHITECTURE`` →
  ``docs/spec/ARCHITECTURE.md``, ``CAP``/``CAPABILITIES`` →
  ``docs/spec/CAPABILITIES.md``, ``ROADMAP`` → ``docs/spec/ROADMAP.md``.
  Inherited lists (``ARCH §1, §4.2, §11``) resolve against the same doc.
- Citations with no doc token (the round-1/round-2 critique numbering, e.g.
  ``round 2 §4.5``, and the CHANGELOG's own ``### §N`` headings) are review
  history, not spec anchors, and are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CHANGELOG = _ROOT / "docs" / "CHANGELOG.md"
_DOCS: dict[str, Path] = {
    "ARCH": _ROOT / "docs" / "spec" / "ARCHITECTURE.md",
    "CAP": _ROOT / "docs" / "spec" / "CAPABILITIES.md",
    "ROADMAP": _ROOT / "docs" / "spec" / "ROADMAP.md",
}
_DOC_TOKEN = re.compile(r"\b(ARCHITECTURE|CAPABILITIES|ROADMAP|ARCH|CAP)\b")
_REF = re.compile(r"§(\d+(?:\.\d+)?)")
_ALIASES = {
    "ARCH": "ARCH",
    "ARCHITECTURE": "ARCH",
    "CAP": "CAP",
    "CAPABILITIES": "CAP",
    "ROADMAP": "ROADMAP",
}


def _section_numbers(path: Path) -> set[str]:
    """All ``## N.`` and ``### N.M`` heading numbers of a spec document."""
    numbers: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if (match := re.match(r"^## (\d+)\. ", line)) or (
            match := re.match(r"^### (\d+\.\d+)", line)
        ):
            numbers.add(match.group(1))
    return numbers


def test_changelog_anchors_resolve() -> None:
    """Every ARCH/CAP/ROADMAP citation in docs/CHANGELOG.md names a real heading."""
    valid = {doc: _section_numbers(path) for doc, path in _DOCS.items()}
    dangling: list[str] = []
    for lineno, line in enumerate(_CHANGELOG.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue  # the CHANGELOG's own headings are not spec anchors
        for ref in _REF.finditer(line):
            tokens = _DOC_TOKEN.findall(line[: ref.start()])
            if not tokens:
                continue  # not a spec anchor (review numbering / own headings)
            doc = _ALIASES[tokens[-1]]
            if ref.group(1) not in valid[doc]:
                dangling.append(f"line {lineno}: {doc} §{ref.group(1)} — {line.strip()[:70]}")
    message = "dangling spec anchors in docs/CHANGELOG.md:\n" + "\n".join(dangling)
    assert not dangling, message
