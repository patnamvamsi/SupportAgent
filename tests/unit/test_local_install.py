"""The local installer (``./install.sh``): generated files, the wizard, the demo ServiceNow."""

# Every credential below is a fake one for a fake service.
# ruff: noqa: S105, S106

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from supportagent.adapters.fakes.demo_servicenow import add_incident, build, seed_healthy
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.adapters.install.probes import (
    ProbeError,
    confluence_space,
    list_models,
    servicenow_cis,
)
from supportagent.app.doctor.connectors.servicenow import (
    AuditRetentionCheck,
    AuthCheck,
    HistoryAccessCheck,
    IntegrationRoleCheck,
    JournalVisibilityCheck,
    RateLimitCheck,
)
from supportagent.bootstrap import ticket_connector
from supportagent.config.loader import load_config
from supportagent.config.local_install import (
    DEMO_SERVICES,
    Answers,
    ConfluenceAnswers,
    LlmAnswers,
    ServiceAnswer,
    ServiceNowAnswers,
    generated_secrets,
    parse_env,
    render_catalog,
    render_config,
    render_env,
    render_realm,
    service_name,
    tenant_id,
)
from supportagent.domain.model.catalog import Catalog
from supportagent.entrypoints.cli import init as wizard
from supportagent.entrypoints.cli.main import app

pytestmark = pytest.mark.unit


def _answers(**over: Any) -> Answers:
    base: dict[str, Any] = {
        "team_name": "Acme Support",
        "llm": LlmAnswers(kind="anthropic", model_id="claude-sonnet-4-5-20250929", api_key="k"),
        "servicenow": ServiceNowAnswers(kind="demo"),
        "services": DEMO_SERVICES,
        "admin_user": "ada",
        "admin_email": "ada@example.com",
        "admin_password": "pw-ada-123",
    }
    return Answers(**{**base, **over})


def _write(tmp_path: Path, answers: Answers) -> dict[str, str]:
    generated = generated_secrets(answers.previous)
    env_text = render_env(answers, generated)
    (tmp_path / "supportagent.yaml").write_text(render_config(answers))
    return parse_env(env_text)


@pytest.mark.parametrize(
    "llm",
    [
        LlmAnswers(kind="anthropic", model_id="claude-sonnet-4-5-20250929", api_key="k"),
        LlmAnswers(kind="openai", model_id="gpt-4.1-2025-04-14", api_key="k"),
        LlmAnswers(kind="local", model_id="qwen/qwen3.8-27b", base_url="http://h:1234/v1"),
        LlmAnswers(kind="ollama", model_id="qwen3:32b", base_url="http://h:11434"),
    ],
    ids=["anthropic", "openai", "local", "ollama"],
)
def test_every_llm_choice_renders_a_config_that_loads(tmp_path: Path, llm: LlmAnswers) -> None:
    answers = _answers(llm=llm)
    env = _write(tmp_path, answers)
    config = load_config(tmp_path / "supportagent.yaml", env)
    assert config.mode == "shadow"
    assert set(config.llm.slots) == {"classify", "propose_hypotheses", "entail"}
    assert config.llm.base_url == llm.base_url
    assert config.identity.backchannel_url == "http://keycloak:8080"
    assert config.identity.oidc_issuer == "http://localhost:8880/realms/supportagent"


def test_a_real_instance_with_oauth_confluence_and_pagerduty_loads(tmp_path: Path) -> None:
    from supportagent.config.local_install import PagerDutyAnswers  # noqa: PLC0415

    answers = _answers(
        servicenow=ServiceNowAnswers(
            kind="oauth",
            instance_url="https://acme.service-now.com",
            client_id="cid",
            client_secret="csecret",
        ),
        services=(ServiceAnswer("mail-server-us", ("MailServerUS",), ("b0c4",)),),
        confluence=ConfluenceAnswers("https://x.atlassian.net/wiki", "OPS", "a@x.com", "tok"),
        pagerduty=PagerDutyAnswers("pd-token", "oncall@x.com"),
        app_port=8801,
        keycloak_port=8881,
    )
    env = _write(tmp_path, answers)
    config = load_config(tmp_path / "supportagent.yaml", env)
    assert ticket_connector(config).auth.kind == "oauth_client_credentials"
    assert env["COMPOSE_PROFILES"] == ""  # no demo instance
    assert config.knowledge.sources[0].kind == "confluence"
    assert config.paging.pagerduty is not None
    assert config.identity.redirect_url == "http://localhost:8801/auth/callback"
    catalog = Catalog.model_validate(yaml.safe_load(render_catalog(answers)))
    [svc] = catalog.services
    assert (svc.name, svc.resources, svc.cmdb_ci, svc.docs) == (
        "mail-server-us",
        ["MailServerUS"],
        ["b0c4"],
        ["confluence"],
    )


