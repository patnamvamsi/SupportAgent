"""WP-002: config schema + loader (02 §1).

Canonical test IDs from 10-wp §WP-002:
``test_config_rejects_unknown_key``, ``test_config_secret_from_env_masked``,
``test_config_routing_label_must_exist``, ``test_config_floating_model_alias_rejected``,
``test_config_standard_mode_rejected_before_v02``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from supportagent.bootstrap import ticket_connector
from supportagent.config.loader import load_config
from supportagent.config.schema import BasicAuth
from supportagent.config.validation import validate_bundle
from supportagent.domain.errors import ConfigError
from supportagent.domain.model import Catalog, ClassificationTaxonomy, HypothesisTaxonomy
from tests.builders import SECRET_ENV, a_catalog_file, a_config_file
from tests.yaml_io import write_yaml_file


def test_config_rejects_unknown_key(tmp_path: Path) -> None:
    # 02 §1: extra="forbid" everywhere — an unknown key is a startup ConfigError.
    data = a_config_file()
    data["database"]["bogus_key"] = 1
    path = write_yaml_file(tmp_path, "config.yaml", data)
    with pytest.raises(ConfigError, match="bogus_key"):
        load_config(path, SECRET_ENV)


def test_config_secret_from_env_masked(tmp_path: Path) -> None:
    # 02 §1: load_config(path, env) resolves `*_env` into SecretStr.
    path = write_yaml_file(tmp_path, "config.yaml", a_config_file())
    cfg = load_config(path, SECRET_ENV)
    url = cfg.database.url
    assert isinstance(url, SecretStr)
    assert url.get_secret_value() == SECRET_ENV["SUPPORTAGENT_DB_URL"]
    # Masking: no secret value may appear in rendered forms.
    rendered = repr(cfg) + str(cfg.database) + repr(cfg.identity)
    assert "sa:sa@localhost" not in rendered
    assert SECRET_ENV["SA_SESSION_SECRET"] not in rendered
    assert SECRET_ENV["ANTHROPIC_API_KEY"] not in rendered


def test_config_floating_model_alias_rejected(tmp_path: Path) -> None:
    # 02 §1 / ARCH §9: llm.slots.*.model_id must be a dated snapshot — a
    # floating alias (no date/version component) fails validation.
    data = a_config_file()
    data["llm"]["slots"]["classify"]["model_id"] = "claude-sonnet-latest"
    path = write_yaml_file(tmp_path, "config.yaml", data)
    with pytest.raises(ConfigError, match="floating"):
        load_config(path, SECRET_ENV)


def test_config_standard_mode_rejected_before_v02(tmp_path: Path) -> None:
    # 02 §1: `mode: standard` is rejected before v0.2.
    data = a_config_file()
    data["mode"] = "standard"
    path = write_yaml_file(tmp_path, "config.yaml", data)
    with pytest.raises(ConfigError, match="standard"):
        load_config(path, SECRET_ENV)


@pytest.mark.parametrize("model_id", ["claude-3-5-sonnet-latest", "gpt-4o-latest"])
def test_config_latest_suffix_alias_rejected(tmp_path: Path, model_id: str) -> None:
    # 02 §1 denylist: a "-latest" alias floats even when the id carries digits.
    data = a_config_file()
    data["llm"]["slots"]["classify"]["model_id"] = model_id
    path = write_yaml_file(tmp_path, "config.yaml", data)
    with pytest.raises(ConfigError, match="floating"):
        load_config(path, SECRET_ENV)


def test_config_assisted_mode_accepted_in_v01(tmp_path: Path) -> None:
    data = a_config_file()
    data["mode"] = "assisted"
    path = write_yaml_file(tmp_path, "config.yaml", data)
    assert load_config(path, SECRET_ENV).mode == "assisted"


def test_config_routing_label_must_exist(tmp_path: Path) -> None:
    # 02 §1: every routing row's match.classification must exist in the
    # taxonomy; the violation is reported by validate_bundle (cross-file).
    path = write_yaml_file(tmp_path, "config.yaml", a_config_file())
    cfg = load_config(path, SECRET_ENV)
    catalog = Catalog(**a_catalog_file())
    # A taxonomy that does NOT define database/saturation (the routing label).
    # model_validate: dict input is the raw-YAML entry point the loader uses.
    taxonomy = ClassificationTaxonomy.model_validate(
        {
            "version": "1.0.0",
            "labels": [{"id": "deploy/failure", "description": "A release failed or is stuck."}],
        }
    )
    hypotheses = HypothesisTaxonomy.model_validate(
        {"version": "1.0.0", "hypotheses": [{"id": "recent_change_push", "excludes": []}]}
    )
    issues = validate_bundle(cfg, catalog, taxonomy, hypotheses)
    assert any(issue.code == "routing.label-unknown" for issue in issues)


def test_config_rejects_unknown_config_version(tmp_path: Path) -> None:
    # 06 §3: config_version: 1 — the loader rejects unknown versions.
    data = a_config_file()
    data["config_version"] = 2
    path = write_yaml_file(tmp_path, "config.yaml", data)
    with pytest.raises(ConfigError, match="config_version"):
        load_config(path, SECRET_ENV)


def test_config_missing_secret_env_var_rejected(tmp_path: Path) -> None:
    # 02 §1: resolving `*_env` against a missing variable fails with the
    # variable name, not a generic error.
    path = write_yaml_file(tmp_path, "config.yaml", a_config_file())
    env = dict(SECRET_ENV)
    del env["SN_CLIENT_SECRET"]
    with pytest.raises(ConfigError, match="SN_CLIENT_SECRET"):
        load_config(path, env)


def test_config_session_secret_requires_32_bytes(tmp_path: Path) -> None:
    # 02 §1: the session secret must be >= 32 bytes (checked on the value).
    path = write_yaml_file(tmp_path, "config.yaml", a_config_file())
    env = {**SECRET_ENV, "SA_SESSION_SECRET": "too-short"}
    with pytest.raises(ConfigError, match="session_secret"):
        load_config(path, env)


def test_config_fixture_provider_requires_test_env(tmp_path: Path) -> None:
    # 02 §1: the fixture provider is only usable with SUPPORTAGENT_ENV=test
    # (or the --demo CLI flag, which conventionally sets that variable).
    data = a_config_file()
    data["llm"]["provider"] = "fixture"
    data["llm"]["api_key_env"] = None
    path = write_yaml_file(tmp_path, "config.yaml", data)
    with pytest.raises(ConfigError, match="fixture"):
        load_config(path, SECRET_ENV)
    assert load_config(path, {**SECRET_ENV, "SUPPORTAGENT_ENV": "test"}).llm.provider == "fixture"


def test_config_basic_auth_variant_resolves(tmp_path: Path) -> None:
    # 02 §1: auth kind basic is the alternate credential shape.
    data = a_config_file()
    data["servicenow"]["auth"] = {
        "kind": "basic",
        "user_env": "SN_BASIC_USER",
        "password_env": "SN_BASIC_PASSWORD",
    }
    path = write_yaml_file(tmp_path, "config.yaml", data)
    cfg = load_config(
        path, {**SECRET_ENV, "SN_BASIC_USER": "sn-user", "SN_BASIC_PASSWORD": "sn-pass"}
    )
    auth = ticket_connector(cfg).auth
    assert isinstance(auth, BasicAuth)
    assert auth.user.get_secret_value() == "sn-user"
    assert auth.password.get_secret_value() == "sn-pass"


def test_config_rejects_invalid_routing_action(tmp_path: Path) -> None:
    # 02 §1: routing actions are ActionKind names only.
    data = a_config_file()
    data["routing"][0]["actions"] = ["not_an_action_kind"]
    path = write_yaml_file(tmp_path, "config.yaml", data)
    with pytest.raises(ConfigError, match="actions"):
        load_config(path, SECRET_ENV)


def test_load_config_offline_needs_no_secrets_and_selects_the_fixture_llm() -> None:
    """WP-016: ``eval run`` without ``--live`` reads the example config offline."""
    from supportagent.config.loader import load_config_offline  # noqa: PLC0415

    repo = Path(__file__).resolve().parents[2]
    cfg = load_config_offline(repo / "supportagent.example.yaml")
    assert cfg.llm.provider == "fixture"
    assert cfg.llm.api_key is None
    assert cfg.routing[0].id == "db-saturation"
    assert cfg.identity.session_secret.get_secret_value().startswith("offline-eval-placeholder")
