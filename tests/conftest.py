"""Global test options (04 §3-§6).

Sockets are disabled by default (``--disable-socket`` in the pyproject
``addopts``): unit/property/contract suites run with **no network and no
Docker** (04 §1). Suites that need a live endpoint opt in per run
(``make record-sn`` via ``--allow-hosts``; live-LLM evals via ``--live-llm``).

The fixture table in 04 §3 (``clock``, ``ids``, ``db``, ``uow``, ``events``,
``fake_sn``, ``fake_llm``, ``fake_oidc``) lands with the WPs that own it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TESTS_ROOT = Path(__file__).resolve().parent
# Top-level test directory → 04 §1 marker, so ``pytest -m "unit or property"``
# selects each suite without per-function annotations.
_LAYER_BY_DIR: dict[str, str] = {
    "unit": "unit",
    "property": "property",
    "contract": "contract",
    "invariants": "invariant",
    "integration": "integration",
    "e2e": "e2e",
    "faults": "faults",
    "soak": "soak",
}
# Markers that count as "belongs to a suite layer" (04 §1). The directory map
# covers all of these except the eval layers, whose tests declare the marker
# explicitly (WP-016).
_LAYER_MARKERS: frozenset[str] = frozenset(_LAYER_BY_DIR.values()) | {
    "eval_smoke",
    "eval_quality",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    unmarked: list[str] = []
    for item in items:
        try:
            top = item.path.relative_to(_TESTS_ROOT).parts[0]
        except ValueError:
            top = None
        # NB: `add_marker` is unconditional — `item.keywords` also contains
        # enclosing Dir/fixture names (e.g. "unit" for tests/unit/), which
        # are keywords, not markers, and would mask the real test.
        if top is not None and (marker := _LAYER_BY_DIR.get(top)):
            item.add_marker(marker)
        if not any(m.name in _LAYER_MARKERS for m in item.iter_markers()):
            unmarked.append(item.nodeid)
    # 04 §1: every test belongs to exactly one suite layer, and a CI job runs
    # each layer by marker. A test outside a mapped directory with no layer
    # marker of its own (e.g. ``tests/test_x.py``) would be collected but run
    # by no job — fail loudly at collection instead of silently skipping it.
    if unmarked:
        message = (
            "tests without a layer marker (04 §1) — no CI job would run them; "
            "move them into a mapped tests/<layer>/ directory or declare a marker "
            f"from {sorted(_LAYER_MARKERS)}: {unmarked}"
        )
        raise pytest.UsageError(message)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--db",
        default="sqlite",
        choices=["sqlite", "postgres"],
        help="database backend for invariant/integration suites (04 §6)",
    )
    parser.addoption(
        "--live-llm",
        action="store_true",
        default=False,
        help="permit live-LLM eval calls (default off; 04 §1)",
    )