def test_the_realm_matches_the_env_and_the_app_port() -> None:
    answers = _answers(app_port=8802)
    generated = generated_secrets({})
    env = parse_env(render_env(answers, generated))
    realm = json.loads(render_realm(answers, generated))
    [client] = realm["clients"]
    assert client["secret"] == env["OIDC_CLIENT_SECRET"]
    assert client["redirectUris"] == ["http://localhost:8802/auth/callback"]
    [user] = realm["users"]
    assert user["username"] == "ada"
    assert user["credentials"][0]["value"] == "pw-ada-123"


def test_generated_secrets_are_kept_on_a_rerun_and_the_db_url_follows() -> None:
    first = generated_secrets({})
    assert len(bytes.fromhex(first["SA_MASTER_KEY_ENV"])) == 32
    assert len(first["SA_SESSION_SECRET"]) >= 32
    again = generated_secrets({**first, "unrelated": "x"})
    assert again == first
    env = parse_env(render_env(_answers(), again))
    assert first["POSTGRES_PASSWORD"] in env["SUPPORTAGENT_DB_URL"]


def test_a_value_that_cannot_be_quoted_is_refused() -> None:
    answers = _answers(llm=LlmAnswers(kind="openai", model_id="gpt-4.1-2025-04-14", api_key="a'b"))
    with pytest.raises(ValueError, match="single quote"):
        render_env(answers, generated_secrets({}))


def test_names_become_valid_ids() -> None:
    assert tenant_id("Acme Support (EU)!") == "acme-support-eu"
    assert tenant_id("?") == "local"
    assert service_name("Sales Force Automation") == "sales-force-automation"
    assert service_name("X") == "svc-x"


def test_url_helpers() -> None:
    assert (
        wizard._for_container("http://localhost:1234/v1") == "http://host.docker.internal:1234/v1"
    )
    assert wizard._for_container("http://127.0.0.1:11434") == "http://host.docker.internal:11434"
    assert wizard._for_container("http://10.1.2.3:1234/v1") == "http://10.1.2.3:1234/v1"
    assert wizard._instance_url("dev12345") == "https://dev12345.service-now.com"
    assert wizard._instance_url("https://acme.service-now.com/now/nav/ui") == (
        "https://acme.service-now.com"
    )
    assert wizard._confluence_url(
        "https://x.atlassian.net/wiki/spaces/OPS/overview?homepageId=1"
    ) == ("https://x.atlassian.net/wiki", "OPS")
    assert wizard._numbers("1, 3,3", 4) == [0, 2]
    assert wizard._numbers("all", 3) == [0, 1, 2]
    assert wizard._numbers("9", 3) == []


def test_init_with_answers_writes_local_and_keeps_secrets_on_rerun(tmp_path: Path) -> None:
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "team_name": "Demo",
                "llm": {"kind": "local", "base_url": "http://localhost:1234/v1", "model_id": "m-1"},
                "servicenow": {"kind": "demo"},
                "admin_password": "pw-1",
            }
        )
    )
    out = tmp_path / "local"
    runner = CliRunner()
    first = runner.invoke(app, ["init", "--dir", str(out), "--answers", str(answers)])
    assert first.exit_code == 0, first.output
    env = parse_env((out / ".env").read_text())
    assert stat.S_IMODE((out / ".env").stat().st_mode) == 0o600
    assert env["COMPOSE_PROFILES"] == "demo" and env["SA_ADMIN_PASSWORD"] == "pw-1"
    config = yaml.safe_load((out / "supportagent.yaml").read_text())
    assert config["llm"]["base_url"] == "http://host.docker.internal:1234/v1"
    assert "api_key" not in (out / "answers.json").read_text()

    again = runner.invoke(app, ["init", "--dir", str(out), "--answers", str(answers)])
    assert again.exit_code == 0, again.output
    env2 = parse_env((out / ".env").read_text())
    assert env2["SA_MASTER_KEY_ENV"] == env["SA_MASTER_KEY_ENV"]  # data stays readable
    assert env2["POSTGRES_PASSWORD"] == env["POSTGRES_PASSWORD"]


