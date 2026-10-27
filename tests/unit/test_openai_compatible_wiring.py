"""BL-042: ``llm.base_url`` points the ``openai`` provider at a compatible server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from supportagent import bootstrap
from supportagent.adapters.fakes.events import RecordingEventSink
from supportagent.adapters.fakes.ids import SequentialIdGenerator
from supportagent.adapters.files.taxonomy_loader import load_classification
from supportagent.adapters.llm.prompts import PromptRegistry
from supportagent.config.schema import AppConfig
from supportagent.domain.slots import SlotName
from tests.unit.test_triage_service import _shadow_config


def _openai_config(base_url: str | None) -> AppConfig:
    data = _shadow_config().model_dump(mode="json")
    for secret in ("client_id", "client_secret", "session_secret"):
        data["identity"][secret] = "o-" + secret + "-" + "x" * 32
    data["servicenow"]["auth"] = {"kind": "basic", "user": "svc", "password": "pw-fake"}
    data["llm"].update(provider="openai", api_key="lm-studio", base_url=base_url)
    data["llm"]["slots"]["classify"]["model_id"] = "qwen/qwen3.8-27b"
    return AppConfig.model_validate(data)


@pytest.mark.parametrize("base_url", [None, "http://gpu-box.internal:1234/v1"])
def test_openai_client_receives_the_configured_base_url(
    clock: Any, monkeypatch: pytest.MonkeyPatch, base_url: str | None
) -> None:
    seen: dict[str, Any] = {}

    def fake_openai(**kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr("supportagent.bootstrap.openai.OpenAI", fake_openai)
    client = bootstrap._build_llm(
        _openai_config(base_url), RecordingEventSink(), SequentialIdGenerator(), clock
    )
    assert client is not None
    assert seen["base_url"] == base_url
    assert seen["api_key"] == "lm-studio"


def test_classify_prompt_lists_every_taxonomy_label() -> None:
    """BL-043: the model picks from the shipped labels instead of inventing slugs."""
    config = _openai_config(None)
    labels = bootstrap._taxonomy_labels(config)
    rendered = PromptRegistry(Path(config.paths.prompts)).render(
        SlotName.CLASSIFY,
        {"taxonomy_labels": labels, "pack_guidance": "", "ticket_subject": "s", "ticket_body": "b"},
    )
    for label in load_classification(Path(config.paths.taxonomy)).labels:
        assert f"- {label.id}: " in rendered.system
    assert rendered.version == "v3"
