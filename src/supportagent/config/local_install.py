"""Local install (``./install.sh``): answers -> the files under ``local/``.

``supportagent init`` asks the questions; this module turns its answers into:

- ``local/supportagent.yaml``: the config, secrets referenced by ``*_env``;
- ``local/.env``: every secret and port, read by Docker Compose (chmod 600);
- ``local/catalog.yaml``: the services to watch;
- ``local/keycloak/supportagent-realm.json``: the sign-in realm Keycloak
  imports on first start (client, redirect URL, the first user).

Every path in the config is relative to ``/app`` in the container, where
Compose mounts the repo's data directories read-only and ``local/`` read-write.
Pure: no prompts, no network, no environment.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

LlmKind = Literal["anthropic", "openai", "local", "ollama"]

#: The Compose service names the config points at (``docker/local/compose.yaml``).
DEMO_SN_URL = "http://demo-servicenow:8081"
KEYCLOAK_BACKCHANNEL = "http://keycloak:8080"
REALM = "supportagent"
CLIENT_ID = "supportagent"
INTEGRATION_USER = "svc_supportagent"

#: Generated once and kept on every re-run, so the database and Keycloak stay valid.
GENERATED = (
    "POSTGRES_PASSWORD",
    "SA_MASTER_KEY_ENV",
    "SA_SESSION_SECRET",
    "KEYCLOAK_ADMIN_PASSWORD",
    "OIDC_CLIENT_SECRET",
    "SUPPORTAGENT_ALERTMANAGER_TOKEN",
)
#: Where the app container sees the system's code (``SA_SYSTEM_DIR``), read-only.
SYSTEM_MOUNT = "/mnt/system"
#: What onboarding reads from a checkout on this machine (dependencies and
#: secret-named files are never read, whatever these match).
LOCAL_PATHS: tuple[str, ...] = (
    "**/*.py",
    "**/*compose*.y*ml",
    "**/Dockerfile*",
    "**/requirements*.txt",
    "**/pyproject.toml",
    "**/package.json",
    "**/pom.xml",
    "**/go.mod",
    "**/*.sql",
    "**/*.md",
)


@dataclass(frozen=True, slots=True)
class FrontierAnswers:
    """ADR-0015 decision 3: the frontier model onboarding uses when triage's is local."""

    kind: Literal["anthropic", "openai"]
    model_id: str
    api_key: str


@dataclass(frozen=True, slots=True)
class LlmAnswers:
    kind: LlmKind
    model_id: str
    #: The server as the containers reach it (``localhost`` already rewritten).
    base_url: str | None = None
    api_key: str | None = None
    onboarding: FrontierAnswers | None = None


@dataclass(frozen=True, slots=True)
class ServiceNowAnswers:
    """``demo`` uses the bundled demo instance; ``basic``/``oauth`` a real one; ``none``:
    no ticket system, so alerts are the only intake (ADR-0022, ADR-0023 §5)."""

    kind: Literal["demo", "basic", "oauth", "none"]
    instance_url: str = DEMO_SN_URL
    user: str | None = None
    password: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    integration_user: str = INTEGRATION_USER


@dataclass(frozen=True, slots=True)
class ServiceAnswer:
    name: str
    #: CI names (``cmdb_ci.name``) and sys_ids (``cmdb_ci``) that map to it.
    resources: tuple[str, ...] = ()
    cmdb_ci: tuple[str, ...] = ()
    #: The PagerDuty service id its pages live under; the name when not given.
    pagerduty_service: str | None = None


@dataclass(frozen=True, slots=True)
class ConfluenceAnswers:
    base_url: str
    space_key: str
    user: str
    token: str


@dataclass(frozen=True, slots=True)
class PagerDutyAnswers:
    token: str
    from_email: str


@dataclass(frozen=True, slots=True)
class SignalsAnswers:
    """ADR-0023 §5: what the system measures, and whether its alerting sends alerts here."""

    prometheus_url: str | None = None
    loki_url: str | None = None
    #: An Alertmanager (or Grafana) posts alerts to the ``alertmanager`` webhook.
    alertmanager: bool = False


