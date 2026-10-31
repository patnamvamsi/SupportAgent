"""Load the plugin registry (ADR-0015 §2, WP-O1): ``plugins/index.yaml`` and each manifest.

The index pins every plugin: its version and the sha256 of its manifest. A
plugin directory the index doesn't list, a version that differs, or a
manifest whose bytes changed since it was pinned doesn't load. That is the
supply-chain floor for first-party plugins in this repo; signatures arrive
with external catalogs. ``supportagent plugin index`` re-pins after a
reviewed change.

ADR-0017: a plugin can be one self-contained directory, its pack under
``pack/``. Such a plugin's pin also carries ``tree_sha256``, the digest of
every file in it, so a changed runbook doesn't load either. A **catalogue**
of available plugins has the same layout and index; :func:`install_plugin`
copies one in and pins it.
"""

from __future__ import annotations

import hashlib
import shutil
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


def tree_sha256(directory: Path) -> str | None:
    """The digest of every file in a plugin beyond its manifest; ``None`` if there are none.

    Recorded model answers (``recordings/``) are a cache, not knowledge: they're left out.
    """
    files = [
        p
        for p in sorted(directory.rglob("*"))
        if p.is_file()
        and p != directory / MANIFEST
        and not {"__pycache__", "recordings"} & set(p.relative_to(directory).parts)
    ]
    if not files:
        return None
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(directory).as_posix().encode())
        h.update(b"\x00")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


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
        if pin.get("tree_sha256", tree_sha256(directory) or "") != (tree_sha256(directory) or ""):
            raise ConfigError(  # noqa: TRY003
                f"plugin {plugin_id}: its files changed since it was pinned "
                "(review them, then `supportagent plugin index`)"
            )
        loaded.append(LoadedPlugin(manifest, directory, digest))
    return loaded


def _pin_row(directory: Path) -> str:
    manifest = load_manifest(directory)
    digest = manifest_sha256(directory / MANIFEST)
    tree = tree_sha256(directory)
    extra = f", tree_sha256: {tree}" if tree is not None else ""
    return f"  - {{id: {manifest.id}, version: {manifest.version}, sha256: {digest}{extra}}}"


def write_index(root: Path) -> list[str]:
    """Pin every plugin under ``root`` as it is now; returns the ids pinned."""
    rows = []
    for directory in sorted(d for d in root.iterdir() if (d / MANIFEST).is_file()):
        rows.append(_pin_row(directory))
    header = (
        "# The plugin registry (ADR-0015 §2): each plugin pinned by version and the\n"
        "# sha256 of its manifest. Written by `supportagent plugin index` after review;\n"
        "# a manifest that changes without re-pinning doesn't load.\n"
    )
    (root / INDEX).write_text(header + "plugins:\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return [r.split("id: ", 1)[1].split(",", 1)[0] for r in rows]


__all__ = [
    "INDEX",
    "MANIFEST",
    "LoadedPlugin",
    "install_plugin",
    "load_manifest",
    "load_registry",
    "pin_one",
    "tree_sha256",
    "write_index",
]


def pin_one(root: Path, plugin_id: str) -> None:
    """Pin (or re-pin) one plugin, leaving every other pin as it was."""
    index = load_yaml_file(root / INDEX)
    entries = [
        e for e in index.get("plugins") or [] if isinstance(e, dict) and e.get("id") != plugin_id
    ]
    rows = ["  - {" + ", ".join(f"{k}: {v}" for k, v in e.items()) + "}" for e in entries] + [
        _pin_row(root / plugin_id)
    ]
    rows.sort()
    header = (
        "# The plugin registry (ADR-0015 §2): each plugin pinned by version and the\n"
        "# sha256 of its manifest. Written by `supportagent plugin index` after review;\n"
        "# a manifest that changes without re-pinning doesn't load.\n"
    )
    (root / INDEX).write_text(header + "plugins:\n" + "\n".join(rows) + "\n", encoding="utf-8")


def install_plugin(catalog: LoadedPlugin, root: Path) -> Path:
    """ADR-0017: copy a catalogue's (verified) plugin into ``root`` and pin it there."""
    target = root / catalog.manifest.id
    if target.exists():
        raise ConfigError(f"plugin {catalog.manifest.id} is already installed")  # noqa: TRY003
    shutil.copytree(
        catalog.directory, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    pin_one(root, catalog.manifest.id)
    return target
