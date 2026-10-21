"""BL-006: ``llm.provider: ollama`` wires a keyless local provider (ARCH §9 air gap)."""

from __future__ import annotations

from typing import Any

import pytest

from supportagent.adapters.fakes.events import RecordingEventSink
from supportagent.adapters.fakes.ids import SequentialIdGenerator
from supportagent.bootstrap import _build_llm
from supportagent.config.schema import AppConfig
from tests.unit.test_triage_service import _shadow_config


def _ollama_config(base_url: str | None) -> AppConfig:
    data = _shadow_config().model_dump(mode="json")
    for secret in ("client_id", "client_secret", "session_secret"):
        data["identity"][secret] = "o-" + secret + "-" + "x" * 32
    data["servicenow"]["auth"] = {"kind": "basic", "user": "svc", "password": "pw-fake"}
    data["llm"].update(provider="ollama", api_key=None, base_url=base_url)
    data["llm"]["slots"]["classify"]["model_id"] = "llama3.1:8b-instruct-q4_K_M"
    return AppConfig.model_validate(data)


@pytest.mark.parametrize("base_url", [None, "http://gpu-box.internal:11434"])
def test_ollama_provider_builds_without_an_api_key(clock: Any, base_url: str | None) -> None:
    client = _build_llm(
        _ollama_config(base_url), RecordingEventSink(), SequentialIdGenerator(), clock
    )
    assert client is not None


def test_ollama_rejects_an_api_key() -> None:
    data = _ollama_config(None).model_dump(mode="json")
    for secret in ("client_id", "client_secret", "session_secret"):
        data["identity"][secret] = "o-" + secret + "-" + "x" * 32
    data["servicenow"]["auth"] = {"kind": "basic", "user": "svc", "password": "pw-fake"}
    data["llm"]["api_key"] = "sk-not-for-ollama"
    with pytest.raises(ValueError, match="api_key"):
        AppConfig.model_validate(data)