@dataclass(frozen=True, slots=True)
class OnboardAnswers:
    """The system ``onboard run`` reads (ADR-0015 §1), written as ``local/system.yaml``."""

    system_id: str
    system_name: str
    #: The ticket system's groups that handle its incidents (none: no ticket history).
    assignment_groups: tuple[str, ...] = ()
    business_service: str | None = None
    #: ADR-0023 §1: a checkout on this machine, mounted read-only at ``SYSTEM_MOUNT``.
    local_dir: str | None = None
    #: ``github:<owner>/<name glob>`` each.
    repos: tuple[str, ...] = ()
    github_token: str | None = field(default=None, repr=False)
    #: A catalogue directory, or a git URL with its commit (``catalog_ref``).
    catalog: str | None = None
    catalog_ref: str | None = None
    catalog_path: str = ""


#: The files onboarding reads from each repository (config that names the stack).
REPO_PATHS: tuple[str, ...] = (
    "**/pom.xml",
    "**/build.gradle*",
    "**/application*.yml",
    "**/application*.properties",
    "**/package.json",
    "**/requirements*.txt",
    "**/pyproject.toml",
    "**/go.mod",
    "**/Dockerfile",
    "**/docker-compose*.yml",
    "**/helm/**",
    "**/k8s/**",
    "**/*.env.example",
)


@dataclass(frozen=True, slots=True)
class Answers:
    team_name: str
    llm: LlmAnswers
    servicenow: ServiceNowAnswers
    services: tuple[ServiceAnswer, ...]
    admin_user: str
    admin_email: str
    admin_password: str
    app_port: int = 8800
    keycloak_port: int = 8880
    confluence: ConfluenceAnswers | None = None
    pagerduty: PagerDutyAnswers | None = None
    #: ADR-0017: the system to onboard, and where plugins come from (optional).
    onboard: OnboardAnswers | None = None
    #: ADR-0023 §5: Prometheus, Loki and alerts (optional).
    signals: SignalsAnswers | None = None
    #: Values from an earlier run (``local/.env``): generated secrets are kept.
    previous: Mapping[str, str] = field(default_factory=dict)

    @property
    def tenant_id(self) -> str:
        return tenant_id(self.team_name)


#: The demo catalog: the services the demo ServiceNow's incidents name.
DEMO_SERVICES: tuple[ServiceAnswer, ...] = (
    ServiceAnswer("nightly-sync", ("nightly-sync",), ("CI1001",)),
    ServiceAnswer("core-db", ("core-db",), ("CI2002",)),
    ServiceAnswer("payroll-api", ("payroll-api",), ("CI3003",)),
)


def tenant_id(name: str) -> str:
    """A ``tenant.id`` (``^[a-z0-9-]{2,32}$``) from a display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-")
    return slug if len(slug) >= 2 else "local"  # noqa: PLR2004 — the pattern's minimum


def service_name(label: str) -> str:
    """A catalog service name (``^[a-z0-9][a-z0-9-]{1,62}$``) from a CI name."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:63].strip("-")
    return slug if len(slug) >= 2 else f"svc-{slug or 'x'}"  # noqa: PLR2004


def generated_secrets(previous: Mapping[str, str]) -> dict[str, str]:
    """The install's own secrets: kept from ``previous`` when present, else new."""
    fresh = {
        "POSTGRES_PASSWORD": secrets.token_hex(16),
        "SA_MASTER_KEY_ENV": secrets.token_hex(32),
        "SA_SESSION_SECRET": secrets.token_urlsafe(48),
        "KEYCLOAK_ADMIN_PASSWORD": secrets.token_urlsafe(18),
        "OIDC_CLIENT_SECRET": secrets.token_urlsafe(32),
        "SUPPORTAGENT_ALERTMANAGER_TOKEN": secrets.token_urlsafe(32),
    }
    return {k: previous.get(k) or v for k, v in fresh.items()}


