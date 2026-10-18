"""WP-001: the spec docs stay canonical for action kinds (ARCH §12.2)."""

from __future__ import annotations

import re
from pathlib import Path

from supportagent.domain.kinds import ActionKind

SPEC_DIR = Path(__file__).resolve().parents[2] / "docs" / "spec"


def test_docs_action_kinds_canonical() -> None:
    # CI greps the spec docs for action-kind names and fails on any token that
    # is not in the ActionKind enum. A snake_case token is a candidate when it
    # has two or more words and every word appears in the enum's word
    # vocabulary — that keeps ordinary prose (safe_default, prior_zeroed, ...)
    # out while catching invented kinds.
    kinds = set(ActionKind)
    vocabulary: set[str] = set()
    for kind in kinds:
        vocabulary.update(kind.value.split("_"))
    candidates: set[str] = set()
    for doc in sorted(SPEC_DIR.glob("*.md")):
        tokens = re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", doc.read_text(encoding="utf-8"))
        for token in tokens:
            words = token.split("_")
            if len(words) >= 2 and all(word in vocabulary for word in words):
                candidates.add(token)
    unknown = {token for token in candidates if token not in kinds}
    assert not unknown, (
        f"action-kind-looking tokens in docs/spec not in ActionKind: {sorted(unknown)}"
    )
