"""``supportagent plugin ...`` and ``supportagent onboard detect`` (ADR-0015 §2, WP-O1).

``plugin list`` / ``show`` read the pinned registry (``plugins/index.yaml``).
``plugin validate`` loads it the way the app does, so a changed manifest that
wasn't re-pinned fails here first. ``plugin index`` re-pins after review.
``onboard detect DIR`` says which plugins a system needs, and why. DIR is a
system in the onboarding lab's layout (``system.yaml`` plus ``sources/``);
WP-O2 points the same detection at live sources.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import typer

from supportagent.adapters.files.plugin_loader import LoadedPlugin
from supportagent.app.request_review import request_id
from supportagent.bootstrap import (
    AUTOPILOT_STATE,
    detect_plugins,
    fetch_catalogs,
    ingest_system,
    install_and_record,
    load_plugins,
    onboard_bind,
    onboard_catalog,
    onboard_cycle,
    onboard_drift,
    onboard_evidence,
    onboard_flows,
    onboard_gaps,
    onboard_graph,
    onboard_incidents,
    onboard_readiness,
    onboard_requests,
    onboard_run,
    onboard_unbind,
    onboard_verify,
    pin_plugins,
    scaffold_plugin,
    toggle_plugin,
)
from supportagent.config.loader import load_config_default
from supportagent.domain.errors import (
    ConfigError,
    DomainValidationError,
    ExternalServiceError,
    NotFound,
)

plugin_app = typer.Typer(help="The plugin registry (ADR-0015).", no_args_is_help=True)
onboard_app = typer.Typer(help="Onboarding a system (ADR-0015).", no_args_is_help=True)

_ROOT = typer.Option(
    None, "--root", help="The plugin registry directory (default: the installed plugins)."
)


def _registry(root: Path | None) -> list[LoadedPlugin]:
    try:
        return load_plugins(root)
    except ConfigError as exc:
        typer.echo(f"plugin registry: {exc}", err=True)
        raise typer.Exit(2) from exc


@plugin_app.command("list")
def list_plugins(root: Path | None = _ROOT) -> None:
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
def show(
    plugin_id: str = typer.Argument(..., help="The plugin id."), root: Path | None = _ROOT
) -> None:
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
def validate(root: Path | None = _ROOT) -> None:
    """Load the registry as the app does: manifests valid, pinned, packs present."""
    plugins = _registry(root)
    typer.echo(f"{len(plugins)} plugin(s) valid and pinned")


@plugin_app.command("index")
def index(root: Path | None = _ROOT) -> None:
    """Re-pin every plugin (version and manifest sha256) after a reviewed change."""
    try:
        pinned = pin_plugins(root)
    except ConfigError as exc:
        typer.echo(f"plugin registry: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"pinned {len(pinned)} plugin(s): {', '.join(pinned)}")


@plugin_app.command("install")
def install(
    plugin_id: str = typer.Argument(
        ..., help="The plugin to install, from a configured catalogue."
    ),
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (catalogues, the model)."
    ),
) -> None:
    """Install a plugin from a catalogue (ADR-0017), pin it, and prove it on its own lab here.

    A plugin that fails its lab here is installed as a skeleton: onboarding can
    detect it and ask for its access, but its incident classes can't be ready.
    """
    try:
        result = install_and_record(load_config_default(config), plugin_id)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"installed {result.plugin} as {result.status}: {result.detail}")
    typer.echo("re-run `supportagent onboard run` to use it")


@plugin_app.command("fetch")
def fetch(
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (plugins.catalogs)."
    ),
) -> None:
    """Fetch each git catalogue at its pinned commit (read-only) and check its index.

    A commit already fetched is reused without the network. Exit 1 when a
    catalogue can't be fetched or doesn't load.
    """
    try:
        results = fetch_catalogs(load_config_default(config))
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not results:
        typer.echo("no catalogues configured (plugins.catalogs)")
    for label, offered, error in results:
        typer.echo(f"{'FAIL' if error else 'ok  '}  {label}: {error or f'{offered} plugin(s)'}")
    if any(error for _, _, error in results):
        raise typer.Exit(1)


@plugin_app.command("new")
def new(
    plugin_id: str = typer.Argument(..., help="The new plugin's id (the stack: go, ruby, ...)."),
    manifest: str = typer.Option(..., "--from", help="The manifest that names the stack (go.mod)."),
    out: Path = typer.Option(  # noqa: B008
        Path("plugin-drafts"), "--out", help="Where to write it (outside the registry)."
    ),
) -> None:
    """Scaffold a skeleton plugin from an onboarding gap (ADR-0017), to review and complete."""
    try:
        path = scaffold_plugin(plugin_id, manifest, out)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(
        f"wrote {path}: add its causes and a pack, then a catalogue or plugins/, then pin it"
    )


@plugin_app.command("disable")
def disable(
    plugin_id: str = typer.Argument(..., help="The installed plugin to take out of use."),
    config: Path = typer.Option(Path("supportagent.yaml"), "--config"),  # noqa: B008
) -> None:
    """Take a plugin out of detection and packs without uninstalling it (audited)."""
    _toggle(plugin_id, config, disabled=True)


@plugin_app.command("enable")
def enable(
    plugin_id: str = typer.Argument(..., help="The disabled plugin to put back in use."),
    config: Path = typer.Option(Path("supportagent.yaml"), "--config"),  # noqa: B008
) -> None:
    """Put a disabled plugin back into detection and packs (audited)."""
    _toggle(plugin_id, config, disabled=False)


def _toggle(plugin_id: str, config: Path, *, disabled: bool) -> None:
    try:
        toggle_plugin(load_config_default(config), plugin_id, disabled=disabled)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    state = "disabled" if disabled else "enabled"
    typer.echo(f"{plugin_id} {state}; it applies from the next start and `onboard run`")


@onboard_app.command("detect")
def onboard_detect(
    system: Path = typer.Argument(..., help="A system directory (system.yaml + sources/)."),  # noqa: B008
    root: Path | None = _ROOT,
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


@onboard_app.command("incidents")
def onboard_incidents_command(
    workspace: Path = typer.Option(..., "--workspace", help="An ingested workspace."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (the model)."
    ),
) -> None:
    """Map the workspace's incident history to classes and causes (incident_map.json)."""
    try:
        mapped, path = onboard_incidents(load_config_default(config), workspace)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(
        f"incident map: {len(mapped.classes)} classes; {mapped.coverage:.0%} of incidents "
        "have a cause the plugins know"
    )
    for c in mapped.classes:
        hours = f"{c.mean_hours_to_resolve:g}h" if c.mean_hours_to_resolve is not None else "?"
        typer.echo(
            f"  {c.share:>4.0%}  {c.cause or '(no cause)':<34} {c.label or '?':<22} "
            f"{len(c.incidents):>3} incidents, mean {hours} to resolve"
        )
    if mapped.unread:
        typer.echo(f"no answer for {len(mapped.unread)} incident(s): re-run to retry them")
    typer.echo(f"wrote {path}")


