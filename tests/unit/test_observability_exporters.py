"""WP-212: the opt-in exporter stack (ADR-0022 §4) stays generic and safe.

Targets come from settings only, every exporter is behind a profile, images
are pinned to exact versions, no exporter publishes a host port, and the
Postgres password is read from a file, never passed as a value.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_DIR = Path(__file__).resolve().parents[2] / "docker" / "observability"
_EXPORTERS = (
    "cadvisor",
    "node-exporter",
    "postgres-exporter",
    "kafka-exporter",
    "redis-exporter",
    "blackbox-exporter",
    "alertmanager",
)


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((_DIR / "compose.exporters.yaml").read_text("utf-8"))
    return data


def test_exporters_compose_config_valid() -> None:
    services = _compose()["services"]
    assert set(_EXPORTERS) <= set(services)
    for name in _EXPORTERS:
        svc = services[name]
        assert svc.get("profiles"), f"{name} must be opt-in through a profile"
        assert re.search(r":v\d+\.\d+\.\d+$", svc["image"]), f"{name} image must pin a version"
        assert "ports" not in svc, f"{name} must not publish a host port"


def test_exporter_targets_from_settings_only() -> None:
    services = _compose()["services"]
    pg = services["postgres-exporter"]["environment"]
    assert "${PG_EXPORTER_DSN_HOST" in pg["DATA_SOURCE_URI"]
    assert "DATA_SOURCE_PASS" not in pg, "the password comes from a file"
    assert pg["DATA_SOURCE_PASS_FILE"].startswith("/run/secrets/")
    assert "${KAFKA_EXPORTER_SERVER" in " ".join(services["kafka-exporter"]["command"])
    assert "${REDIS_EXPORTER_ADDR" in services["redis-exporter"]["environment"]["REDIS_ADDR"]
    assert "${EXPORTERS_TARGET_NETWORK" in _compose()["networks"]["target"]["name"]


def test_exporters_discovered_by_dns_not_listed() -> None:
    scrape = yaml.safe_load((_DIR / "exporters" / "scrape.yml").read_text("utf-8"))
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    for job in ("cadvisor", "node", "postgres", "kafka", "redis"):
        assert "dns_sd_configs" in jobs[job] and "static_configs" not in jobs[job]
    base = yaml.safe_load((_DIR / "prometheus.yml").read_text("utf-8"))
    assert base["scrape_config_files"] == ["/etc/prometheus/scrape.d/*.yml"]
    assert base["rule_files"] == ["/etc/prometheus/rules.d/*.yml"]
