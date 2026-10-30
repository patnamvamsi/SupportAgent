"""WP-O2 (ADR-0015 P1): ingest reads a system's sources into a workspace.

The round trip is the main test. Each reference system's sources are served
through fakes of ServiceNow (the contract fake), Confluence (REST v2) and
GitHub (REST), and ingested through the real adapters. Detection on the
workspace must then give the same plugins as on the lab directory: what
ingest stores is what the later stages need.
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.adapters.files.workspace import Workspace
from supportagent.adapters.gitsource.reader import GitHubReader, path_matches, read_local
from supportagent.bootstrap import (
    ONBOARDING_DIR,
    IngestTransports,
    detect_plugins,
    ingest_system,
    load_plugins,
)
from supportagent.domain.errors import ConfigError
from supportagent.domain.onboarding import IngestedFiles
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit

# Every credential here is a fake for a fake server.
# ruff: noqa: S105, S106

_SITE = "https://acme.atlassian.net"


# --- fakes built from a reference system ---------------------------------------------


def _fake_servicenow(system: Path, *, extra_text: str = "") -> FakeServiceNowHTTP:
    sn = FakeServiceNowHTTP()
    src = system / "sources" / "servicenow"
    for i, row in enumerate(yaml.safe_load((src / "incidents.yaml").read_text())):
        record = {
            "number": row["number"],
            "sys_id": row["sys_id"],
            "opened_at": row["opened_at"],
            "resolved_at": row.get("resolved_at"),
            "priority": row["priority"],
            "cmdb_ci.name": row.get("cmdb_ci"),
            "category": row.get("category"),
            "assignment_group.name": row["assignment_group"],
            "short_description": row["short_description"],
            "description": row["description"] + (extra_text if i == 0 else ""),
            "state": row["state"],
            "close_code": row["close_code"],
            "close_notes": row["close_notes"],
            "problem_id.number": row.get("problem_id"),
            "caused_by.number": row.get("caused_by"),
        }
        sn.seed_row("incident", record)
    for row in yaml.safe_load((src / "problems.yaml").read_text()):
        sn.seed_row("problem", {k: row[k] for k in ("number", "short_description", "state")})
    for row in yaml.safe_load((src / "changes.yaml").read_text()):
        sn.seed_row(
            "change_request",
            {
                "number": row["number"],
                "short_description": row["short_description"],
                "cmdb_ci.name": row["cmdb_ci"],
                "start_date": row["start_date"],
                "type": row["type"],
                "state": row["state"],
            },
        )
    cmdb = yaml.safe_load((src / "cmdb.yaml").read_text())
    for ci in cmdb["cis"]:
        sn.seed_row("cmdb_ci", ci)
    for rel in cmdb["relationships"]:
        sn.seed_row(
            "cmdb_rel_ci",
            {
                "sys_id": rel["sys_id"],
                "parent.name": rel["parent"],
                "type.name": rel["type"],
                "child.name": rel["child"],
            },
        )
    return sn


def _pages(system: Path) -> dict[str, list[dict[str, Any]]]:
    by_space: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((system / "sources" / "confluence").rglob("*.md")):
        _, front, body = path.read_text().split("---", 2)
        meta = yaml.safe_load(front)
        storage = "".join(f"<p>{html.escape(line)}</p>" for line in body.splitlines() if line)
        by_space.setdefault(meta["space"], []).append(
            {
                "id": meta["page_id"],
                "title": meta["title"],
                "version": {
                    "number": int(meta["version"]),
                    "createdAt": f"{meta['last_updated']}T10:00:00Z",
                },
                "body": {"storage": {"value": storage}},
                "_links": {"webui": f"/spaces/{meta['space']}/pages/{meta['page_id']}"},
                "labels": meta["labels"],
            }
        )
    return by_space


def _fake_confluence(system: Path) -> httpx.MockTransport:
    spaces = _pages(system)
    pages = {p["id"]: p for ps in spaces.values() for p in ps}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        path = request.url.path
        if path == "/wiki/api/v2/spaces":
            key = request.url.params.get("keys")
            return httpx.Response(200, json={"results": [{"id": key}] if key in spaces else []})
        if path.startswith("/wiki/api/v2/spaces/") and path.endswith("/pages"):
            key = path.split("/")[5]
            batch = [{k: v for k, v in p.items() if k != "labels"} for p in spaces[key]]
            return httpx.Response(200, json={"results": batch, "_links": {}})
        if path.endswith("/labels"):
            labels = pages[path.split("/")[5]]["labels"]
            return httpx.Response(200, json={"results": [{"name": n} for n in labels]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _fake_github(system: Path, org: str) -> httpx.MockTransport:
    git = system / "sources" / "git"
    repos = sorted(d.name for d in git.iterdir() if d.is_dir())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        path = request.url.path
        if path == f"/orgs/{org}/repos":
            return httpx.Response(200, json=[{"name": r, "default_branch": "main"} for r in repos])
        parts = path.split("/")
        if len(parts) > 5 and parts[4] == "git":  # /repos/<org>/<repo>/git/trees/main
            root = git / parts[3]
            tree = [
                {"path": p.relative_to(root).as_posix(), "type": "blob", "size": p.stat().st_size}
                for p in sorted(root.rglob("*"))
                if p.is_file()
            ]
            return httpx.Response(200, json={"tree": tree})
        if len(parts) > 5 and parts[4] == "contents":  # /repos/<org>/<repo>/contents/<path>
            file = git / parts[3] / "/".join(parts[5:])
            return httpx.Response(
                200, json={"content": base64.b64encode(file.read_bytes()).decode()}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _manifest(system: Path, tmp_path: Path) -> Path:
    raw = yaml.safe_load((system / "system.yaml").read_text())
    raw["sources"]["servicenow"]["history"] = "99mo"  # the fixture's history doesn't age out
    for space in raw["sources"]["confluence"]:
        space.update(base_url=f"{_SITE}/wiki", user_env="CF_USER", token_env="CF_TOKEN")
    for source in raw["sources"]["git"]:
        source["token_env"] = "GH_TOKEN"
    path = tmp_path / "system.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


_ENV = {"CF_USER": "ada@acme.test", "CF_TOKEN": "cf-fake", "GH_TOKEN": "gh-fake"}


@pytest.mark.parametrize(
    ("name", "org"), [("system-a", "acme-payments"), ("system-b", "acme-analytics")]
)
def test_the_round_trip_ingests_a_system_detection_reads_the_same(
    tmp_path: Path, name: str, org: str
) -> None:
    system = ONBOARDING_DIR / name
    sn = _fake_servicenow(system, extra_text=" Contact jane.doe@acme.test about this.")
    transports = IngestTransports(
        servicenow=SyncASGITransport(sn.app),
        confluence=_fake_confluence(system),
        github=_fake_github(system, org),
    )
    out = tmp_path / "workspace"
    reports = ingest_system(
        _shadow_config(), _manifest(system, tmp_path), out, transports=transports, env=_ENV
    )
    assert {r.status for r in reports} == {"read"}, [r.line() for r in reports]

    lab_incidents = yaml.safe_load((system / "sources/servicenow/incidents.yaml").read_text())
    got = yaml.safe_load((out / "sources/servicenow/incidents.yaml").read_text())
    assert len(got) == len(lab_incidents)
    assert {i["number"] for i in got} == {i["number"] for i in lab_incidents}
    assert "jane.doe@acme.test" not in (out / "sources/servicenow/incidents.yaml").read_text()
    lab_pages = sorted(p.name for p in (system / "sources/confluence").rglob("*.md"))
    assert len(list((out / "sources/confluence").rglob("*.md"))) == len(lab_pages)
    lab_files = {
        p.relative_to(system / "sources/git")
        for p in (system / "sources/git").rglob("*")
        if p.is_file()
    }
    got_files = {
        p.relative_to(out / "sources/git") for p in (out / "sources/git").rglob("*") if p.is_file()
    }
    assert got_files == lab_files

    plugins = load_plugins()
    from_lab = sorted(d.plugin for d in detect_plugins(system, plugins) if d.detected)
    from_ingest = sorted(d.plugin for d in detect_plugins(out, plugins) if d.detected)
    assert from_ingest == from_lab


def test_ingest_resumes_and_refreshes(tmp_path: Path) -> None:
    system = ONBOARDING_DIR / "system-a"
    sn = _fake_servicenow(system)
    transports = IngestTransports(
        servicenow=SyncASGITransport(sn.app),
        confluence=_fake_confluence(system),
        github=_fake_github(system, "acme-payments"),
    )
    manifest = _manifest(system, tmp_path)
    out = tmp_path / "ws"
    first = ingest_system(_shadow_config(), manifest, out, transports=transports, env=_ENV)
    again = ingest_system(_shadow_config(), manifest, out, transports=transports, env=_ENV)
    assert {r.status for r in first} == {"read"}
    assert {r.status for r in again} == {"resumed"}
    assert again[0].counts == first[0].counts  # the recorded counts come back
    state = json.loads((out / "ingest.json").read_text())
    assert all(v["done"] for v in state.values())
    fresh = ingest_system(
        _shadow_config(), manifest, out, refresh=True, transports=transports, env=_ENV
    )
    assert {r.status for r in fresh} == {"read"}


def test_the_incident_budget_is_reported_when_reached(tmp_path: Path) -> None:
    system = ONBOARDING_DIR / "system-a"
    raw = yaml.safe_load(_manifest(system, tmp_path).read_text())
    raw["budget"] = {"max_incidents": 10}
    raw["sources"]["confluence"] = []
    raw["sources"]["git"] = []
    manifest = tmp_path / "small.yaml"
    manifest.write_text(yaml.safe_dump(raw))
    sn = _fake_servicenow(system)
    [report] = ingest_system(
        _shadow_config(),
        manifest,
        tmp_path / "ws",
        transports=IngestTransports(servicenow=SyncASGITransport(sn.app)),
        env=_ENV,
    )
    assert report.counts["incidents"] == 10 and report.truncated
    assert "budget reached" in report.line()


def test_without_a_servicenow_connection_the_other_sources_still_run(tmp_path: Path) -> None:
    system = ONBOARDING_DIR / "system-b"
    reports = ingest_system(
        None,
        _manifest(system, tmp_path),
        tmp_path / "ws",
        transports=IngestTransports(
            confluence=_fake_confluence(system), github=_fake_github(system, "acme-analytics")
        ),
        env=_ENV,
    )
    assert [r.status for r in reports] == ["unavailable", "read", "read", "read"]


def test_a_missing_confluence_credential_names_the_variable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="CF_USER"):
        ingest_system(
            None, _manifest(ONBOARDING_DIR / "system-a", tmp_path), tmp_path / "ws", env={}
        )


# --- the readers ------------------------------------------------------------------------


def test_path_globs_match_at_the_repository_root_too() -> None:
    assert path_matches("pom.xml", ["**/pom.xml"])
    assert path_matches("svc/src/main/resources/application.yml", ["**/application*.yml"])
    assert not path_matches("src/Main.java", ["**/pom.xml", "**/helm/**"])


def test_local_checkouts_are_read_with_the_same_budgets(tmp_path: Path) -> None:
    repo = tmp_path / "checkouts" / "svc-a"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / "pom.xml").write_text("<project>spring-boot</project>")
    (repo / "big.yml").write_text("x" * 5000)
    (repo / "bin.yml").write_bytes(b"\0\1\2")
    found = IngestedFiles()
    read_local(
        str(tmp_path / "checkouts" / "svc-*"),
        ["**/*.xml", "**/*.yml", "**/config"],
        max_files=10,
        max_file_bytes=1000,
        into=found,
    )
    assert found.repos == ["svc-a"]
    assert set(found.files) == {"svc-a/pom.xml"}  # .git/ never read
    assert found.skipped == {"svc-a/big.yml": "larger than 1000 bytes", "svc-a/bin.yml": "binary"}
    with pytest.raises(ConfigError, match="no directory matches"):
        read_local(str(tmp_path / "nope-*"), ["**/*"], max_files=1, max_file_bytes=1, into=found)


def test_github_falls_back_to_a_user_and_filters_repos_by_glob() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.headers["Authorization"] == "Bearer t"
        if path == "/orgs/ada/repos":
            return httpx.Response(404)
        if path == "/users/ada/repos":
            return httpx.Response(
                200, json=[{"name": "pay-api", "default_branch": "dev"}, {"name": "blog"}]
            )
        if path == "/repos/ada/pay-api/git/trees/dev":
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "pom.xml", "type": "blob", "size": 10},
                        {"path": "docs", "type": "tree"},
                        {"path": "huge.yml", "type": "blob", "size": 10_000_000},
                    ]
                },
            )
        if path == "/repos/ada/pay-api/contents/pom.xml":
            return httpx.Response(200, json={"content": base64.b64encode(b"<project/>").decode()})
        return httpx.Response(404)

    reader = GitHubReader(token="t", transport=httpx.MockTransport(handler))
    found = IngestedFiles()
    reader.read(
        "ada/pay-*", ["**/pom.xml", "**/*.yml"], max_files=10, max_file_bytes=1000, into=found
    )
    assert found.repos == ["pay-api"]
    assert found.files == {"pay-api/pom.xml": "<project/>"}
    assert found.skipped == {"pay-api/huge.yml": "larger than 1000 bytes"}


def test_the_workspace_refuses_paths_outside_itself(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    ws.prepare("system: {}\n")
    with pytest.raises(ConfigError, match="outside the workspace"):
        ws.replace_repos(["r"], {"r/../../escape.txt": "x"})
    with pytest.raises(ConfigError, match="outside the workspace"):
        ws.replace_repos(["r"], {"/etc/passwd": "x"})