def render_env(answers: Answers, generated: Mapping[str, str]) -> str:
    """``local/.env``: Compose reads it (``--env-file``); single-quoted, so literal."""
    values: dict[str, str] = {
        "COMPOSE_PROJECT_NAME": "supportagent-local",
        "COMPOSE_PROFILES": "demo" if answers.servicenow.kind == "demo" else "",
        "SA_APP_PORT": str(answers.app_port),
        "SA_KEYCLOAK_PORT": str(answers.keycloak_port),
        "SA_ADMIN_USER": answers.admin_user,
        **generated,
        "SUPPORTAGENT_DB_URL": (
            "postgresql+psycopg://supportagent:"
            f"{generated['POSTGRES_PASSWORD']}@postgres:5432/supportagent"
        ),
        "OIDC_CLIENT_ID": CLIENT_ID,
    }
    if answers.llm.kind in ("anthropic", "openai", "local"):
        values["LLM_API_KEY"] = answers.llm.api_key or "local"
    if answers.llm.onboarding is not None:
        values["ONBOARDING_LLM_API_KEY"] = answers.llm.onboarding.api_key
    sn = answers.servicenow
    if sn.kind == "oauth":
        values["SN_CLIENT_ID"] = sn.client_id or ""
        values["SN_CLIENT_SECRET"] = sn.client_secret or ""
    elif sn.kind != "none":
        values["SN_USER"] = sn.user or INTEGRATION_USER
        values["SN_PASSWORD"] = sn.password or "demo"
    if answers.confluence is not None:
        values["CONFLUENCE_USER"] = answers.confluence.user
        values["CONFLUENCE_API_TOKEN"] = answers.confluence.token
    if answers.pagerduty is not None:
        values["PAGERDUTY_TOKEN"] = answers.pagerduty.token
    if answers.onboard is not None and answers.onboard.github_token:
        values["GITHUB_TOKEN"] = answers.onboard.github_token
    if answers.onboard is not None and answers.onboard.local_dir:
        values["SA_SYSTEM_DIR"] = answers.onboard.local_dir
    lines = [
        "# SupportAgent local install: written by `supportagent init` (./install.sh).",
        "# Secrets live only here. Keep it private (chmod 600) and never commit it.",
        "# Re-run ./install.sh reconfigure to change a value.",
    ]
    lines += [f"{key}={_env_quote(key, value)}" for key, value in values.items()]
    return "\n".join(lines) + "\n"