def test_the_interactive_wizard_demo_path(tmp_path: Path) -> None:
    """Keystrokes for: team, Anthropic + key + model, demo ServiceNow, no extras, sign-in."""
    keys = "\n".join(
        [
            "Acme",  # team
            "1",  # anthropic
            "sk-test",  # API key
            "claude-sonnet-4-5-20250929",  # model id (no probe: typed)
            "",  # system name: the team's
            "",  # no checkout on this machine
            "",  # no GitHub repositories
            "",  # no plugin catalogue
            "",  # no Prometheus
            "",  # no Loki
            "",  # no Alertmanager
            "2",  # demo ServiceNow
            "n",  # Confluence
            "n",  # PagerDuty
            "ada",  # user
            "",  # email default
            "",  # generate password
        ]
    )
    out = tmp_path / "local"
    result = CliRunner().invoke(app, ["init", "--dir", str(out), "--no-probe"], input=keys + "\n")
    assert result.exit_code == 0, result.output
    env = parse_env((out / ".env").read_text())
    assert env["LLM_API_KEY"] == "sk-test" and env["SA_ADMIN_USER"] == "ada"
    assert len(env["SA_ADMIN_PASSWORD"]) >= 12
    assert "sk-test" not in result.output  # secrets are not echoed
    catalog = yaml.safe_load((out / "catalog.yaml").read_text())
    assert [s["name"] for s in catalog["services"]] == ["nightly-sync", "core-db", "payroll-api"]


# --- probes ---------------------------------------------------------------------------


def test_model_listing_per_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.anthropic.com":
            assert request.headers["x-api-key"] == "k"
            return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-5-20250929"}]})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": "qwen/qwen3.8-27b"}, {"id": "nomic-embed-text"}]}
            )
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:32b"}]})
        return httpx.Response(404)

    t = httpx.MockTransport(handler)
    assert list_models("anthropic", base_url=None, api_key="k", transport=t) == [
        "claude-sonnet-4-5-20250929"
    ]
    assert list_models("local", base_url="http://h:1234/v1", api_key=None, transport=t) == [
        "qwen/qwen3.8-27b"
    ]  # embedding models aren't chat models
    assert list_models("ollama", base_url="http://h:11434", api_key=None, transport=t) == [
        "qwen3:32b"
    ]
    refused = httpx.MockTransport(lambda _: httpx.Response(401, json={}))
    with pytest.raises(ProbeError, match="refused the credentials"):
        list_models("openai", base_url=None, api_key="bad", transport=refused)


def test_servicenow_probe_counts_cis_and_names_a_missing_user() -> None:
    sn = FakeServiceNowHTTP()
    seed_healthy(sn)
    now = datetime(2026, 10, 29, tzinfo=UTC)
    for n in (0, 1, 3):  # core-db twice, payroll-api once
        add_incident(sn, n, now=now)
    t = SyncASGITransport(sn.app)
    cis = servicenow_cis("https://x", headers={}, integration_user="svc_supportagent", transport=t)
    counts = {c.name: c.incidents for c in cis}
    assert counts == {"core-db": 2, "payroll-api": 2}  # one is the seeded closed incident
    with pytest.raises(ProbeError, match="no user 'nobody'"):
        servicenow_cis("https://x", headers={}, integration_user="nobody", transport=t)


