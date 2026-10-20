"""BL-005: the fixture LLM provider (ARCH §9: fixtures test code, not prompts)."""

from __future__ import annotations

import json

import pytest

from supportagent.adapters.llm.providers import make_llm_provider
from supportagent.adapters.llm.providers.fixture import FixtureAnswer, FixtureLLMProvider
from supportagent.domain.errors import LLMRateLimited, LLMTimeout, LLMUnavailable
from supportagent.ports.llm import ProviderRequest


def _request(user: str) -> ProviderRequest:
    return ProviderRequest(
        model_id="claude-sonnet-4-5-20250929",
        system="s",
        user=user,
        json_schema={},
        max_output_tokens=100,
        timeout_s=5,
    )


def test_first_matching_answer_wins_and_model_is_resolved_as_pinned() -> None:
    provider = FixtureLLMProvider(
        [
            FixtureAnswer.triage("database/saturation", "P2", match="pool"),
            FixtureAnswer.triage("network/latency", "P3"),
        ]
    )
    pool = provider.complete_json(_request("core-db pool at limit"))
    assert json.loads(pool.raw_text)["classification"] == "database/saturation"
    assert pool.resolved_model == "claude-sonnet-4-5-20250929"
    other = provider.complete_json(_request("dns is slow"))
    assert json.loads(other.raw_text)["classification"] == "network/latency"
    assert provider.calls == 2


@pytest.mark.parametrize(
    ("fault", "error"),
    [("timeout", LLMTimeout), ("rate_limited", LLMRateLimited), ("unavailable", LLMUnavailable)],
)
def test_scripted_faults_raise_typed_errors(fault: str, error: type[Exception]) -> None:
    provider = FixtureLLMProvider([FixtureAnswer(fault=fault)])  # type: ignore[arg-type]
    with pytest.raises(error):
        provider.complete_json(_request("x"))


def test_no_matching_answer_fails_closed() -> None:
    provider = FixtureLLMProvider([FixtureAnswer.triage("a/b", "P3", match="only-this")])
    with pytest.raises(LLMUnavailable, match="no scripted answer"):
        provider.complete_json(_request("something else"))


def test_load_replaces_the_script_and_raw_is_verbatim() -> None:
    provider = FixtureLLMProvider()
    provider.load([FixtureAnswer(raw="not json at all")])
    assert provider.complete_json(_request("x")).raw_text == "not json at all"


def test_answer_needs_exactly_one_of_raw_or_fault() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        FixtureAnswer()
    with pytest.raises(ValueError, match="exactly one"):
        FixtureAnswer(raw="{}", fault="timeout")


def test_factory_builds_the_fixture_provider() -> None:
    shared = FixtureLLMProvider()
    assert make_llm_provider("fixture", shared) is shared
    assert isinstance(make_llm_provider("fixture", []), FixtureLLMProvider)