def parse_env(text: str) -> dict[str, str]:
    """Read back a ``local/.env`` written by ``render_env`` (single-quoted values)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if len(value) >= 2 and value[0] == value[-1] == "'":  # noqa: PLR2004
            value = value[1:-1]
        out[key.strip()] = value
    return out


def render_config(answers: Answers) -> str:
    """``local/supportagent.yaml``: shadow mode, every secret by ``*_env``."""
    q = json.dumps
    llm = answers.llm
    local_model = llm.kind in ("local", "ollama")
    timeout, max_tokens = (90, 4000) if local_model else (30, 1000)
    provider = {"anthropic": "anthropic", "openai": "openai", "local": "openai"}.get(
        llm.kind, "ollama"
    )
    slot = f"{{model_id: {q(llm.model_id)}, timeout_s: {timeout}, max_output_tokens: {max_tokens}}}"
    # Onboarding's calls read whole components and pages: a local model that reasons
    # before answering needs far longer than triage's (a frontier model overrides these).
    long_slot = f"{{model_id: {q(llm.model_id)}, timeout_s: 300, max_output_tokens: 16000}}"
    sn = answers.servicenow
    if sn.kind == "oauth":
        sn_auth = (
            "    kind: oauth_client_credentials\n"
            "    client_id_env: SN_CLIENT_ID\n"
            "    client_secret_env: SN_CLIENT_SECRET"
        )
    else:
        sn_auth = "    kind: basic\n    user_env: SN_USER\n    password_env: SN_PASSWORD"
    out = [
        "# SupportAgent local install: written by `supportagent init` (./install.sh).",
        "# Re-run ./install.sh reconfigure to regenerate it; hand edits are kept only",
        "# until then. Secrets are never here: each *_env names a variable in local/.env.",
        "# The full reference is supportagent.example.yaml at the repo root.",
        "config_version: 1",
        "tenant:",
        f"  id: {q(answers.tenant_id)}",
        f"  display_name: {q(answers.team_name)}",
        "# Shadow mode: the agent reads tickets and records what it would do; it never",
        "# writes to ServiceNow. Promotion to assisted is a reviewed decision (ADR-0008).",
        "mode: shadow",
        "database:",
        "  url_env: SUPPORTAGENT_DB_URL",
    ]
    if sn.kind == "none":
        out.append("# No ticket system: alerts are the intake (ADR-0022). Shadow mode only.")
    else:
        out += [
            "servicenow:",
            f"  instance_url: {q(sn.instance_url)}"
            + ("   # the bundled demo instance" if sn.kind == "demo" else ""),
            "  auth:",
            sn_auth,
            f"  integration_user: {q(sn.integration_user)}",
            "  tables: [incident]",
            "  poll_interval_s: 60",
            "  overlap_s: 120",
            "  page_size: 200",
            "  qps_limit: 5",
            '  resolved_states: ["6", "7", "8"]',
            "  classification_map: {}",
        ]
    out += _signals_block(answers)
    out += [
        "identity:                            # the bundled Keycloak (docker/local/compose.yaml)",
        f"  oidc_issuer: {q(f'http://localhost:{answers.keycloak_port}/realms/{REALM}')}",
        f"  backchannel_url: {q(KEYCLOAK_BACKCHANNEL)}   # how the app container reaches it",
        "  client_id_env: OIDC_CLIENT_ID",
        "  client_secret_env: OIDC_CLIENT_SECRET",
        f"  redirect_url: {q(f'http://localhost:{answers.app_port}/auth/callback')}",
        "  session_secret_env: SA_SESSION_SECRET",
        "llm:",
        f"  provider: {provider}",
    ]
    out.append("  api_key_env: LLM_API_KEY" if provider != "ollama" else "  api_key_env: null")
    out.append(f"  base_url: {q(llm.base_url) if llm.base_url else 'null'}")
    out += [
        "  slots:",
        f"    classify: {slot}",
        f"    propose_hypotheses: {slot}",
        f"    entail: {slot}",
        *(
            [
                f"    summarize_component: {long_slot}",
                f"    extract_graph: {long_slot}",
                f"    map_incidents: {long_slot}",
            ]
            if local_model
            else []
        ),
        "  daily_budget_tokens: 2000000",
        "  deprecation_feed: null",
        *_onboarding_block(llm.onboarding),
        "redaction:",
        "  custom_patterns: []",
        "  tenant_constants: []",
        "calibration:",
        "  path: local/evals/calibration.json   # ./install.sh calibrate writes it",
        "  min_grade: smoke",
        "  # A trial floor: smoke calibration is too coarse to route at 0.80. Raise it",
        "  # before assisted mode, once thresholds are set from your history (ADR-0003).",
        "  min_score: 0.0",
        "paths:",
        "  taxonomy: taxonomy/classification.yaml",
        "  hypotheses: taxonomy/hypotheses.yaml",
        "  catalog: local/catalog.yaml",
        "  prompts: prompts/",
        "  runbooks: runbooks/",
        "  packs: packs/",
        "routing:",
        "  - id: db-saturation",
        "    match: {classification: database/saturation, severity: [P1, P2, P3]}",
        "    actions: [work_note]",
        "    rca: lite",
        "    comms: null",
        "default: {id: generic, actions: [work_note], rca: none}",
        "proposals:",
        "  ttl_hours: 24",
        "grants_request: []",
    ]
    if answers.pagerduty is not None:
        out += [
            "paging:",
            "  pagerduty:",
            "    token_env: PAGERDUTY_TOKEN",
            f"    from_email: {q(answers.pagerduty.from_email)}",
        ]
    if answers.confluence is not None:
        c = answers.confluence
        out += [
            "knowledge:                           # team docs as RCA guidance, never evidence",
            "  sources:",
            "    - kind: confluence",
            "      space: confluence",
            f"      base_url: {q(c.base_url)}",
            f"      space_key: {q(c.space_key)}",
            "      user_env: CONFLUENCE_USER",
            "      token_env: CONFLUENCE_API_TOKEN",
            "      max_pages: 200",
        ]
    out += _plugins_block(answers.onboard)
    out += [
        "onboarding:                          # ADR-0023: the instance onboards the system itself",
        "  workspace: local/onboarding",
        "  autopilot_interval_h: 24",
        "retention:",
        "  events_days: 395",
        "  comms_days: 395",
        "  alerts_days: 90",
    ]
    return "\n".join(out) + "\n"


def alerts_webhook(answers: Answers) -> bool:
    """Alerts arrive by webhook: an Alertmanager says so, or there's no ticket system."""
    signals = answers.signals
    return answers.servicenow.kind == "none" or bool(signals and signals.alertmanager)


