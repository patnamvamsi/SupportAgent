"""ADR-0021 §4a: PII is pseudonymised before the model sees a stage result.

The allow-list runs first, then each remaining string is analysed and every
PII value becomes a placeholder that is stable for the question. The traced
id passes through. An analyser that can't run fails closed.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from supportagent.adapters.fakes.clock import FrozenClock
from supportagent.app.doctor.base import CheckResult
from supportagent.app.doctor.checks.local import PiiCheck
from supportagent.app.pii import PiiGuard
from supportagent.domain.errors import PiiUnavailable
from supportagent.domain.fieldpath import project, valid_path, values_at
from supportagent.domain.pii import PiiSpan, Pseudonyms, valid_entity

pytestmark = pytest.mark.unit

_PATTERNS = {
    "PERSON": re.compile(r"\b(?:Jane Doe|John Smith)\b"),
    "EMAIL_ADDRESS": re.compile(r"\b[\w.]+@[\w.]+\.\w+\b"),
    "ACCOUNT": re.compile(r"\bACC-\d{6}\b"),
}


class RegexAnalyzer:
    """A stand-in for Presidio: fixed patterns, full confidence."""

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, text: str) -> list[PiiSpan]:
        self.calls += 1
        return [
            PiiSpan(m.start(), m.end(), entity, 1.0)
            for entity, pattern in _PATTERNS.items()
            for m in pattern.finditer(text)
        ]


class BrokenAnalyzer:
    def analyze(self, text: str) -> list[PiiSpan]:
        raise PiiUnavailable("presidio is not installed: install supportagent[pii]")  # noqa: TRY003


def _spans(text: str) -> list[PiiSpan]:
    return RegexAnalyzer().analyze(text)


def test_same_value_same_placeholder_across_texts() -> None:
    names = Pseudonyms()
    a = names.apply("Jane Doe paid", _spans("Jane Doe paid"))
    b = names.apply(
        "refund for Jane Doe and John Smith", _spans("refund for Jane Doe and John Smith")
    )
    assert a == "<PERSON_1> paid"
    assert b == "refund for <PERSON_1> and <PERSON_2>"
    assert names.counts == {"PERSON": 2}


def test_counts_never_carry_values() -> None:
    names = Pseudonyms()
    names.apply("mail jane@example.com", _spans("mail jane@example.com"))
    assert "jane" not in repr(names.counts)


def test_kept_value_passes_through() -> None:
    # A deployment recogniser for accounts would also match the traced id itself.
    text = "trade ACC-123456 booked for ACC-654321"
    names = Pseudonyms(keep=["ACC-123456"])
    assert names.apply(text, _spans(text)) == "trade ACC-123456 booked for <ACCOUNT_1>"


def test_overlap_keeps_the_higher_score() -> None:
    text = "Jane Doe"
    spans = [PiiSpan(0, 4, "PERSON", 0.4), PiiSpan(0, 8, "PERSON", 0.9)]
    assert Pseudonyms().apply(text, spans) == "<PERSON_1>"


def test_out_of_range_span_is_ignored() -> None:
    assert Pseudonyms().apply("abc", [PiiSpan(2, 9, "PERSON", 1.0)]) == "abc"


def test_project_keeps_only_allowed_fields() -> None:
    data = {
        "status": "REJECTED",
        "customer": {"name": "Jane Doe", "tier": "gold"},
        "rows": [{"id": 1, "note": "x", "email": "a@b.co"}, {"id": 2, "note": "y"}],
        "secret": "s",
    }
    got = project(data, ["status", "customer.tier", "rows[].id", "rows[].note", "absent.field"])
    assert got == {
        "status": "REJECTED",
        "customer": {"tier": "gold"},
        "rows": [{"id": 1, "note": "x"}, {"id": 2, "note": "y"}],
    }


def test_paths_and_entities_are_validated() -> None:
    assert valid_path("rows[].status")
    assert not valid_path("rows..x")
    assert not valid_path("$.rows")
    assert valid_entity("IBAN_CODE")
    assert not valid_entity("person")


def test_guard_allow_list_runs_before_detection() -> None:
    analyzer = RegexAnalyzer()
    guard = PiiGuard(analyzer, keep=["T-1"])
    got = guard.clean(
        {"id": "T-1", "owner": "Jane Doe", "reason": "no reference data for John Smith"},
        ["id", "reason"],
    )
    assert got == {"id": "T-1", "reason": "no reference data for <PERSON_1>"}
    assert analyzer.calls == 2  # the dropped field was never analysed
    assert guard.pseudonyms.counts == {"PERSON": 1}


def test_guard_is_consistent_across_stages() -> None:
    guard = PiiGuard(RegexAnalyzer())
    first = guard.clean({"by": "jane@example.com"}, ["by"])
    second = guard.text("retry requested by jane@example.com")
    assert first == {"by": "<EMAIL_ADDRESS_1>"}
    assert second == "retry requested by <EMAIL_ADDRESS_1>"


def test_guard_fails_closed() -> None:
    guard = PiiGuard(BrokenAnalyzer())
    with pytest.raises(PiiUnavailable, match="supportagent\\[pii\\]"):
        guard.clean({"note": "Jane Doe"}, ["note"])


def test_guard_leaves_non_strings_alone() -> None:
    got = PiiGuard(RegexAnalyzer()).clean(
        {"n": 3, "ok": True, "x": None, "s": ""}, ["n", "ok", "x", "s"]
    )
    assert got == {"n": 3, "ok": True, "x": None, "s": ""}


def test_values_at_steps_into_lists() -> None:
    data = {"matches": [{"ts": "a"}, {"ts": "b"}], "rows": []}
    assert values_at(data, "matches[].ts") == ["a", "b"]
    assert values_at(data, "rows[]") == []
    assert values_at(data, "rows") == [[]]
    assert values_at(data, "absent[].x") == []


# --- doctor -------------------------------------------------------------------


def _doctor(analyzer: object | None, engine: str) -> CheckResult:
    from tests.unit.test_doctor import _ctx  # noqa: PLC0415

    base = _ctx(clock=FrozenClock(start=datetime(2026, 11, 8, tzinfo=UTC)))
    pii = base.config.pii.model_copy(update={"engine": engine})
    config = base.config.model_copy(update={"pii": pii})
    return PiiCheck().run(replace(base, config=config, pii=analyzer))  # type: ignore[arg-type]


class SilentAnalyzer:
    def analyze(self, text: str) -> list[PiiSpan]:
        return []


def test_doctor_pii_off_passes() -> None:
    res = _doctor(None, "off")
    assert (res.status, res.detail) == ("pass", "off: traced data is never passed to the model")


def test_doctor_pii_finds_the_probe() -> None:
    res = _doctor(RegexAnalyzer(), "presidio")
    assert res.status == "pass"
    assert res.detail == "presidio: found EMAIL_ADDRESS, PERSON in a probe"


def test_doctor_pii_unavailable_fails() -> None:
    res = _doctor(BrokenAnalyzer(), "presidio")
    assert res.status == "fail" and "supportagent[pii]" in res.detail


def test_doctor_pii_blind_analyser_warns() -> None:
    assert _doctor(SilentAnalyzer(), "presidio").status == "warn"


def test_config_validates_entities_and_recognizers() -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    from supportagent.config.schema import PiiConfig  # noqa: PLC0415

    ok = PiiConfig.model_validate(
        {"engine": "presidio", "recognizers": [{"entity": "CUSTOMER_ID", "regex": r"CUST-\\d{8}"}]}
    )
    assert ok.recognizers[0].score == 0.6
    assert "DATE_TIME" not in ok.entities  # a trace needs its timestamps
    with pytest.raises(ValidationError, match="UPPER_SNAKE"):
        PiiConfig.model_validate({"entities": ["person"]})
    with pytest.raises(ValidationError, match="does not compile"):
        PiiConfig.model_validate({"recognizers": [{"entity": "X", "regex": "("}]})


def test_presidio_missing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib  # noqa: PLC0415

    from supportagent.adapters.pii.presidio import PresidioAnalyzer  # noqa: PLC0415

    real = importlib.import_module

    def no_presidio(name: str, package: str | None = None) -> object:
        if name.startswith(("presidio_analyzer", "spacy")):
            raise ImportError(name)
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", no_presidio)
    analyzer = PresidioAnalyzer(
        entities=["PERSON"], min_score=0.5, language="en", model="en_core_web_sm"
    )
    with pytest.raises(PiiUnavailable, match="supportagent\\[pii\\]"):
        analyzer.analyze("Jane Doe")
