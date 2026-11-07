"""ADR-0021 §4a: the real Presidio analyser, when it is installed.

Skipped unless ``supportagent[pii]`` and the small spaCy model are installed
(``en_core_web_sm``). It checks the adapter against the engine itself: the
built-in recognisers, a deployment recogniser, the score threshold, and the
pseudonymised result end to end.
"""

from __future__ import annotations

import importlib.util

import pytest

from supportagent.adapters.pii.presidio import PiiRecognizerSpec, PresidioAnalyzer
from supportagent.app.pii import PiiGuard
from supportagent.domain.errors import PiiUnavailable

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        importlib.util.find_spec("presidio_analyzer") is None
        or importlib.util.find_spec("en_core_web_sm") is None,
        reason="supportagent[pii] and en_core_web_sm are not installed",
    ),
]


def _analyzer(model: str = "en_core_web_sm") -> PresidioAnalyzer:
    return PresidioAnalyzer(
        entities=["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"],
        min_score=0.4,
        language="en",
        model=model,
        recognizers=[PiiRecognizerSpec("CUSTOMER_ID", r"\bCUST-\d{8}\b", 0.9)],
    )


def test_presidio_pseudonymises_a_stage_result() -> None:
    guard = PiiGuard(_analyzer(), keep=["T-20261108"])
    got = guard.clean(
        {
            "matches": [
                {"line": "trade T-20261108 rejected: no counterparty for CUST-12345678"},
                {"line": "contact jane.doe@example.com for CUST-12345678"},
            ],
            "owner": "Jane Doe",
        },
        ["matches[].line"],
    )
    matches = got["matches"]
    assert isinstance(matches, list)
    lines = [m["line"] for m in matches]
    assert lines[0] == "trade T-20261108 rejected: no counterparty for <CUSTOMER_ID_1>"
    assert lines[1] == "contact <EMAIL_ADDRESS_1> for <CUSTOMER_ID_1>"
    assert "owner" not in got
    assert guard.pseudonyms.counts == {"CUSTOMER_ID": 1, "EMAIL_ADDRESS": 1}


def test_presidio_finds_a_person() -> None:
    spans = _analyzer().analyze("Please call Jane Doe back about her refund.")
    assert "PERSON" in {s.entity for s in spans}


def test_a_missing_model_is_never_downloaded() -> None:
    with pytest.raises(PiiUnavailable, match="python -m spacy download en_core_web_xx_missing"):
        _analyzer("en_core_web_xx_missing").analyze("Jane Doe")
