"""Built-in secret detectors (01 §7.13, INV-06).

Re-exports :func:`built_in_detectors` (one instance per closed class) so the
redactor and the test seams can import it from the package root.
"""

from __future__ import annotations

from supportagent.adapters.llm.detectors.builtin import built_in_detectors

__all__ = ["built_in_detectors"]