def test_confluence_probe_reads_the_space_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/api/v2/spaces"
        keys = request.url.params["keys"]
        return httpx.Response(200, json={"results": [{"name": "Ops"}] if keys == "OPS" else []})

    t = httpx.MockTransport(handler)
    assert (
        confluence_space(
            "https://x.atlassian.net/wiki", space_key="OPS", user="u", token="t", transport=t
        )
        == "Ops"
    )
    with pytest.raises(ProbeError, match="no space with key 'NOPE'"):
        confluence_space(
            "https://x.atlassian.net", space_key="NOPE", user="u", token="t", transport=t
        )


# --- the demo ServiceNow ------------------------------------------------------------------


def test_the_demo_instance_passes_every_servicenow_doctor_check(clock: Any) -> None:
    from tests.unit.test_doctor import _ctx  # noqa: PLC0415

    sn = FakeServiceNowHTTP()
    seed_healthy(sn)
    add_incident(sn, 0, now=clock.now())
    ctx = _ctx(sn, clock=clock)
    for check in (
        AuthCheck(),
        JournalVisibilityCheck(),
        IntegrationRoleCheck(),
        AuditRetentionCheck(),
        HistoryAccessCheck(),
        RateLimitCheck(),
    ):
        result = check.run(ctx)
        assert result.status == "pass", (result.name, result.detail)


def test_the_demo_feed_adds_incidents_updated_now(clock: Any) -> None:
    sn, stop = build(clock=clock, interval_s=0.01, first_after_s=0.0)
    try:
        t = SyncASGITransport(sn.app)
        with httpx.Client(transport=t, base_url="http://demo") as http:
            assert http.get("/health").json()["status"] == "ok"
            for _ in range(200):
                result = http.get("/api/now/table/incident").json()["result"]
                rows = [r for r in result if r["sys_id"] != "demo-0000"]  # the seeded old one
                if len(rows) >= 2:
                    break
                stop.wait(0.01)
    finally:
        stop.set()
    run = f"{int(clock.now().timestamp()) // 60 % 10_000:04d}"
    assert {r["number"] for r in rows[:2]} == {f"INC{run}001", f"INC{run}002"}
    assert all(r["sys_updated_by"] == "alice" for r in rows)


# --- ADR-0015 decision 3: a frontier model for onboarding --------------------------------


def test_a_local_model_with_a_frontier_one_for_onboarding(tmp_path: Path) -> None:
    from supportagent.bootstrap import onboarding_llm  # noqa: PLC0415
    from supportagent.config.local_install import FrontierAnswers  # noqa: PLC0415

    llm = LlmAnswers(
        kind="local",
        model_id="qwen/qwen3.8-27b",
        base_url="http://h:1234/v1",
        onboarding=FrontierAnswers("anthropic", "claude-sonnet-4-5-20250929", "sk-onb"),
    )
    answers = _answers(llm=llm)
    env = _write(tmp_path, answers)
    assert env["ONBOARDING_LLM_API_KEY"] == "sk-onb"
    config = load_config(tmp_path / "supportagent.yaml", env)
    chosen = onboarding_llm(config)
    assert (chosen.provider, chosen.model_id, chosen.source) == (
        "anthropic",
        "claude-sonnet-4-5-20250929",
        "onboarding",
    )
    assert config.llm.slots["classify"].model_id == "qwen/qwen3.8-27b"  # triage stays local

    plain = load_config(tmp_path / "supportagent.yaml", env).model_copy(
        update={"llm": config.llm.model_copy(update={"onboarding": None})}
    )
    fallback = onboarding_llm(plain)
    assert (fallback.model_id, fallback.source) == ("qwen/qwen3.8-27b", "triage")


def test_the_onboarding_model_must_be_pinned() -> None:
    from pydantic import ValidationError  # noqa: PLC0415

    from supportagent.config.schema import OnboardingModel  # noqa: PLC0415

    with pytest.raises(ValidationError, match="floating alias"):
        OnboardingModel.model_validate(
            {"provider": "openai", "api_key": "k", "model_id": "gpt-4o-latest"}
        )


