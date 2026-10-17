# 0004. PyYAML as the explicit runtime dependency for config parsing

- **Status:** accepted
- **Date:** 2026-10-18
- **Code PR:** WP-002
- **Spec sections changed:** none — adds `pyyaml` to the 05 §1 runtime dependency allowlist (which 05 §1 says requires an ADR)

## Context

02 §1 defines `supportagent.yaml` and the taxonomy/catalog files as YAML, and 10-wp §WP-002 delivers the loaders. 05 §1 closes the runtime dependency allowlist: "Adding any other runtime dependency needs an ADR." `pyyaml` is not on that list, yet it is the only parser that matches the spec's `yaml.safe_load`-style reading of plain-YAML customer files (no anchors, no custom tags), and it is already present transitively in `uv.lock`.

The WP-002 red phase deliberately kept shared test helpers (`tests/builders.py`) YAML-free; the one module that writes YAML fixtures is `tests/yaml_io.py`, so the dependency boundary is visible in both red and green.

## Decision

Adopt `pyyaml` (pinned by `uv.lock`) as an explicit runtime dependency. Production code may import it only at the two file-reading boundaries: `config/loader.py` (config file) and `adapters/files/` (catalog and taxonomy files). It is **not** a `domain/` or `ports/` dependency: `import-linter` gains `yaml` to the forbidden lists of `domain-pure` and `ports-protocol-only`, so the pure core stays free of it and the 05 §1 allowlist stays closed by construction.

## Consequences

- `load_config(path, env)` and the `adapters/files` loaders parse YAML; everything downstream (`config/schema.py` models, `domain/model/catalog.py`, `domain/model/taxonomy.py`) consumes plain dicts and stays parser-agnostic.
- Removing YAML support (for example a future JSON config format) is a one-ADR, two-module change.
- The dependency is declared in `pyproject.toml`, so it no longer depends on a transitive edge (bandit/cel-python/libcst) for availability.
