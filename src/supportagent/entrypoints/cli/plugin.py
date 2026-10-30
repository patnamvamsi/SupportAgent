"""``supportagent plugin ...`` and ``supportagent onboard detect`` (ADR-0015 §2, WP-O1).

``plugin list`` / ``show`` read the pinned registry (``plugins/index.yaml``).
``plugin validate`` loads it the way the app does, so a changed manifest that
wasn't re-pinned fails here first. ``plugin index`` re-pins after review.
``onboard detect DIR`` says which plugins a system needs, and why. DIR is a
system in the onboarding lab's layout (``system.yaml`` plus ``sources/``);
WP-O2 points the same detection at live sources.
"""

from __future__ import annotations

from pathlib import Path

import typer

from supportagent.adapters.files.plugin_loader import LoadedPlugin
from supportagent.bootstrap import (
    PLUGINS_DIR,
    detect_plugins,
    ingest_system,
    load_plugins,
    onboard_graph,
    pin_plugins,
)
from supportagent.config.loader import load_config_default
from supportagent.domain.errors import ConfigError, ExternalServiceError

plugin_app = typer.Typer(help="The plugin registry (ADR-0015).", no_args_is_help=True)
onboard_app = typer.Typer(help="Onboarding a system (ADR-0015).", no_args_is_help=True)

_ROOT = typer.Option(PLUGINS_DIR, "--root", help="The plugin registry directory.")


def _registry(root: Path) -> list[LoadedPlugin]:
    try:
        return load_plugins(root)
    except ConfigError as exc:
        typer.echo(f"plugin registry: {exc}", err=True)
        raise typer.Exit(2) from exc


@plugin_app.command("list")
def list_plugins(root: Path = _ROOT) -> None:
    """Every pinned plugin: version, status, what it provides, the access it needs."""
    for plugin in _registry(root):
        m = plugin.manifest
        parts = []
        if m.provides.pack:
            parts.append(f"pack {m.provides.pack}")
        if m.provides.capabilities:
            parts.append(f"{len(m.provides.capabilities)} capabilities")
        access = ", ".join(m.requires) or "no access"
        typer.echo(f"{m.id:<16} {m.version:<8} {m.status:<10} {'; '.join(parts) or '-'}; {access}")


@plugin_app.command("show")
def show(plugin_id: str = typer.Argument(..., help="The plugin id."), root: Path = _ROOT) -> None:
    """One plugin's manifest in full: capabilities, backends, access contracts, detectors."""
    found = [p for p in _registry(root) if p.manifest.id == plugin_id]
    if not found:
        typer.echo(f"no plugin {plugin_id!r}", err=True)
        raise typer.Exit(2)
    m = found[0].manifest
    typer.echo(f"{m.id} {m.version} ({m.status}, {m.publisher}, {m.trust}): {m.description}")
    typer.echo(f"  pinned sha256 {found[0].sha256}")
    for name, cap in m.provides.capabilities.items():
        typer.echo(f"  capability {name}: {cap.description}")
    for backend in m.provides.backends:
        how = (
            f"first-party {backend.first_party}"
            if backend.first_party
            else (f"MCP {backend.mcp.image}" if backend.mcp else "?")
        )
        typer.echo(f"  backend {backend.capability}: {how} (access {backend.access or '-'})")
    for access_id, contract in m.requires.items():
        typer.echo(f"  access {access_id} on each {contract.target}: {contract.grant}")
        typer.echo(f"    verified: can {contract.verify_can}; cannot {contract.verify_cannot}")
    if m.provides.needs:
        typer.echo(f"  needs from other plugins: {', '.join(m.provides.needs)}")
    for signal in m.detectors.signals:
        typer.echo(f"  detector ({signal.kind}, weight {signal.weight:g})")
    typer.echo(f"  detected at a score of {m.detectors.min_score:g}")


@plugin_app.command("validate")
def validate(root: Path = _ROOT) -> None:
    """Load the registry as the app does: manifests valid, pinned, packs present."""
    plugins = _registry(root)
    typer.echo(f"{len(plugins)} plugin(s) valid and pinned")


@plugin_app.command("index")
def index(root: Path = _ROOT) -> None:
    """Re-pin every plugin (version and manifest sha256) after a reviewed change."""
    try:
        pinned = pin_plugins(root)
    except ConfigError as exc:
        typer.echo(f"plugin registry: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"pinned {len(pinned)} plugin(s): {', '.join(pinned)}")


@onboard_app.command("detect")
def onboard_detect(
    system: Path = typer.Argument(..., help="A system directory (system.yaml + sources/)."),  # noqa: B008
    root: Path = _ROOT,
    show_all: bool = typer.Option(False, "--all", help="Also list plugins that weren't detected."),
) -> None:
    """Which plugins the system needs, and the evidence for each."""
    try:
        detections = detect_plugins(system, load_plugins(root))
    except (ConfigError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    for d in detections:
        if d.detected or show_all:
            typer.echo(("needs  " if d.detected else "       ") + d.explain())


@onboard_app.command("ingest")
def onboard_ingest(
    system: Path = typer.Option(..., "--system", help="The system's system.yaml."),  # noqa: B008
    out: Path = typer.Option(..., "--out", help="The workspace to write (lab layout)."),  # noqa: B008
    config: Path | None = typer.Option(  # noqa: B008
        None, "--config", help="supportagent.yaml, for the ServiceNow connection."
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Re-read sources already done."),
) -> None:
    """Read a system's ServiceNow, Confluence and Git sources into a workspace (redacted).

    Read-only. A finished source is skipped on the next run unless --refresh,
    so an interrupted ingest resumes. Then: ``onboard detect <out>``.
    """
    try:
        app_config = load_config_default(config) if config is not None else None
        reports = ingest_system(app_config, system, out, refresh=refresh)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    except ExternalServiceError as exc:
        typer.echo(f"source unreachable: {exc} (re-run to resume)", err=True)
        raise typer.Exit(1) from exc
    for report in reports:
        typer.echo(report.line())
        for note in report.notes:
            typer.echo(f"    {note}")
    typer.echo(f"workspace: {out}  (next: supportagent onboard detect {out})")


@onboard_app.command("graph")
def onboard_graph_command(
    workspace: Path = typer.Option(..., "--workspace", help="An ingested workspace."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (database, model)."
    ),
    docs: bool = typer.Option(True, "--docs/--no-docs", help="Read documentation with the model."),
) -> None:
    """Build the system graph from a workspace and save it for review at /ui/onboarding."""
    try:
        report = onboard_graph(load_config_default(config), workspace, docs=docs)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    saved = report.saved
    typer.echo(
        f"graph: {report.nodes} nodes, {report.edges} edges, {report.questions} question(s); "
        f"saved {saved['new']} new, {saved['kept']} kept, {saved['gone']} no longer found"
    )
    if docs:
        typer.echo(
            f"docs: {report.pages_read} current pages read, {report.pages_skipped} left out "
            f"(legacy, draft, superseded), {report.rejected} facts rejected as ungrounded"
        )
    if report.failed_pages:
        typer.echo(f"no answer for pages {list(report.failed_pages)}: re-run to retry them")
    typer.echo("review it at /ui/onboarding")