def test_the_wizard_offers_a_frontier_model_after_a_local_one(tmp_path: Path) -> None:
    keys = "\n".join(
        [
            "Acme",
            "3",  # a local OpenAI-compatible server
            "http://localhost:1234/v1",
            "qwen/qwen3.8-27b",
            "y",  # add a frontier model for onboarding
            "1",  # Anthropic
            "sk-onboarding",
            "claude-sonnet-4-5-20250929",
            "",  # system name: the team's
            "/srv/shop",  # its checkout on this machine
            "",  # no GitHub repositories
            "",  # no plugin catalogue
            "http://localhost:9090",  # Prometheus
            "",  # no Loki
            "y",  # an Alertmanager sends alerts here
            "3",  # no ticket system: alerts only
            "n",
            "n",
            "ada",
            "",
            "",
        ]
    )
    out = tmp_path / "local"
    result = CliRunner().invoke(app, ["init", "--dir", str(out), "--no-probe"], input=keys + "\n")
    assert result.exit_code == 0, result.output
    env = parse_env((out / ".env").read_text())
    assert env["ONBOARDING_LLM_API_KEY"] == "sk-onboarding"
    config = yaml.safe_load((out / "supportagent.yaml").read_text())
    assert config["llm"]["onboarding"]["model_id"] == "claude-sonnet-4-5-20250929"
    assert config["llm"]["base_url"] == "http://host.docker.internal:1234/v1"
    public = (out / "answers.json").read_text()
    assert "sk-onboarding" not in public and "claude-sonnet-4-5-20250929" in public
    assert "servicenow" not in config and config["webhooks"][0]["source"] == "alertmanager"
    assert config["tools"]["prometheus"]["base_url"] == "http://host.docker.internal:9090"
    assert env["SA_SYSTEM_DIR"] == "/srv/shop"
    assert "file:/mnt/system" in (out / "system.yaml").read_text()


# --- ADR-0017: onboarding a system from the installer -----------------------------------


_COMMIT = "c0ffee" + "0" * 34


def test_answers_that_onboard_a_system_write_a_system_yaml_that_loads(tmp_path: Path) -> None:
    from supportagent.config.loader import load_system_manifest  # noqa: PLC0415

    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "team_name": "Acme",
                "llm": {"kind": "anthropic", "model_id": "claude-sonnet-4-5-20250929"},
                "servicenow": {
                    "kind": "basic",
                    "instance_url": "https://acme.service-now.com",
                    "user": "svc",
                    "password": "pw",
                },
                "services": [{"name": "payments-api", "cmdb_ci": ["CI9"]}],
                "confluence": {
                    "base_url": "https://acme.atlassian.net/wiki",
                    "space_key": "PAY",
                    "user": "ada@acme.test",
                    "token": "cf-token",
                },
                "onboard": {
                    "system_name": "Payments Platform",
                    "assignment_groups": ["PAY-L2", "PAY-DBA"],
                    "business_service": "Payments",
                    "repos": ["github:acme-payments/*"],
                    "github_token": "gh-secret-token",
                    "catalog": "https://github.com/acme/sa-plugins.git",
                    "catalog_ref": _COMMIT,
                    "catalog_path": "catalog",
                },
                "admin_password": "pw-1",
            }
        )
    )
    out = tmp_path / "local"
    result = CliRunner().invoke(app, ["init", "--dir", str(out), "--answers", str(answers)])
    assert result.exit_code == 0, result.output
    env = parse_env((out / ".env").read_text())
    assert env["GITHUB_TOKEN"] == "gh-secret-token"
    for written in ("system.yaml", "supportagent.yaml", "answers.json"):
        assert "gh-secret-token" not in (out / written).read_text(), written

    system = load_system_manifest(out / "system.yaml", env)
    assert (system.system.id, system.system.name) == ("payments-platform", "Payments Platform")
    sn = system.sources.servicenow
    assert sn is not None and sn.assignment_groups == ["PAY-L2", "PAY-DBA"]
    assert sn.business_service == "Payments"
    [space] = system.sources.confluence
    assert space.space == "PAY" and space.token is not None
    assert space.token.get_secret_value() == "cf-token"
    [repo] = system.sources.git
    assert repo.repo == "github:acme-payments/*" and "**/pom.xml" in repo.paths
    assert repo.token is not None and repo.token.get_secret_value() == "gh-secret-token"

    config = load_config(out / "supportagent.yaml", env)
    [catalog] = config.plugins.catalogs
    assert not isinstance(catalog, str)
    assert (catalog.git, catalog.ref, catalog.path) == (
        "https://github.com/acme/sa-plugins.git",
        _COMMIT,
        "catalog",
    )
    assert config.plugins.auto_install == "none"


