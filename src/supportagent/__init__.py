"""SupportAgent — an application support analyst.

The ``docs/`` tree (spec, tech-spec, review history) is the product; this
package is its v0.0 walking skeleton (``10-wp`` WP-000). Layout and
dependency rules: ``01-architecture-and-contracts`` §1-2 (frozen).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    #: Single-sourced from ``pyproject.toml`` (06 §1) through the installed metadata.
    __version__ = version("supportagent")
except PackageNotFoundError:  # pragma: no cover — a bare source tree, never installed
    __version__ = "0.0.0+unknown"
