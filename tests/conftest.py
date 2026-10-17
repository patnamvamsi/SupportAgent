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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        try:
            top = item.path.relative_to(_TESTS_ROOT).parts[0]
        except ValueError:
            continue
        if marker := _LAYER_BY_DIR.get(top):
            item.add_marker(marker)


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
