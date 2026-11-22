"""``supportagent init``: the local installer's setup wizard (``./install.sh`` runs it).

It asks for what only you know (the LLM, ServiceNow, optional Confluence and
PagerDuty, your sign-in), checks each credential as you type it, and writes
``local/``: the config, the catalog, the Keycloak realm and ``.env`` with every
secret (chmod 600). Run it again to change anything: earlier answers are the
defaults, and the generated secrets (database password, master key, sign-in
secrets) are kept, so existing data stays readable.

``install.sh`` runs it inside the SupportAgent image, so it sees the network
the containers see. ``localhost`` in a URL you type means your laptop; the
config gets ``host.docker.internal`` instead, which is how a container reaches
it. ``--answers FILE`` (JSON) skips the questions, for scripted installs.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

import typer

from supportagent.bootstrap import probe_confluence, probe_llm_models, probe_servicenow
from supportagent.config.loader import load_config
from supportagent.config.local_install import (
    DEMO_SERVICES,
    INTEGRATION_USER,
    Answers,
    ConfluenceAnswers,
    FrontierAnswers,
    LlmAnswers,
    LlmKind,
    OnboardAnswers,
    PagerDutyAnswers,
    ServiceAnswer,
    ServiceNowAnswers,
    SignalsAnswers,
    generated_secrets,
    parse_env,
    render_catalog,
    render_config,
    render_env,
    render_realm,
    render_system,
    service_name,
)
from supportagent.config.schema import is_floating_alias
from supportagent.domain.errors import ConfigError

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})  # noqa: S104  # nosec B104 — matched, not bound
_DOCKER_HOST = "host.docker.internal"
_MAX_CIS_SHOWN = 15
_DEFAULT_CIS = 5


def init(  # noqa: PLR0913, PLR0917 — one option per install setting
    out: Path = typer.Option(Path("local"), "--dir", help="Where to write the install files."),  # noqa: B008
    app_port: int = typer.Option(8800, "--app-port", help="SupportAgent's port on this machine."),
    keycloak_port: int = typer.Option(8880, "--keycloak-port", help="Keycloak's port."),
    answers_file: Path | None = typer.Option(  # noqa: B008
        None, "--answers", help="A JSON file of answers: no questions asked."
    ),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Check each credential."),
    in_container: bool = typer.Option(
        False, "--in-container", help="Set by install.sh: running in the image."
    ),
) -> None:
    """Ask the setup questions and write local/ (config, catalog, realm, .env)."""
    previous_env = _read(out / ".env")
    previous = json.loads(_read_text(out / "answers.json") or "{}")
    if answers_file is not None:
        answers = _from_file(answers_file, previous_env, app_port, keycloak_port)
    else:
        wizard = _Wizard(previous, previous_env, probe=probe, in_container=in_container)
        answers = wizard.run(app_port=app_port, keycloak_port=keycloak_port)
    _write(out, answers)


# --- the questions ----------------------------------------------------------------


class _Wizard:
    def __init__(
        self,
        previous: dict[str, Any],
        env: dict[str, str],
        *,
        probe: bool,
        in_container: bool,
    ) -> None:
        self.prev = previous
        self.env = env
        self.probe = probe
        self.in_container = in_container

    def run(self, *, app_port: int, keycloak_port: int) -> Answers:
        _banner(
            "SupportAgent setup",
            "A few questions, then everything runs in Docker on this machine.\n"
            "Press Enter to take the [default]. Secrets you type are not echoed and are\n"
            "saved only in local/.env (chmod 600).",
        )
        team = _ask("Your team or company name", self.prev.get("team_name", "My Team"))
        self.prev["team_name"] = team
        # A failed check offers "try again": the section is asked again, with what
        # was just typed as the defaults.
        llm = _retrying(self.llm)
        # ADR-0023 §5: the system and its signals first; a ticket system is optional.
        system = self.system(team)
        signals = self.signals()
        servicenow, services = _retrying(self.servicenow)
        onboard = self.ticket_history(team, system, servicenow)
        confluence = _retrying(self.confluence)
        pagerduty = _retrying(self.pagerduty)
        if pagerduty is not None:
            services = self.pagerduty_services(services)
        user, email, password = self.sign_in()
        return Answers(
            team_name=team,
            llm=llm,
            servicenow=servicenow,
            services=services,
            admin_user=user,
            admin_email=email,
            admin_password=password,
            app_port=app_port,
            keycloak_port=keycloak_port,
            confluence=confluence,
            pagerduty=pagerduty,
            onboard=onboard,
            signals=signals,
            previous=self.env,
        )

    # -- LLM ---------------------------------------------------------------------

    def llm(self) -> LlmAnswers:
        prev = self.prev.get("llm", {})
        _section(
            "1/7  Language model",
            "SupportAgent sends redacted ticket text to this model to classify incidents\n"
            "and check root-cause claims.",
        )
        kind = _choose(
            "Which model provider?",
            [
                ("anthropic", "Anthropic Claude (API key)"),
                ("openai", "OpenAI (API key)"),
                ("local", "A local OpenAI-compatible server: LM Studio, vLLM, llama.cpp"),
                ("ollama", "Ollama"),
            ],
            prev.get("kind", "anthropic"),
        )
        typed_url = None
        if kind in ("local", "ollama"):
            default = prev.get("typed_url") or (
                "http://localhost:1234/v1" if kind == "local" else "http://localhost:11434"
            )
            typed_url = _ask("Server URL (as you'd open it on this machine)", default)
            if kind == "local" and urlparse(typed_url).path in ("", "/"):
                typed_url = typed_url.rstrip("/") + "/v1"
        key = None
        if kind in ("anthropic", "openai"):
            key = self.secret(
                f"{'Anthropic' if kind == 'anthropic' else 'OpenAI'} API key", "LLM_API_KEY"
            )
        self.prev["llm"] = {**prev, "kind": kind, "typed_url": typed_url}
        container_url = _for_container(typed_url) if typed_url else None
        probe_url = container_url if self.in_container else typed_url

        def models() -> list[str]:
            return probe_llm_models(kind, base_url=probe_url, api_key=key)

        found: list[str] = self.check("Listing the models it offers", models, default=[])
        model = self.pick_model(kind, found, prev.get("model_id"))
        self.prev["llm"] = {"kind": kind, "typed_url": typed_url, "model_id": model}
        frontier = self.frontier(prev.get("onboarding")) if kind in ("local", "ollama") else None
        return LlmAnswers(
            kind=cast("LlmKind", kind),
            model_id=model,
            base_url=container_url,
            api_key=key,
            onboarding=frontier,
        )

    def frontier(self, prev: dict[str, Any] | None) -> FrontierAnswers | None:
        """ADR-0015 decision 3: onboarding reads a system's docs with a frontier model."""
        typer.echo(
            "\n  Onboarding a new system reads all of its docs and drafts a map of it. A\n"
            "  frontier model (Anthropic or OpenAI) does that much better than most local\n"
            "  ones; triage stays on your local model."
        )
        if not typer.confirm("  Add a frontier model for onboarding?", default=bool(prev)):
            return None
        kind = _choose(
            "Which one?",
            [("anthropic", "Anthropic Claude"), ("openai", "OpenAI")],
            (prev or {}).get("kind", "anthropic"),
        )
        vendor = "Anthropic" if kind == "anthropic" else "OpenAI"
        key = self.secret(f"{vendor} API key (for onboarding)", "ONBOARDING_LLM_API_KEY")

        def models() -> list[str]:
            return probe_llm_models(kind, base_url=None, api_key=key)

        found: list[str] = self.check("Listing the models it offers", models, default=[])
        model = self.pick_model(kind, found, (prev or {}).get("model_id"))
        self.prev["llm"]["onboarding"] = {"kind": kind, "model_id": model}
        frontier_kind = cast('Literal["anthropic", "openai"]', kind)
        return FrontierAnswers(kind=frontier_kind, model_id=model, api_key=key)

    def pick_model(self, kind: str, found: Sequence[str], previous: str | None) -> str:
        fallback = {
            "anthropic": "claude-sonnet-4-5-20250929",
            "openai": "gpt-4.1-2025-04-14",
        }.get(kind, "")
        pinned = [m for m in found if not is_floating_alias(m)]
        if pinned:
            preferred = previous if previous in pinned else _preferred(kind, pinned)
            shown = pinned[:_MAX_CIS_SHOWN]
            if preferred not in shown:
                shown = [preferred, *shown[: _MAX_CIS_SHOWN - 1]]
            return _choose("Which model?", [(m, m) for m in shown], preferred)
        if found:
            typer.echo(
                "  Every model id it lists floats (no date or version). SupportAgent needs a\n"
                "  pinned id, so evals stay comparable: type one with a version in it."
            )
        while True:
            model = _ask("Model id", previous or fallback or None)
            if not is_floating_alias(model):
                return model
            typer.echo("  That id floats (no date or version, or ends in 'latest'); pin one.")

    # -- ServiceNow ----------------------------------------------------------------

    def servicenow(self) -> tuple[ServiceNowAnswers, tuple[ServiceAnswer, ...]]:
        prev = self.prev.get("servicenow", {})
        _section(
            "4/7  Tickets",
            "SupportAgent starts in shadow mode: it reads incidents and records what it\n"
            "would do, and never writes to ServiceNow until you promote it. Without a\n"
            "ticket system, alerts are its intake.",
        )
        kind = _choose(
            "Which ticket system?",
            [
                ("instance", "My ServiceNow instance (URL and an integration user)"),
                ("demo", "The demo ServiceNow: no instance needed, sample incidents arrive"),
                ("none", "None: alerts only (from Alertmanager, Grafana or its own detectors)"),
            ],
            prev.get("kind") if prev.get("kind") in ("demo", "none") else "instance",
        )
        if kind == "none":
            self.prev["servicenow"] = {"kind": "none"}
            return ServiceNowAnswers(kind="none"), ()
        if kind == "demo":
            self.prev["servicenow"] = {"kind": "demo"}
            return ServiceNowAnswers(kind="demo"), DEMO_SERVICES
        url = _instance_url(_ask("Instance URL or name (e.g. dev12345)", prev.get("instance_url")))
        auth = _choose(
            "How does SupportAgent sign in?",
            [("basic", "User name and password"), ("oauth", "OAuth client credentials")],
            prev.get("auth", "basic"),
        )
        integration_user = _ask(
            "Integration user name (role itil, nothing broader)",
            prev.get("integration_user", INTEGRATION_USER),
        )
        if auth == "basic":
            sn = ServiceNowAnswers(
                kind="basic",
                instance_url=url,
                user=integration_user,
                password=self.secret(f"Password for {integration_user}", "SN_PASSWORD"),
                integration_user=integration_user,
            )
        else:
            sn = ServiceNowAnswers(
                kind="oauth",
                instance_url=url,
                client_id=self.secret("OAuth client id", "SN_CLIENT_ID", hide=False),
                client_secret=self.secret("OAuth client secret", "SN_CLIENT_SECRET"),
                integration_user=integration_user,
            )

        def cis() -> list[tuple[str, str, int]]:
            return probe_servicenow(
                sn.instance_url,
                integration_user=sn.integration_user,
                user=sn.user,
                password=sn.password,
                client_id=sn.client_id,
                client_secret=sn.client_secret,
            )

        self.prev["servicenow"] = {
            "kind": auth,
            "instance_url": url,
            "auth": auth,
            "integration_user": integration_user,
        }
        none: list[tuple[str, str, int]] = []
        found = self.check("Signing in and reading recent incidents", cis, default=none)
        services = self.pick_services(found)
        typer.echo(
            "  Sync also reads each incident's history: the integration user needs read\n"
            "  access to sys_audit and sys_journal_field. `./install.sh doctor` checks it;\n"
            "  docs/quickstart.md shows the ACL."
        )
        return sn, services

    def pick_services(self, found: Sequence[tuple[str, str, int]]) -> tuple[ServiceAnswer, ...]:
        typer.echo(
            "\n  SupportAgent triages incidents on the services it knows. A service is one\n"
            "  or more configuration items (CIs); incidents on other CIs are skipped."
        )
        if found:
            shown = list(found[:_MAX_CIS_SHOWN])
            for i, (name, _, n) in enumerate(shown, 1):
                typer.echo(f"   {i:>2}. {name}  ({n} recent incident{'s' if n != 1 else ''})")
            default = ",".join(str(i) for i in range(1, min(_DEFAULT_CIS, len(shown)) + 1))
            while True:
                raw = _ask("Watch which? Numbers like 1,3 or 'all'", default)
                picked = _numbers(raw, len(shown))
                if picked:
                    break
                typer.echo("  Give numbers from the list, separated by commas.")
            return tuple(
                ServiceAnswer(service_name(shown[i][0]), (shown[i][0],), (shown[i][1],))
                for i in picked
            )
        typer.echo("  No recent incidents with a CI were found. Name the CIs to watch instead.")
        out: list[ServiceAnswer] = []
        while True:
            ci = _ask("CI name (as on the incident's Configuration item; Enter when done)", "")
            if not ci:
                if out:
                    return tuple(out)
                typer.echo("  Add at least one; you can change them later in local/catalog.yaml.")
                continue
            out.append(ServiceAnswer(service_name(ci), (ci,), ()))

    # -- optional sources ---------------------------------------------------------------

    def confluence(self) -> ConfluenceAnswers | None:
        prev = self.prev.get("confluence") or {}
        _section("5/7  Confluence (optional)", "Team docs RCA can cite as guidance, read-only.")
        if not typer.confirm("  Connect a Confluence space?", default=bool(prev)):
            self.prev["confluence"] = None
            return None
        typed = _ask(
            "Space URL (paste it from the browser) or site URL",
            prev.get("base_url", "https://<site>.atlassian.net/wiki"),
        )
        site, key_in_url = _confluence_url(typed)
        key = _ask("Space key", key_in_url or prev.get("space_key"))
        user = _ask("Atlassian account email (the token's owner)", prev.get("user"))
        self.prev["confluence"] = {"base_url": site, "space_key": key, "user": user}
        token = self.secret(
            "API token (id.atlassian.com > Security > API tokens)", "CONFLUENCE_API_TOKEN"
        )

        def space() -> str:
            return probe_confluence(site, space_key=key, user=user, token=token)

        name = self.check("Reading the space", space, default="")
        if name:
            typer.echo(f"  Space: {name}")
        self.prev["confluence"] = {"base_url": site, "space_key": key, "user": user}
        return ConfluenceAnswers(base_url=site, space_key=key, user=user, token=token)

    def pagerduty(self) -> PagerDutyAnswers | None:
        prev = self.prev.get("pagerduty") or {}
        _section(
            "6/7  PagerDuty (optional)",
            "Lets SupportAgent attach its findings to an open page. It never creates one.",
        )
        if not typer.confirm("  Connect PagerDuty?", default=bool(prev)):
            self.prev["pagerduty"] = None
            return None
        token = self.secret("PagerDuty REST API key", "PAGERDUTY_TOKEN")
        email = _ask("From email for notes (a PagerDuty user)", prev.get("from_email"))
        self.prev["pagerduty"] = {"from_email": email}
        return PagerDutyAnswers(token=token, from_email=email)

    def pagerduty_services(self, services: tuple[ServiceAnswer, ...]) -> tuple[ServiceAnswer, ...]:
        typer.echo(
            "  Each watched service's PagerDuty service id (in its URL: .../services/PXXXXXX)."
        )
        known = {s["name"]: s.get("pagerduty_service") for s in self.prev.get("services", [])}
        out = []
        for svc in services:
            pid = _ask(f"PagerDuty service id for {svc.name}", known.get(svc.name) or svc.name)
            out.append(replace(svc, pagerduty_service=pid))
        return tuple(out)

    # -- onboarding (ADR-0017) --------------------------------------------------

    def system(self, team: str) -> OnboardAnswers | None:
        """ADR-0023 §1: the system to learn, by where its code is."""
        prev = self.prev.get("onboard") or {}
        _section(
            "2/7  The system",
            "Which system SupportAgent learns. After setup it reads the system's code itself\n"
            "(read-only), builds its picture of the system and lists anything it needs from you.",
        )
        name = _ask("System name", prev.get("system_name", team))
        local = _ask(
            "A checkout on this machine, mounted read-only (absolute path; Enter for none)",
            prev.get("local_dir") or "",
        )
        repos = _ask(
            "GitHub repositories, like github:acme/* (comma-separated; Enter for none)",
            ",".join(prev.get("repos", [])),
        )
        repo_list = tuple(r.strip() for r in repos.split(",") if r.strip())
        token = (
            self.secret("GitHub token (read-only, fine-grained)", "GITHUB_TOKEN")
            if repo_list
            else None
        )
        catalog = _ask(
            "Plugin catalogue: a directory, or a git URL (Enter for none)",
            prev.get("catalog") or "",
        )
        ref = path = None
        if (catalog and "://" in catalog) or catalog.startswith("git@"):
            ref = _ask(
                "The catalogue's commit id (a full commit, never a branch)", prev.get("catalog_ref")
            )
            path = _ask(
                "Its directory in the repository (Enter for the root)",
                prev.get("catalog_path") or "",
            )
        answers = OnboardAnswers(
            system_id=service_name(name),
            system_name=name,
            local_dir=local.strip() or None,
            repos=repo_list,
            github_token=token,
            catalog=catalog or None,
            catalog_ref=ref or None,
            catalog_path=path or "",
        )
        self.prev["onboard"] = _public_onboard(answers)
        return answers if (answers.local_dir or answers.repos) else None

    def signals(self) -> SignalsAnswers | None:
        """ADR-0023 §4-5: what the system measures, and whether alerts are sent here."""
        prev = self.prev.get("signals") or {}
        _section(
            "3/7  Signals (optional)",
            "Prometheus and Loki, read-only: SupportAgent measures the system's normal from\n"
            "their history, proposes detectors and evaluates the ones you enable.",
        )
        prom = _ask("Prometheus URL (Enter for none)", prev.get("prometheus_url") or "")
        loki = _ask("Loki URL (Enter for none)", prev.get("loki_url") or "")
        alerts = typer.confirm(
            "  Will an Alertmanager or Grafana send its alerts here?",
            default=bool(prev.get("alertmanager")),
        )
        answers = SignalsAnswers(
            prometheus_url=_for_container(prom) if prom.strip() else None,
            loki_url=_for_container(loki) if loki.strip() else None,
            alertmanager=alerts,
        )
        self.prev["signals"] = asdict(answers)
        if not (answers.prometheus_url or answers.loki_url or answers.alertmanager):
            return None
        return answers

    def ticket_history(
        self, team: str, system: OnboardAnswers | None, servicenow: ServiceNowAnswers
    ) -> OnboardAnswers | None:
        """With a real ticket system, onboarding can also read the system's past incidents."""
        if servicenow.kind not in ("basic", "oauth"):
            return system
        prev = self.prev.get("onboard") or {}
        if not typer.confirm(
            "  Read the system's past incidents for onboarding?",
            default=bool(prev.get("assignment_groups")),
        ):
            return system
        groups = _ask(
            "ServiceNow assignment groups that handle its incidents (comma-separated)",
            ",".join(prev.get("assignment_groups", [])) or None,
        )
        business = _ask(
            "ServiceNow business service (Enter for none)", prev.get("business_service") or ""
        )
        base = system or OnboardAnswers(
            system_id=service_name(prev.get("system_name", team)),
            system_name=prev.get("system_name", team),
        )
        answers = replace(
            base,
            assignment_groups=tuple(g.strip() for g in groups.split(",") if g.strip()),
            business_service=business or None,
        )
        self.prev["onboard"] = _public_onboard(answers)
        return answers

    def sign_in(self) -> tuple[str, str, str]:
        prev = self.prev.get("admin", {})
        _section(
            "7/7  Your sign-in",
            "A bundled Keycloak handles sign-in. The first person to sign in is the admin.",
        )
        user = _ask("User name", prev.get("user", "admin"))
        email = _ask("Email", prev.get("email", f"{user}@example.com"))
        password = typer.prompt(
            "  Password (Enter to generate one)",
            default="",
            hide_input=True,
            show_default=False,
        )
        if not password:
            password = self.env.get("SA_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        self.prev["admin"] = {"user": user, "email": email}
        return user, email, password

    # -- helpers -------------------------------------------------------------------------

    def secret(self, label: str, env_key: str, *, hide: bool = True) -> str:
        current = self.env.get(env_key)
        hint = " (Enter keeps the current one)" if current else ""
        while True:
            value: str = typer.prompt(
                f"  {label}{hint}", default="", hide_input=hide, show_default=False
            )
            if value or current:
                return value or current or ""
            typer.echo("  Required.")

    def check[R](self, what: str, fn: Callable[[], R], *, default: R) -> R:
        if not self.probe:
            return default
        while True:
            typer.echo(f"  {what}...", nl=False)
            try:
                result = fn()
            except ConfigError as exc:
                typer.echo(" failed")
                typer.echo(f"  ✗ {_reason(exc)}")
                choice = _choose(
                    "What now?",
                    [("retry", "Try again"), ("skip", "Continue anyway (doctor will flag it)")],
                    "retry",
                )
                if choice == "skip":
                    return default
                raise _AskAgainError from exc
            typer.echo(" ok")
            return result


class _AskAgainError(Exception):
    """Re-ask the section after a failed check."""


def _retrying[R](step: Callable[[], R]) -> R:
    while True:
        try:
            return step()
        except _AskAgainError:
            continue


# --- non-interactive ----------------------------------------------------------------


def _from_file(path: Path, env: dict[str, str], app_port: int, keycloak_port: int) -> Answers:
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        sn = raw.get("servicenow") or {"kind": "demo"}
        signals = raw.get("signals")
        services = (
            DEMO_SERVICES
            if sn.get("kind", "demo") == "demo"
            else tuple(
                ServiceAnswer(
                    s["name"],
                    tuple(s.get("resources", ())),
                    tuple(s.get("cmdb_ci", ())),
                    s.get("pagerduty_service"),
                )
                for s in (raw.get("services", []) if sn.get("kind") == "none" else raw["services"])
            )
        )
        llm = raw["llm"]
        frontier = llm.get("onboarding")
        return Answers(
            team_name=raw.get("team_name", "My Team"),
            llm=LlmAnswers(
                kind=llm["kind"],
                model_id=llm["model_id"],
                base_url=_for_container(llm["base_url"]) if llm.get("base_url") else None,
                api_key=llm.get("api_key"),
                onboarding=FrontierAnswers(**frontier) if frontier else None,
            ),
            servicenow=ServiceNowAnswers(**sn),
            services=services,
            admin_user=raw.get("admin_user", "admin"),
            admin_email=raw.get("admin_email", "admin@example.com"),
            admin_password=raw.get("admin_password") or secrets.token_urlsafe(12),
            app_port=raw.get("app_port", app_port),
            keycloak_port=raw.get("keycloak_port", keycloak_port),
            confluence=ConfluenceAnswers(**raw["confluence"]) if raw.get("confluence") else None,
            pagerduty=PagerDutyAnswers(**raw["pagerduty"]) if raw.get("pagerduty") else None,
            onboard=_onboard_from(raw.get("onboard")),
            signals=(
                SignalsAnswers(
                    prometheus_url=_for_container(signals["prometheus_url"])
                    if signals.get("prometheus_url")
                    else None,
                    loki_url=_for_container(signals["loki_url"])
                    if signals.get("loki_url")
                    else None,
                    alertmanager=bool(signals.get("alertmanager")),
                )
                if signals
                else None
            ),
            previous=env,
        )
    except (KeyError, TypeError) as exc:
        typer.echo(f"error: {path}: missing or wrong answer: {exc}", err=True)
        raise typer.Exit(2) from exc


# --- writing local/ ---------------------------------------------------------------------


def _write(out: Path, answers: Answers) -> None:
    generated = generated_secrets(answers.previous)
    try:
        env_text = (
            render_env(answers, generated) + f"SA_ADMIN_PASSWORD='{answers.admin_password}'\n"
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    out.mkdir(parents=True, exist_ok=True)
    (out / "keycloak").mkdir(exist_ok=True)
    (out / "evals").mkdir(exist_ok=True)
    env_path = out / ".env"
    env_path.write_text(env_text, encoding="utf-8")
    env_path.chmod(0o600)
    (out / "supportagent.yaml").write_text(render_config(answers), encoding="utf-8")
    (out / "catalog.yaml").write_text(render_catalog(answers), encoding="utf-8")
    system = render_system(answers)
    if system is not None:  # an earlier system.yaml is kept when this run onboards nothing
        (out / "system.yaml").write_text(system, encoding="utf-8")
    # Keycloak's container user reads the realm on first start; local/ is git-ignored.
    realm = out / "keycloak" / "supportagent-realm.json"
    realm.write_text(render_realm(answers, generated), encoding="utf-8")
    realm.chmod(0o644)
    (out / "answers.json").write_text(json.dumps(_public(answers), indent=2) + "\n", "utf-8")
    try:
        load_config(out / "supportagent.yaml", parse_env(env_text))
    except ConfigError as exc:
        typer.echo(f"error: the generated config doesn't validate: {_reason(exc)}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"\n✓ Wrote {out}/: supportagent.yaml, catalog.yaml, .env (secrets), keycloak/")


def _public(answers: Answers) -> dict[str, Any]:
    """What a re-run offers as defaults: no secrets."""
    llm = asdict(answers.llm)
    llm.pop("api_key")
    if llm.get("onboarding"):
        llm["onboarding"].pop("api_key")
    sn = asdict(answers.servicenow)
    for key in ("password", "client_id", "client_secret"):
        sn.pop(key)
    return {
        "team_name": answers.team_name,
        "llm": {**llm, "typed_url": llm.get("base_url")},
        "servicenow": {**sn, "auth": sn["kind"]},
        "services": [asdict(s) for s in answers.services],
        "confluence": (
            {k: v for k, v in asdict(answers.confluence).items() if k != "token"}
            if answers.confluence
            else None
        ),
        "pagerduty": {"from_email": answers.pagerduty.from_email} if answers.pagerduty else None,
        "onboard": _public_onboard(answers.onboard) if answers.onboard else None,
        "signals": asdict(answers.signals) if answers.signals else None,
        "admin": {"user": answers.admin_user, "email": answers.admin_email},
    }


def _public_onboard(onboard: OnboardAnswers) -> dict[str, Any]:
    out = asdict(onboard)
    out.pop("github_token")
    return out


def _onboard_from(raw: dict[str, Any] | None) -> OnboardAnswers | None:
    """``onboard`` in an answers file: groups and repos as lists, the token included."""
    if not raw:
        return None
    name = raw.get("system_name") or raw["system_id"]
    return OnboardAnswers(
        system_id=raw.get("system_id") or service_name(name),
        system_name=name,
        assignment_groups=tuple(raw.get("assignment_groups", ())),
        local_dir=raw.get("local_dir"),
        business_service=raw.get("business_service"),
        repos=tuple(raw.get("repos", ())),
        github_token=raw.get("github_token"),
        catalog=raw.get("catalog"),
        catalog_ref=raw.get("catalog_ref"),
        catalog_path=raw.get("catalog_path", ""),
    )


# --- small helpers ---------------------------------------------------------------------


def _banner(title: str, text: str) -> None:
    typer.echo(f"\n{title}\n{'=' * len(title)}\n{text}")


def _section(title: str, text: str) -> None:
    typer.echo(f"\n{title}\n{'-' * len(title)}\n{text}")


def _ask(label: str, default: str | None) -> str:
    value: str = (
        typer.prompt(f"  {label}", default=default)
        if default is not None
        else (typer.prompt(f"  {label}"))
    )
    return value.strip()


def _choose(question: str, options: Sequence[tuple[str, str]], default: str) -> str:
    typer.echo(f"  {question}")
    keys = [k for k, _ in options]
    for i, (_, label) in enumerate(options, 1):
        typer.echo(f"    {i}. {label}")
    start = keys.index(default) + 1 if default in keys else 1
    while True:
        raw = typer.prompt("  Choose", default=str(start)).strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return keys[int(raw) - 1]
        typer.echo(f"  Pick a number from 1 to {len(options)}.")


def _numbers(raw: str, size: int) -> list[int]:
    if raw.strip().lower() == "all":
        return list(range(size))
    picked: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part.isdigit() or not 1 <= int(part) <= size:
            return []
        if int(part) - 1 not in picked:
            picked.append(int(part) - 1)
    return picked


def _preferred(kind: str, models: Sequence[str]) -> str:
    hints = {"anthropic": ("sonnet",), "openai": ("gpt-4.1", "gpt-4o")}.get(kind, ())
    for hint in hints:
        for model in models:
            if hint in model:
                return model
    return models[0]


def _for_container(url: str) -> str:
    """A URL on this machine as a container reaches it (``host.docker.internal``)."""
    parsed = urlparse(url)
    if parsed.hostname in _LOCAL_HOSTS:
        netloc = _DOCKER_HOST + (f":{parsed.port}" if parsed.port else "")
        return parsed._replace(netloc=netloc).geturl()
    return url


def _instance_url(raw: str) -> str:
    """``dev12345`` / ``acme.service-now.com`` / a full URL -> ``https://…`` (no path)."""
    value = raw.strip().rstrip("/")
    if re.fullmatch(r"[a-z0-9-]+", value, flags=re.IGNORECASE):
        value = f"{value}.service-now.com"
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    return f"{parsed.scheme}://{parsed.netloc}"


def _confluence_url(raw: str) -> tuple[str, str | None]:
    """A pasted space URL -> (site ``https://x.atlassian.net/wiki``, the space key)."""
    parsed = urlparse(raw.strip() if "://" in raw else f"https://{raw.strip()}")
    site = f"{parsed.scheme}://{parsed.netloc}/wiki"
    match = re.search(r"/spaces/([^/?#]+)", parsed.path)
    return site, match.group(1) if match else None


def _reason(exc: ConfigError) -> str:
    errors = exc.errors() if hasattr(exc, "errors") else []
    msg = str(errors[0]["msg"]) if errors else str(exc)
    return msg.removeprefix("Value error, ")


def _read(path: Path) -> dict[str, str]:
    text = _read_text(path)
    return parse_env(text) if text else {}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