def _signals_block(answers: Answers) -> list[str]:
    """ADR-0023 §4-5: the alert webhook, and the Prometheus and Loki the instance reads."""
    q = json.dumps
    out: list[str] = []
    if alerts_webhook(answers):
        out += [
            "webhooks:                     # Alertmanager/Grafana: bearer token in local/.env",
            "  - source: alertmanager",
            "    kind: alertmanager",
            "    secret_env: SUPPORTAGENT_ALERTMANAGER_TOKEN",
        ]
    signals = answers.signals
    if signals is not None and (signals.prometheus_url or signals.loki_url):
        out.append("tools:                               # read-only: detectors are measured here")
        if signals.prometheus_url:
            out += ["  prometheus:", f"    base_url: {q(signals.prometheus_url)}"]
        if signals.loki_url:
            out += ["  loki:", f"    base_url: {q(signals.loki_url)}"]
    return out


def _plugins_block(onboard: OnboardAnswers | None) -> list[str]:
    """ADR-0017: where onboarding finds plugins it can install (a directory or a git commit)."""
    if onboard is None or not onboard.catalog:
        return []
    q = json.dumps
    if onboard.catalog_ref:
        entry = f"{{git: {q(onboard.catalog)}, ref: {q(onboard.catalog_ref)}"
        entry += f", path: {q(onboard.catalog_path)}}}" if onboard.catalog_path else "}"
    else:
        entry = q(onboard.catalog)
    return [
        "plugins:                             # ADR-0017: plugins onboarding may install",
        f"  catalogs: [{entry}]",
        "  auto_install: none                 # each one waits for `supportagent plugin install`",
    ]


def render_system(answers: Answers) -> str | None:
    """``local/system.yaml``: the system ``onboard run`` reads (ADR-0015 §1), or ``None``.

    ServiceNow and Confluence are the ones already configured; repositories
    are read with ``GITHUB_TOKEN``. Every secret stays in ``local/.env``.
    """
    onboard = answers.onboard
    if onboard is None:
        return None
    q = json.dumps
    out = [
        "# The system to onboard (ADR-0015 §1): written by `supportagent init` (./install.sh).",
        "# The installer stores it in the instance, which onboards the system itself",
        "# (ADR-0023): edit it later on /ui/onboarding/system. Onboarding is read-only.",
        "system:",
        f"  id: {q(onboard.system_id)}",
        f"  name: {q(onboard.system_name)}",
        "sources:",
    ]
    tickets = answers.servicenow.kind in ("basic", "oauth") and onboard.assignment_groups
    if tickets:
        out.append("  servicenow:")
        if onboard.business_service:
            out.append(f"    business_service: {q(onboard.business_service)}")
        out += [
            f"    assignment_groups: [{', '.join(q(g) for g in onboard.assignment_groups)}]",
            "    history: 12mo",
        ]
    if answers.confluence is not None:
        c = answers.confluence
        out += [
            "  confluence:",
            f"    - {{space: {q(c.space_key)}, base_url: {q(c.base_url)}, "
            "user_env: CONFLUENCE_USER, token_env: CONFLUENCE_API_TOKEN}",
        ]
    if onboard.repos or onboard.local_dir:
        out.append("  git:")
    if onboard.local_dir:
        out += [
            f"    - repo: {q('file:' + SYSTEM_MOUNT)}   # {onboard.local_dir}, read-only",
            f"      paths: [{', '.join(q(p) for p in LOCAL_PATHS)}]",
            '      exclude: ["backups/**", "**/archive*/**"]',
        ]
    if onboard.repos:
        for repo in onboard.repos:
            out.append(f"    - repo: {q(repo)}")
            out.append(f"      paths: [{', '.join(q(p) for p in REPO_PATHS)}]")
            if onboard.github_token:
                out.append("      token_env: GITHUB_TOKEN")
    return "\n".join(out) + "\n"