def test_a_directory_catalogue_and_no_repositories_render_too(tmp_path: Path) -> None:
    from supportagent.config.local_install import OnboardAnswers, render_system  # noqa: PLC0415

    onboard = OnboardAnswers(
        system_id="billing",
        system_name="Billing",
        assignment_groups=("BILL",),
        catalog="/srv/supportagent/catalog",
    )
    answers = _answers(
        servicenow=ServiceNowAnswers(kind="basic", instance_url="https://a.service-now.com"),
        onboard=onboard,
    )
    env = _write(tmp_path, answers)
    config = load_config(tmp_path / "supportagent.yaml", env)
    assert config.plugins.catalogs == ["/srv/supportagent/catalog"]
    assert "GITHUB_TOKEN" not in env
    raw = yaml.safe_load(render_system(answers) or "")
    assert raw["sources"] == {
        "servicenow": {"assignment_groups": ["BILL"], "history": "12mo"},
    }


def test_no_onboarding_writes_no_system_and_keeps_an_earlier_one(tmp_path: Path) -> None:
    from supportagent.config.local_install import render_system  # noqa: PLC0415

    assert render_system(_answers()) is None
    out = tmp_path / "local"
    out.mkdir()
    (out / "system.yaml").write_text("# kept\n")
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "llm": {"kind": "local", "base_url": "http://localhost:1234/v1", "model_id": "m-1"},
                "servicenow": {"kind": "demo"},
                "admin_password": "pw-1",
            }
        )
    )
    result = CliRunner().invoke(app, ["init", "--dir", str(out), "--answers", str(answers)])
    assert result.exit_code == 0, result.output
    assert (out / "system.yaml").read_text() == "# kept\n"


def test_installer_alerts_only_answers(tmp_path: Path) -> None:
    """ADR-0023 §5: no ticket system; the code on this machine and the signals instead."""
    from supportagent.config.loader import load_system_manifest  # noqa: PLC0415

    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "team_name": "Acme",
                "llm": {"kind": "anthropic", "model_id": "claude-sonnet-4-5-20250929"},
                "servicenow": {"kind": "none"},
                "onboard": {"system_name": "Trading", "local_dir": "/home/ada/trading"},
                "signals": {
                    "prometheus_url": "http://localhost:9090",
                    "loki_url": "http://loki:3100",
                },
                "admin_password": "pw-1",
            }
        )
    )
    out = tmp_path / "local"
    result = CliRunner().invoke(app, ["init", "--dir", str(out), "--answers", str(answers)])
    assert result.exit_code == 0, result.output
    env = parse_env((out / ".env").read_text())
    assert env["SA_SYSTEM_DIR"] == "/home/ada/trading" and env["SUPPORTAGENT_ALERTMANAGER_TOKEN"]
    assert "SN_PASSWORD" not in env and env["COMPOSE_PROFILES"] == ""

    config = load_config(out / "supportagent.yaml", env)
    assert config.servicenow is None and config.mode == "shadow"
    assert [w.source for w in config.webhooks] == ["alertmanager"], "alerts are the intake"
    assert config.tools.prometheus is not None and config.tools.loki is not None
    assert config.tools.prometheus.base_url == "http://host.docker.internal:9090"
    assert config.onboarding.workspace == "local/onboarding", "writable by the host user"

    system = load_system_manifest(out / "system.yaml", env)
    assert system.sources.servicenow is None
    [repo] = system.sources.git
    assert repo.repo == "file:/mnt/system" and "backups/**" in repo.exclude
    catalog = yaml.safe_load((out / "catalog.yaml").read_text())
    assert [s["name"] for s in catalog["services"]] == ["trading"]
