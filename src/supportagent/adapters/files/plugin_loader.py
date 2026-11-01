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
    # ADR-0017 (WP-A6): a disabled plugin stays installed and pinned but isn't loaded.
    disabled = {pid for pid, pin in pinned.items() if pin.get("disabled", "").lower() == "true"}
    on_disk = sorted(d.name for d in root.iterdir() if (d / MANIFEST).is_file())
    unpinned = sorted(set(on_disk) - set(pinned))
    if unpinned:
        raise ConfigError(  # noqa: TRY003
            f"plugins not in {INDEX}: {unpinned} (review them, then `supportagent plugin index`)"
        )
    loaded: list[LoadedPlugin] = []
    for plugin_id, pin in sorted(pinned.items()):
        if plugin_id in disabled:
            continue
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


_HEADER = (
    "# The plugin registry (ADR-0015 §2): each plugin pinned by version and the\n"
    "# sha256 of its manifest. Written by `supportagent plugin index` after review;\n"
    "# a manifest that changes without re-pinning doesn't load.\n"
)


def _pin(directory: Path) -> dict[str, object]:
    manifest = load_manifest(directory)
    pin: dict[str, object] = {
        "id": manifest.id,
        "version": manifest.version,
        "sha256": manifest_sha256(directory / MANIFEST),
    }
    if (tree := tree_sha256(directory)) is not None:
        pin["tree_sha256"] = tree
    return pin


def _entries(root: Path) -> list[dict[str, object]]:
    if not (root / INDEX).is_file():
        return []
    return [e for e in load_yaml_file(root / INDEX).get("plugins") or [] if isinstance(e, dict)]


def _write(root: Path, entries: list[dict[str, object]]) -> None:
    def value(v: object) -> str:
        return str(v).lower() if isinstance(v, bool) else str(v)

    rows = sorted(
        "  - {" + ", ".join(f"{k}: {value(v)}" for k, v in e.items()) + "}" for e in entries
    )
    (root / INDEX).write_text(_HEADER + "plugins:\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _keep_disabled(pin: dict[str, object], previous: list[dict[str, object]]) -> dict[str, object]:
    was = next((e for e in previous if e.get("id") == pin["id"]), {})
    return pin | ({"disabled": True} if was.get("disabled") is True else {})


def write_index(root: Path) -> list[str]:
    """Pin every plugin under ``root`` as it is now (a disabled one stays disabled)."""
    previous = _entries(root)
    pins = [
        _keep_disabled(_pin(d), previous)
        for d in sorted(d for d in root.iterdir() if (d / MANIFEST).is_file())
    ]
    _write(root, pins)
    return [str(p["id"]) for p in pins]


__all__ = [
    "INDEX",
    "MANIFEST",
    "LoadedPlugin",
    "disabled_plugins",
    "index_sha256",
    "install_plugin",
    "load_manifest",
    "load_registry",
    "pin_one",
    "set_disabled",
    "tree_sha256",
    "write_index",
]


def pin_one(root: Path, plugin_id: str) -> None:
    """Pin (or re-pin) one plugin, leaving every other pin as it was."""
    previous = _entries(root)
    others = [e for e in previous if e.get("id") != plugin_id]
    _write(root, [*others, _keep_disabled(_pin(root / plugin_id), previous)])


def set_disabled(root: Path, plugin_id: str, *, disabled: bool) -> None:
    """``plugin disable`` / ``enable`` (ADR-0017, WP-A6): flip the flag, change nothing else."""
    entries = _entries(root)
    if not any(e.get("id") == plugin_id for e in entries):
        raise ConfigError(f"plugin {plugin_id!r} is not installed")  # noqa: TRY003
    updated = []
    for entry in entries:
        if entry.get("id") != plugin_id:
            updated.append(entry)
            continue
        flipped = {k: v for k, v in entry.items() if k != "disabled"}
        updated.append(flipped | ({"disabled": True} if disabled else {}))
    _write(root, updated)


def disabled_plugins(root: Path) -> list[str]:
    """The installed plugins ``plugin disable`` took out (ADR-0017, WP-A6)."""
    return sorted(str(e["id"]) for e in _entries(root) if e.get("disabled") is True)


def index_sha256(root: Path) -> str | None:
    """The digest of the plugin index: any install, re-pin or toggle changes it."""
    path = root / INDEX
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


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