def render_catalog(answers: Answers) -> str:
    """``local/catalog.yaml``: one entry per watched service."""
    q = json.dumps
    out = [
        "# Services SupportAgent watches: written by `supportagent init`.",
        "# An incident maps to a service by its CI name (resources) or CI sys_id",
        "# (cmdb_ci); an incident on any other CI safe-defaults as unresolved.",
        "# The full reference is catalog/services.example.yaml.",
        "version: local-1",
        "services:",
    ]
    docs = "    docs: [confluence]" if answers.confluence is not None else None
    services = answers.services
    if not services:  # alerts only: onboarding drafts the real catalog from the system
        name = service_name(answers.onboard.system_name if answers.onboard else answers.team_name)
        out.insert(
            -1, "# Alerts only: one entry for the whole system until onboarding drafts more."
        )
        services = (ServiceAnswer(name, (name,)),)
    for svc in services:
        # ARCH §14: triage needs a paging_integration on every service. Without a
        # configured provider it resolves to the Null Object (no page lookups,
        # enrich_page a no-op) and doctor only warns.
        out += [
            f"  - name: {q(svc.name)}",
            f"    resources: {_flow(svc.resources)}",
            f"    cmdb_ci: {_flow(svc.cmdb_ci)}",
            f"    paging_integration: {q('pagerduty:' + (svc.pagerduty_service or svc.name))}",
        ]
        if docs:
            out.append(docs)
    return "\n".join(out) + "\n"


def render_realm(answers: Answers, generated: Mapping[str, str]) -> str:
    """The Keycloak realm, imported on first start (``--import-realm``)."""
    first, _, last = answers.admin_user.partition(" ")
    realm = {
        "realm": REALM,
        "enabled": True,
        "sslRequired": "none",  # localhost over http; the ports bind 127.0.0.1 only
        "registrationAllowed": False,
        "clients": [
            {
                "clientId": CLIENT_ID,
                "enabled": True,
                "publicClient": False,
                "secret": generated["OIDC_CLIENT_SECRET"],
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "redirectUris": [f"http://localhost:{answers.app_port}/auth/callback"],
                "webOrigins": [f"http://localhost:{answers.app_port}"],
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    # BL-046: Sign out ends the Keycloak session and returns here.
                    "post.logout.redirect.uris": f"http://localhost:{answers.app_port}/ui/login",
                },
            }
        ],
        "users": [
            {
                "username": answers.admin_user,
                "enabled": True,
                "email": answers.admin_email,
                "emailVerified": True,
                "firstName": first or answers.admin_user,
                "lastName": last or "Admin",
                "credentials": [
                    {"type": "password", "value": answers.admin_password, "temporary": False}
                ],
            }
        ],
    }
    return json.dumps(realm, indent=2) + "\n"


def _onboarding_block(frontier: FrontierAnswers | None) -> list[str]:
    if frontier is None:
        return []
    return [
        "  onboarding:                        # ADR-0015: reads docs, drafts the system graph",
        f"    provider: {frontier.kind}",
        "    api_key_env: ONBOARDING_LLM_API_KEY",
        f"    model_id: {json.dumps(frontier.model_id)}",
        "    timeout_s: 60",
        "    max_output_tokens: 4000",
    ]


def _flow(items: Sequence[str]) -> str:
    return "[" + ", ".join(json.dumps(i) for i in items) + "]"


def _env_quote(key: str, value: str) -> str:
    if "'" in value or "\n" in value:
        raise ValueError(  # noqa: TRY003
            f"{key}: a value with a single quote or a newline can't go in local/.env"
        )
    return f"'{value}'"


__all__ = [
    "CLIENT_ID",
    "DEMO_SERVICES",
    "DEMO_SN_URL",
    "GENERATED",
    "INTEGRATION_USER",
    "REALM",
    "REPO_PATHS",
    "Answers",
    "ConfluenceAnswers",
    "FrontierAnswers",
    "LlmAnswers",
    "OnboardAnswers",
    "PagerDutyAnswers",
    "ServiceAnswer",
    "ServiceNowAnswers",
    "generated_secrets",
    "parse_env",
    "render_catalog",
    "render_config",
    "render_env",
    "render_realm",
    "render_system",
    "service_name",
    "tenant_id",
]
