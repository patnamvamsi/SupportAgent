"""Injectable faults (11-wp WP-106, 04 §1): ``SUPPORTAGENT_FAULT_INJECT=a,b,...``.

The variable is read only by ``config/loader.py`` (05 §6). The adapters'
fault decorators take the parsed set. ``gha_500`` is accepted now and acts
once the GitHub Actions tool lands (WP-107).
"""

from __future__ import annotations

from enum import StrEnum

from supportagent.domain.errors import ConfigError


class Fault(StrEnum):
    LLM_TIMEOUT = "llm_timeout"
    LLM_GARBAGE = "llm_garbage"
    LLM_429 = "llm_429"
    LLM_UNAVAILABLE = "llm_unavailable"
    SN_500 = "sn_500"
    SN_429 = "sn_429"
    GHA_500 = "gha_500"
    CRASH_AFTER_SEND = "crash_after_send"


LLM_FAULTS = frozenset({Fault.LLM_TIMEOUT, Fault.LLM_GARBAGE, Fault.LLM_429, Fault.LLM_UNAVAILABLE})
SINK_FAULTS = frozenset({Fault.SN_500, Fault.SN_429, Fault.CRASH_AFTER_SEND})


def parse_faults(value: str | None) -> frozenset[Fault]:
    """``"llm_timeout,sn_500"`` → the fault set; an unknown name is a ``ConfigError``."""
    names = [n.strip() for n in (value or "").split(",") if n.strip()]
    known = {f.value for f in Fault}
    unknown = sorted(set(names) - known)
    if unknown:
        raise ConfigError(  # noqa: TRY003
            f"SUPPORTAGENT_FAULT_INJECT: unknown fault(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(known))}"
        )
    return frozenset(Fault(n) for n in names)


__all__ = ["LLM_FAULTS", "SINK_FAULTS", "Fault", "parse_faults"]
