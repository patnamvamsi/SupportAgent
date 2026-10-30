"""Load the plugin registry (ADR-0015 §2, WP-O1): ``plugins/index.yaml`` and each manifest.

The index pins every plugin: its version and the sha256 of its manifest. A
plugin directory the index doesn't list, a version that differs, or a
manifest whose bytes changed since it was pinned doesn't load. That is the
supply-chain floor for first-party plugins in this repo; signatures arrive
with external catalogs. ``supportagent plugin index`` re-pins after a
reviewed change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from supportagent.adapters.files._yaml import load_yaml_file
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.plugin import PluginManifest

INDEX = "index.yaml"
MANIFEST = "plugin.yaml"


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    manifest: PluginManifest
    directory: Path
    sha256: str


def manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(directory: Path) -> PluginManifest:
    path = directory / MANIFEST
    try:
        manifest = PluginManifest.model_validate(load_yaml_file(path))
    except ValidationError as exc:
        raise ConfigError(f"plugin {directory.name}: {exc}") from exc  # noqa: TRY003
    if manifest.id != directory.name:
        raise ConfigError(  # noqa: TRY003
            f"plugin {directory.name}: manifest id {manifest.id!r} must match its directory"
        )
    return manifest


def load_registry(root: Path) -> list[LoadedPlugin]:
    """Every pinned plugin under ``root``, verified against the index."""
    index = load_yaml_file(root / INDEX)
    entries = index.get("plugins")
    if not isinstance(entries, list):
        raise ConfigError(f"{root / INDEX}: expected a 'plugins' list")  # noqa: TRY003
    pinned: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not {"id", "version", "sha256"} <= set(entry):
            raise ConfigError(f"{root / INDEX}: each entry needs id, version, sha256")  # noqa: TRY003
        pinned[str(entry["id"])] = {k: str(v) for k, v in entry.items()}
    on_disk = sorted(d.name for d in root.iterdir() if (d / MANIFEST).is_file())
    unpinned = sorted(set(on_disk) - set(pinned))
    if unpinned:
        raise ConfigError(  # noqa: TRY003
            f"plugins not in {INDEX}: {unpinned} (review them, then `supportagent plugin index`)"
        )
    loaded: list[LoadedPlugin] = []
    for plugin_id, pin in sorted(pinned.items()):
        directory = root / plugin_id
        if not (directory / MANIFEST).is_file():
            raise ConfigError(f"{INDEX} pins {plugin_id!r}, which has no {MANIFEST}")  # noqa: TRY003
        manifest = load_manifest(directory)
        digest = manifest_sha256(directory / MANIFEST)
        if manifest.version != pin["version"]:
            raise ConfigError(  # noqa: TRY003
                f"plugin {plugin_id}: version {manifest.version} but {INDEX} pins {pin['version']}"
            )
        if digest != pin["sha256"]:
            raise ConfigError(  # noqa: TRY003
                f"plugin {plugin_id}: manifest changed since it was pinned "
                "(review it, then `supportagent plugin index`)"
            )
        loaded.append(LoadedPlugin(manifest, directory, digest))
    return loaded


def write_index(root: Path) -> list[str]:
    """Pin every plugin under ``root`` as it is now; returns the ids pinned."""
    rows = []
    for directory in sorted(d for d in root.iterdir() if (d / MANIFEST).is_file()):
        manifest = load_manifest(directory)
        digest = manifest_sha256(directory / MANIFEST)
        rows.append(f"  - {{id: {manifest.id}, version: {manifest.version}, sha256: {digest}}}")
    header = (
        "# The plugin registry (ADR-0015 §2): each plugin pinned by version and the\n"
        "# sha256 of its manifest. Written by `supportagent plugin index` after review;\n"
        "# a manifest that changes without re-pinning doesn't load.\n"
    )
    (root / INDEX).write_text(header + "plugins:\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return [r.split("id: ", 1)[1].split(",", 1)[0] for r in rows]


__all__ = ["INDEX", "MANIFEST", "LoadedPlugin", "load_manifest", "load_registry", "write_index"]
