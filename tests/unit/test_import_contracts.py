"""WP-000: the six import-linter contracts from 01 §2 are configured (10-wp L37)."""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / ".importlinter"

# The contract names in the 01 §2 table, in dependency order.
REQUIRED_CONTRACTS: frozenset[str] = frozenset(
    {
        "domain-pure",
        "ports-protocol-only",
        "app-no-adapters",
        "adapters-independent",
        "only-bootstrap-wires",
        "entrypoints-thin",
    }
)


def test_import_contracts_configured() -> None:
    """``.importlinter`` names all six contracts from 01 §2."""
    text = _CONFIG.read_text(encoding="utf-8")
    names = set(re.findall(r"^name\s*=\s*(\S+)", text, re.M))
    missing = REQUIRED_CONTRACTS - names
    assert not missing, f"contracts missing from .importlinter: {sorted(missing)}"
