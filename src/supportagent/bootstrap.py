"""Composition root — the only place adapters are constructed (01 §2).

``build_container()`` wires ports to adapters and hands the graph to
``entrypoints``. It is empty in WP-000; the wiring lands with WP-002/WP-003.
"""

from __future__ import annotations


def build_container() -> object:
    """Build the v0.0 object graph. WP-000 placeholder: nothing is wired yet."""
    raise NotImplementedError("container wiring lands in WP-002 (config) / WP-003 (db)")