@onboard_app.command("requests")
def onboard_requests_command(
    workspace: Path = typer.Option(..., "--workspace", help="An ingested workspace."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (database, model)."
    ),
) -> None:
    """Plan the access this system needs; save the requests for review (requests.json)."""
    try:
        plan, saved, path = onboard_requests(load_config_default(config), workspace)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(
        f"{len(plan.requests)} request(s), together unlocking {plan.unlocked:.0%} of the "
        f"incident weight; saved {saved['new']} new, {saved['kept']} kept, "
        f"{saved['gone']} no longer needed"
    )
    for r in plan.requests:
        typer.echo(
            f"  {r.share:>4.0%}  {r.plugin}/{r.access} on {r.target}: {r.why} "
            f"[{request_id(*r.key)}]"
        )
    for g in plan.gaps:
        typer.echo(f"  gap: {g.class_id} ({g.share:.0%}): {g.why}")
    typer.echo(
        f"wrote {path}; decide at /ui/onboarding/requests, then bind an approved one's "
        "credential with `supportagent onboard bind <id>`"
    )


_BY = typer.Option("cli", "--by", help="Who is doing this, for the audit trail.")


@onboard_app.command("bind")
def onboard_bind_command(
    request: str = typer.Argument(..., help="The request's id (onboard requests prints it)."),
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (database, vault)."
    ),
    stdin: bool = typer.Option(
        False, "--stdin", help="Read the credential from standard input, not a prompt."
    ),
    by: str = _BY,
) -> None:
    """Prove a credential read-only against its contract's probes, then bind it (ADR-0019).

    The credential is the connection string the contract's verifier takes (an
    Oracle DSN, a MongoDB URI, a RabbitMQ management URL with the user in it).
    It is never echoed, and stored only once every probe passed.
    """
    secret = (
        sys.stdin.readline().strip()
        if stdin
        else typer.prompt("credential", hide_input=True).strip()
    )
    try:
        outcome = onboard_bind(load_config_default(config), request, secret, actor=by)
    except (ConfigError, DomainValidationError, NotFound) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    for p in outcome.probes:
        typer.echo(f"  {p.kind:<6} {p.probe:<40} {p.outcome}{f': {p.detail}' if p.detail else ''}")
    r = outcome.request
    if outcome.bound:
        typer.echo(f"bound: {r.plugin}/{r.access} on {r.target} is proven read-only")
        return
    typer.echo(f"refused, nothing stored; the request is open again: {outcome.why}", err=True)
    raise typer.Exit(1)


@onboard_app.command("unbind")
def onboard_unbind_command(
    request: str = typer.Argument(..., help="The request's id."),
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (database, vault)."
    ),
    by: str = _BY,
) -> None:
    """Take a bound credential out of the vault (the request stays approved)."""
    try:
        r = onboard_unbind(load_config_default(config), request, actor=by)
    except (ConfigError, DomainValidationError, NotFound) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"unbound {r.plugin}/{r.access} on {r.target}; it is approved, with no credential")


@onboard_app.command("verify")
def onboard_verify_command(
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (database, vault)."
    ),
    by: str = typer.Option("system", "--by", help="Who is doing this, for the audit trail."),
) -> None:
    """Run every bound credential's probes again. One that can write now is unbound."""
    try:
        outcomes = onboard_verify(load_config_default(config), actor=by)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    for o in outcomes:
        r = o.request
        verdict = "still read-only" if o.bound else f"unbound and reopened: {o.why}"
        typer.echo(f"  {r.plugin}/{r.access} on {r.target}: {verdict}")
    typer.echo(f"{len(outcomes)} bound credential(s) verified")
    if any(not o.bound for o in outcomes):
        raise typer.Exit(1)


@onboard_app.command("evidence")
def onboard_evidence_command(
    workspace: Path = typer.Option(..., "--workspace", help="An ingested workspace."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (the read-only tools)."
    ),
    recapture: bool = typer.Option(
        False, "--recapture", help="Read again incidents that already have evidence."
    ),
) -> None:
    """Capture each past incident's evidence from the read-only tools, as of the incident."""
    try:
        result = onboard_evidence(load_config_default(config), workspace, recapture=recapture)
    except (ConfigError, ExternalServiceError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if result is None:
        typer.echo(
            "no configured tool can read the past (logs, metrics, deploys or CI under "
            "tools:); the workspace's evidence is unchanged"
        )
        return
    typer.echo(result.line())
    typer.echo(f"wrote {workspace / 'sources' / 'evidence'}; next: supportagent onboard readiness")


@onboard_app.command("readiness")
def onboard_readiness_command(
    workspace: Path = typer.Option(..., "--workspace", help="An ingested workspace."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (database, triage model)."
    ),
) -> None:
    """Replay the history per incident class; save which classes are ready (readiness.json)."""
    try:
        report, path = onboard_readiness(load_config_default(config), workspace)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    for line in report.lines():
        typer.echo(line)
    typer.echo(f"wrote {path}; triage routes only ready classes from its next start")


@onboard_app.command("drift")
def onboard_drift_command(
    workspace: Path = typer.Option(..., "--workspace", help="An ingested workspace."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (the model)."
    ),
    accept: bool = typer.Option(
        False, "--accept", help="Take this run as the new baseline (after dealing with it)."
    ),
) -> None:
    """Re-run onboarding and say what changed since the accepted snapshot (drift.json)."""
    try:
        findings, accepted, path = onboard_drift(
            load_config_default(config), workspace, accept=accept
        )
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not findings:
        typer.echo("no drift" if not accepted or accept else "first run: accepted as the baseline")
    for f in findings:
        typer.echo(f"  {f.kind:<16} {f.subject}: {f.detail} -> {f.action}")
    if findings and not accepted:
        typer.echo("deal with these, then re-run with --accept")
    typer.echo(f"wrote {path}")


@onboard_app.command("run")
def onboard_run_command(
    system: Path = typer.Option(..., "--system", help="The system's system.yaml."),  # noqa: B008
    workspace: Path = typer.Option(..., "--workspace", help="The workspace (created if new)."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (database, models)."
    ),
) -> None:
    """Onboard a system end to end (ADR-0017): every step, resumable. Re-run to continue.

    Steps done on unchanged inputs are skipped; blocked and failed ones are
    retried. Ends with what's needed from you, ranked by the incident weight
    it holds back. Exit 0 when the system is ready to support, 1 when not yet.
    """
    try:
        status = onboard_run(load_config_default(config), system, workspace)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    for line in status.lines():
        typer.echo(line)
    typer.echo(f"wrote {workspace / AUTOPILOT_STATE}; re-run to continue once you've acted")
    raise typer.Exit(0 if status.ready else 1)


@onboard_app.command("status")
def onboard_status_command(
    workspace: Path = typer.Option(..., "--workspace", help="An onboarded workspace."),  # noqa: B008
) -> None:
    """Where onboarding stands, and what's needed from you (from the last run)."""
    path = workspace / AUTOPILOT_STATE
    if not path.is_file():
        typer.echo(f"not onboarded yet: run `supportagent onboard run --workspace {workspace}`")
        raise typer.Exit(1)
    raw = json.loads(path.read_text(encoding="utf-8"))
    typer.echo(f"{raw.get('system')}: {'ready' if raw.get('ready') else 'not ready'} to support")
    for step in raw.get("steps", []):
        on = f" (on {step['on']})" if step.get("on") else ""
        typer.echo(f"  {step['step']:<10} {step['state']}{on}: {step['detail']}")
    needs = raw.get("needs", [])
    typer.echo(f"  needed from you: {len(needs) or 'nothing'}")
    for need in needs:
        typer.echo(f"    {need['kind']:<8} {need['what']} -> {need['how']}")


@onboard_app.command("flows")
def onboard_flows_command(
    workspace: Path = typer.Option(..., "--workspace", help="An onboarded workspace."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (the model)."
    ),
) -> None:
    """Candidate flows from the ticket history, and a draft of each (ADR-0021 §2).

    Ranks the business objects tickets ask about by id, with the services their
    CIs belong to, and drafts an unreviewed flow for each one that names two or
    more services, ordered by how data moves through the graph. Writes
    flows.proposed/ in the workspace. Check a draft with `supportagent flow
    verify`, finish its TODOs, and mark it reviewed before tracing with it.
    """
    try:
        found, written = onboard_flows(load_config_default(config), workspace)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not found:
        typer.echo("no business objects asked about by id in the history: write a flow yourself")
    for c in found:
        services = ", ".join(c.services) or "no graph service"
        typer.echo(f"  {c.noun:<16} {c.pattern:<18} {c.tickets} ticket(s)  {services}")
    for path in written:
        typer.echo(f"wrote {path}: review it")


@onboard_app.command("catalog")
def onboard_catalog_command(
    workspace: Path = typer.Option(..., "--workspace", help="An onboarded workspace."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (its catalog, the model)."
    ),
) -> None:
    """Draft the service catalog onboarding implies (ADR-0020 §5), for you to review.

    Each serving component and each CI the history names becomes an entry with the
    stack, databases and namespace the graph gives it; services already in the
    configured catalog keep theirs. Writes catalog.proposed.yaml in the workspace,
    each entry marked with what's left (its paging integration).
    """
    try:
        entries, path = onboard_catalog(load_config_default(config), workspace)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    for item in entries:
        todo = f"  TODO: {'; '.join(item.todo)}" if item.todo else ""
        typer.echo(f"  {item.status:<4}  {item.entry.name:<28} {','.join(item.entry.stack)}{todo}")
    typer.echo(f"wrote {path}: review it, then copy it over your catalog")


@onboard_app.command("gaps")
def onboard_gaps_command(
    workspace: Path = typer.Option(..., "--workspace", help="An ingested workspace."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("supportagent.yaml"), "--config", help="supportagent.yaml (the model)."
    ),
) -> None:
    """What no plugin covers (ADR-0017): unclaimed repos, unmapped CMDB classes, and more."""
    try:
        gaps, path = onboard_gaps(load_config_default(config), workspace)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not gaps:
        typer.echo("nothing uncovered")
    for g in gaps:
        typer.echo(f"  {g.kind:<20} {g.subject}: {'; '.join(g.evidence)}")
        typer.echo(f"  {'':<20} -> {g.suggestion}")
    typer.echo(f"wrote {path}")


@onboard_app.command("watch")
def onboard_watch_command(
    system: Path = typer.Option(..., "--system", help="The system's system.yaml."),  # noqa: B008
    workspace: Path = typer.Option(..., "--workspace", help="The workspace."),  # noqa: B008
    config: Path = typer.Option(Path("supportagent.yaml"), "--config"),  # noqa: B008
    every_hours: float = typer.Option(24.0, "--every-hours", min=0.25, help="Hours between runs."),
    once: bool = typer.Option(False, "--once", help="One cycle, then exit (for cron)."),
) -> None:
    """Keep a system onboarded (ADR-0017): `onboard run` and a drift check, every N hours.

    Run it as its own process or container: a cycle calls the model and can take
    minutes, so it stays out of the triage worker. Drift findings go to the audit
    trail and the onboarding page; accept them with `onboard drift --accept`.
    """
    while True:
        try:
            status, findings = onboard_cycle(load_config_default(config), system, workspace)
        except ConfigError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from exc
        verdict = "ready" if status.ready else "not ready"
        typer.echo(
            f"{status.system}: {verdict}; {len(status.needs)} need(s); "
            f"{len(findings)} drift finding(s)"
        )
        if once:
            return
        time.sleep(every_hours * 3600)
