"""ADR-0019 (WP-O5b): an approved request's credential is bound only once proven read-only.

The done-when: a fake Oracle, MongoDB and RabbitMQ, each with a read-only and
a read-write account, bind the first and refuse the second. The fakes answer
the way the real systems do (their privilege views, ``connectionStatus``, the
management API's status codes), so the verifiers' own logic is what's tested.
"""

from __future__ import annotations

import base64
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from supportagent.adapters.crypto.vault import FileCredentialVault
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.events import RecordingEventSink
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.tools.access import (
    AccessRefusedError,
    MongoAccessVerifier,
    OracleAccessVerifier,
    RabbitAccessVerifier,
)
from supportagent.app.access_binding import AccessBinding
from supportagent.app.onboarding_audit import history
from supportagent.app.request_review import RequestReview, request_id
from supportagent.bootstrap import (
    _event_sink,
    build_access_binding,
    build_tool_registry,
    load_plugins,
)
from supportagent.config.loader import load_config_offline
from supportagent.config.schema import AppConfig
from supportagent.domain.access_verify import ProbeResult, judge, redact
from supportagent.domain.errors import ConfigError, DomainValidationError, ToolError
from supportagent.domain.gap_plan import AccessRequest, GapPlan
from supportagent.ports.access import AccessVerifier
from supportagent.ports.credentials import StoredCredential
from tests.unit.api_support import ADA, TENANT, login, make_client

pytestmark = pytest.mark.unit

_LAB = Path(__file__).resolve().parents[2] / "evals/lab/lab.yaml"

# Fake credentials (fixtures only): low-entropy, never real.
ORA_RO = "diag/ro-pass-1@db.example:1521/PAY"
ORA_RW = "app/rw-pass-1@db.example:1521/PAY"
MONGO_RO = "mongodb://monitor:ro-pass-1@mongo.example:27017/orders"
MONGO_RW = "mongodb://app:rw-pass-1@mongo.example:27017/orders"
RABBIT_RO = "https://monitor:ro-pass-1@rabbit.example:15672"
RABBIT_RW = "https://app:rw-pass-1@rabbit.example:15672"


# --- the fakes ----------------------------------------------------------------------


class FakeOracle:
    """Each account's privileges, as ``SESSION_PRIVS``, ``ALL_TAB_PRIVS`` and
    ``USER_OBJECTS`` report them."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {
            ORA_RO: {
                "system": ["CREATE SESSION", "SELECT ANY DICTIONARY"],
                "objects": [],
                "owned": {},
            },
            ORA_RW: {
                "system": ["CREATE SESSION", "CREATE TABLE"],
                "objects": ["INSERT", "UPDATE", "DELETE"],
                "owned": {"TABLE": 3, "INDEX": 4},
            },
        }
        self.sql: list[str] = []

    def reader(self, dsns: Mapping[str, str]) -> _OracleSession:
        return _OracleSession(self, dict(dsns))


class _OracleSession:
    def __init__(self, oracle: FakeOracle, dsns: dict[str, str]) -> None:
        self._oracle = oracle
        self._dsns = dsns

    def query(self, database: str, sql: str, *, timeout_s: float) -> list[dict[str, object]]:
        self._oracle.sql.append(sql)
        if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
            raise ToolError("oracle: only SELECT statements are allowed")  # noqa: TRY003
        account = self._oracle.accounts.get(self._dsns[database])
        if account is None:
            raise ToolError(f"oracle {database}: DatabaseError: ORA-01017: invalid credential")  # noqa: TRY003
        if "session_privs" in sql:
            return [{"privilege": p} for p in account["system"]]
        if "all_tab_privs" in sql:
            return [{"privilege": p} for p in account["objects"]]
        if "user_objects" in sql:
            return [{"object_type": k, "n": v} for k, v in account["owned"].items()]
        if "V$SESSION" in sql:
            return [{"count(*)": 4}]
        raise ToolError(f"oracle {database}: ORA-00942: table or view does not exist")  # noqa: TRY003


_CLUSTER_MONITOR = {"resource": {"cluster": True}, "actions": ["inprog", "serverStatus"]}
_READ = {"resource": {"db": "orders", "collection": ""}, "actions": ["find", "listCollections"]}
_READ_WRITE = {
    "resource": {"db": "orders", "collection": ""},
    "actions": ["find", "insert", "update", "remove", "createIndex", "dropDatabase"],
}
#: The privilege action each command the verifier sends needs.
_MONGO_NEEDS = {"currentOp": "inprog", "serverStatus": "serverStatus", "find": "find"}


class FakeMongo:
    def __init__(self) -> None:
        self.users: dict[str, list[dict[str, Any]]] = {
            MONGO_RO: [_CLUSTER_MONITOR, _READ],
            MONGO_RW: [_CLUSTER_MONITOR, _READ_WRITE],
        }
        self.unauthenticated = False
        self.closed = 0

    def connect(self, uri: str) -> _MongoConn:
        return _MongoConn(self, uri)


class _MongoConn:
    def __init__(self, mongo: FakeMongo, uri: str) -> None:
        self._mongo = mongo
        self._uri = uri

    def command(
        self, database: str, command: Mapping[str, object], *, timeout_s: float
    ) -> Mapping[str, object]:
        privileges = self._mongo.users.get(self._uri)
        if privileges is None:
            raise AccessRefusedError("mongodb: 18 AuthenticationFailed")  # noqa: TRY003
        name = next(iter(command))
        if name == "connectionStatus":
            users = [] if self._mongo.unauthenticated else [{"user": "u", "db": "admin"}]
            return {
                "authInfo": {"authenticatedUsers": users, "authenticatedUserPrivileges": privileges}
            }
        need = _MONGO_NEEDS[name]
        if any(need in p["actions"] for p in privileges):
            return {"ok": 1}
        raise AccessRefusedError(f"mongodb: 13 not authorized on {database} to execute {name}")  # noqa: TRY003

    def close(self) -> None:
        self._mongo.closed += 1


class FakeRabbit:
    """The management API: basic auth; vhost permissions decide writes; a missing queue
    is *not found* only to a user the vhost lets in."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {
            "monitor": {"password": "ro-pass-1", "tags": ["monitoring"], "vhosts": set()},
            "app": {"password": "rw-pass-1", "tags": [], "vhosts": {"/", "orders"}},
        }
        self.calls: list[tuple[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        raw = request.url.raw_path.decode().split("?", 1)[0]
        parts = [unquote(p) for p in raw.split("/")[1:]]
        self.calls.append((request.method, raw))
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        name, _, password = (
            base64.b64decode(token).decode().partition(":") if token else ("", "", "")
        )
        user = self.users.get(name)
        if scheme != "Basic" or user is None or user["password"] != password:
            return httpx.Response(401, json={"error": "not_authorised"})
        match request.method, parts:
            case "GET", ["api", "whoami"]:
                return httpx.Response(200, json={"name": name, "tags": user["tags"]})
            case "GET", ["api", "vhosts"]:
                return httpx.Response(200, json=[{"name": "/"}, {"name": "orders"}])
            case "GET", ["api", "queues"]:
                return httpx.Response(200, json=[])
            case "GET", ["api", "queues", _vhost, _queue]:
                return httpx.Response(404, json={"error": "Object Not Found"})
            case "POST", ["api", "exchanges", vhost, "amq.default", "publish"]:
                if vhost in user["vhosts"]:
                    return httpx.Response(200, json={"routed": False})
                return httpx.Response(401, json={"error": "not_authorised"})
            case "DELETE", ["api", "queues", vhost, _queue, *_rest]:
                if vhost in user["vhosts"]:
                    return httpx.Response(404, json={"error": "Object Not Found"})
                return httpx.Response(401, json={"error": "not_authorised"})
        return httpx.Response(404)


@pytest.fixture
def oracle() -> FakeOracle:
    return FakeOracle()


@pytest.fixture
def mongo() -> FakeMongo:
    return FakeMongo()


@pytest.fixture
def rabbit() -> FakeRabbit:
    return FakeRabbit()


@pytest.fixture
def verifiers(
    oracle: FakeOracle, mongo: FakeMongo, rabbit: FakeRabbit
) -> dict[str, AccessVerifier]:
    return {
        "oracle": OracleAccessVerifier(reader=oracle.reader),
        "mongodb": MongoAccessVerifier(connect=mongo.connect),
        "rabbitmq": RabbitAccessVerifier(transport=rabbit.transport(), probe_name=lambda: "p1"),
    }


def _contract(plugin: str, access: str) -> Any:
    manifest = next(p.manifest for p in load_plugins() if p.manifest.id == plugin)
    return manifest.requires[access]


def _request(plugin: str, access: str, target: str) -> AccessRequest:
    c = _contract(plugin, access)
    return AccessRequest(
        plugin=plugin,
        access=access,
        target=target,
        found_in="graph node",
        scope=c.grant,
        description=c.description,
        verify_can=tuple(c.verify_can),
        verify_cannot=tuple(c.verify_cannot),
        capabilities=(),
        unlocks=("cls",),
        share=0.2,
        p1=0,
    )


_REQUESTS = [
    ("oracle", "oracle_ro", "paydb"),
    ("mongodb", "mongodb_ro", "orders"),
    ("rabbitmq", "rabbitmq_ro", "events"),
    ("github", "github_ro", "acme/*"),
]


def _config(tmp_path: Path) -> AppConfig:
    config = load_config_offline(_LAB)
    vault = str(tmp_path / "vault" / "credentials.vault")
    return config.model_copy(
        update={"onboarding": config.onboarding.model_copy(update={"credentials_vault": vault})}
    )


@pytest.fixture
def binding(  # noqa: PLR0913, PLR0917
    tmp_path: Path,
    db: Any,
    clock: Any,
    ids: Any,
    master_key: bytes,
    verifiers: dict[str, AccessVerifier],
) -> AccessBinding:
    review = RequestReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)
    review.save(GapPlan(tuple(_request(*r) for r in _REQUESTS), (), 0.8))
    for r in _REQUESTS:
        review.decide(request_id(*r), "approved", actor="user_ada")
    return build_access_binding(
        _config(tmp_path),
        engine=db,
        master_key=master_key,
        clock=clock,
        ids=ids,
        verifiers=verifiers,
    )


def _vault(tmp_path: Path, master_key: bytes) -> FileCredentialVault:
    return FileCredentialVault(
        tmp_path / "vault" / "credentials.vault", master_key=master_key, tenant_id=TENANT
    )


# --- the verdict ----------------------------------------------------------------------


def test_bound_only_when_every_read_works_and_every_write_is_refused() -> None:
    can, cannot = ["read"], ["insert", "ddl"]

    def results(*outcomes: str) -> list[ProbeResult]:
        kinds = ["can", "cannot", "cannot"]
        probes = ["read", "insert", "ddl"]
        return [ProbeResult(k, p, o, "d") for k, p, o in zip(kinds, probes, outcomes, strict=True)]  # type: ignore[arg-type]

    assert judge(results("allowed", "refused", "refused"), can=can, cannot=cannot).bound
    assert "it can't read" in str(
        judge(results("refused", "refused", "refused"), can=can, cannot=cannot).why
    )
    assert "it can insert" in str(
        judge(results("allowed", "allowed", "refused"), can=can, cannot=cannot).why
    )
    # An error is never taken for a refusal: fail closed.
    assert "couldn't prove it can't ddl" in str(
        judge(results("allowed", "refused", "error"), can=can, cannot=cannot).why
    )
    assert "wasn't probed" in str(
        judge(results("allowed", "refused", "refused")[:2], can=can, cannot=cannot).why
    )


def test_details_never_carry_the_credential_or_its_password() -> None:
    assert redact(f"failed for {ORA_RW}", ORA_RW) == "failed for ***"
    assert "rw-pass-1" not in redact("ORA-01017 for app/rw-pass-1", ORA_RW)
    said = redact("GET https://app:rw-pass-1@rabbit.example:15672/api -> 401", RABBIT_RW)
    assert "rw-pass-1" not in said and "app:" not in said


# --- each verifier, against its fake -------------------------------------------------


@pytest.mark.parametrize(
    ("plugin", "access", "target", "ro", "rw", "write"),
    [
        ("oracle", "oracle_ro", "paydb", ORA_RO, ORA_RW, "insert"),
        ("mongodb", "mongodb_ro", "orders", MONGO_RO, MONGO_RW, "insert"),
        ("rabbitmq", "rabbitmq_ro", "events", RABBIT_RO, RABBIT_RW, "publish a message"),
    ],
)
def test_each_verifier_proves_the_read_only_account_and_catches_the_writer(  # noqa: PLR0913, PLR0917
    verifiers: dict[str, AccessVerifier],
    plugin: str,
    access: str,
    target: str,
    ro: str,
    rw: str,
    write: str,
) -> None:
    contract = _contract(plugin, access)
    verifier = verifiers[str(contract.verifier)]

    def run(credential: str) -> list[ProbeResult]:
        return verifier.verify(
            credential, target, can=contract.verify_can, cannot=contract.verify_cannot, timeout_s=5
        )

    read_only = run(ro)
    # Every probe the shipped contract names is one its verifier knows.
    assert [(r.kind, r.outcome) for r in read_only] == [("can", "allowed")] * len(
        contract.verify_can
    ) + [("cannot", "refused")] * len(contract.verify_cannot)
    writer = {r.probe: r for r in run(rw)}
    assert writer[write].outcome == "allowed"
    assert all(writer[p].outcome == "allowed" for p in contract.verify_can)


def test_the_oracle_probes_only_read(
    oracle: FakeOracle, verifiers: dict[str, AccessVerifier]
) -> None:
    contract = _contract("oracle", "oracle_ro")
    verifiers["oracle"].verify(
        ORA_RW, "paydb", can=contract.verify_can, cannot=contract.verify_cannot, timeout_s=5
    )
    assert oracle.sql and all(re.match(r"^\s*SELECT\b", s, re.IGNORECASE) for s in oracle.sql)


def test_a_mongodb_without_authentication_lets_anyone_write(
    mongo: FakeMongo, verifiers: dict[str, AccessVerifier]
) -> None:
    mongo.unauthenticated = True
    out = verifiers["mongodb"].verify(MONGO_RO, "orders", can=[], cannot=["insert"], timeout_s=5)
    assert out[0].outcome == "allowed" and "without authentication" in out[0].detail
    assert mongo.closed == 1


def test_rabbitmq_writes_touch_no_real_queue(
    rabbit: FakeRabbit, verifiers: dict[str, AccessVerifier]
) -> None:
    contract = _contract("rabbitmq", "rabbitmq_ro")
    verifiers["rabbitmq"].verify(
        RABBIT_RW, "events", can=contract.verify_can, cannot=contract.verify_cannot, timeout_s=5
    )
    writes = [(m, p) for m, p in rabbit.calls if m in ("POST", "DELETE")]
    assert writes and all("supportagent-verify-p1" in p or "amq.default" in p for _, p in writes)


def test_an_administrator_tag_counts_as_every_write(
    rabbit: FakeRabbit, verifiers: dict[str, AccessVerifier]
) -> None:
    rabbit.users["monitor"]["tags"] = ["monitoring", "administrator"]
    out = verifiers["rabbitmq"].verify(
        RABBIT_RO, "x", can=[], cannot=["delete a queue"], timeout_s=5
    )
    assert out[0].outcome == "allowed" and "administrator" in out[0].detail


def test_an_unknown_probe_is_an_error_not_a_refusal(verifiers: dict[str, AccessVerifier]) -> None:
    for name, credential in (("oracle", ORA_RO), ("mongodb", MONGO_RO), ("rabbitmq", RABBIT_RO)):
        out = verifiers[name].verify(
            credential, "t", can=[], cannot=["truncate everything"], timeout_s=5
        )
        assert out[0].outcome == "error", name


# --- binding, end to end --------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_key", "ro", "rw", "why"),
    [
        (_REQUESTS[0], ORA_RO, ORA_RW, "it can insert"),
        (_REQUESTS[1], MONGO_RO, MONGO_RW, "it can insert"),
        (_REQUESTS[2], RABBIT_RO, RABBIT_RW, "it can publish a message"),
    ],
)
def test_the_read_only_account_binds_and_the_read_write_one_is_refused(  # noqa: PLR0913, PLR0917
    binding: AccessBinding,
    tmp_path: Path,
    master_key: bytes,
    db: Any,
    clock: Any,
    request_key: tuple[str, str, str],
    ro: str,
    rw: str,
    why: str,
) -> None:
    rid = request_id(*request_key)
    refused = binding.bind(rid, rw, actor="user_ada")
    assert not refused.bound and why in str(refused.why)
    assert refused.request.status == "open" and why in str(refused.request.reopened_why)
    assert _vault(tmp_path, master_key).get(rid) is None  # nothing stored

    RequestReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT).decide(
        rid, "approved", actor="user_ada"
    )
    bound = binding.bind(rid, ro, actor="user_ada")
    assert bound.bound and bound.request.status == "bound"
    stored = _vault(tmp_path, master_key).get(rid)
    assert stored is not None and stored.secret == ro and stored.target == request_key[2]
    probes: Any = (bound.request.verification or {})["probes"]
    assert {p["outcome"] for p in probes if p["kind"] == "cannot"} == {"refused"}


def test_every_probe_is_recorded_and_no_secret_reaches_the_database(
    binding: AccessBinding, db: Any, master_key: bytes, clock: Any
) -> None:
    rid = request_id(*_REQUESTS[0])
    binding.bind(rid, ORA_RW, actor="user_ada")
    RequestReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT).decide(
        rid, "approved", actor="user_ada"
    )
    clock.advance(60)
    binding.bind(rid, ORA_RO, actor="user_ada")
    uow = SqlUnitOfWork(db)
    with uow:
        events = history(_event_sink(uow, master_key, clock), TENANT)
    verified = [e for e in events if e["action"] == "access_verified"]
    assert [e["bound"] for e in verified] == [True, False]  # newest first
    assert all(len(e["probes"]) == 5 for e in verified)  # type: ignore[arg-type]
    with db.connect() as conn:
        dump = " ".join(
            str(v)
            for table in ("onboarding_requests", "events")
            for row in conn.execute(sa.text(f"SELECT * FROM {table}"))  # noqa: S608
            for v in row
        )
    assert "ro-pass-1" not in dump and "rw-pass-1" not in dump


def test_a_contract_without_a_verifier_is_never_bound(
    binding: AccessBinding, tmp_path: Path, master_key: bytes
) -> None:
    rid = request_id(*_REQUESTS[3])
    out = binding.bind(rid, "ghp_fake_token_value", actor="user_ada")
    assert not out.bound and "names no verifier" in str(out.why)
    assert _vault(tmp_path, master_key).all() == []


def test_a_bound_request_must_be_unbound_before_it_changes(
    binding: AccessBinding, db: Any, clock: Any, tmp_path: Path, master_key: bytes
) -> None:
    rid = request_id(*_REQUESTS[1])
    binding.bind(rid, MONGO_RO, actor="user_ada")
    review = RequestReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)
    with pytest.raises(DomainValidationError, match="unbind it first"):
        review.decide(rid, "declined", actor="user_ada")
    with pytest.raises(DomainValidationError, match="bound already"):
        binding.bind(rid, MONGO_RO, actor="user_ada")
    assert binding.unbind(rid, actor="user_ada").status == "approved"
    assert _vault(tmp_path, master_key).get(rid) is None
    assert review.progress()["bound"] == 0


def test_reverify_unbinds_a_credential_that_can_write_now(
    binding: AccessBinding, oracle: FakeOracle, tmp_path: Path, master_key: bytes
) -> None:
    ora, mongo = request_id(*_REQUESTS[0]), request_id(*_REQUESTS[1])
    binding.bind(ora, ORA_RO, actor="user_ada")
    binding.bind(mongo, MONGO_RO, actor="user_ada")
    oracle.accounts[ORA_RO]["system"].append("INSERT ANY TABLE")  # a DBA widened it
    outcomes = {o.request.id: o for o in binding.reverify()}
    assert outcomes[mongo].bound and outcomes[mongo].request.decided_by == "user_ada"
    assert not outcomes[ora].bound and "INSERT ANY TABLE" in str(outcomes[ora].why)
    assert outcomes[ora].request.status == "open"
    assert [c.request for c in _vault(tmp_path, master_key).all()] == [mongo]


# --- the vault ------------------------------------------------------------------------


def _cred(rid: str = "r1", secret: str = ORA_RO) -> StoredCredential:
    return StoredCredential(
        request=rid,
        plugin="oracle",
        access="oracle_ro",
        target="paydb",
        backend="first_party:oracle_ro",
        bound_ts=1,
        secret=secret,
    )


def test_the_vault_is_sealed_owner_only_and_opens_only_with_its_key(
    tmp_path: Path, master_key: bytes
) -> None:
    vault = FileCredentialVault(tmp_path / "v", master_key=master_key, tenant_id=TENANT)
    vault.put(_cred())
    vault.put(_cred("r2", MONGO_RO))
    assert [c.request for c in vault.all()] == ["r1", "r2"]
    assert "ro-pass-1" not in repr(vault.get("r1"))
    data = (tmp_path / "v").read_bytes()
    assert b"ro-pass-1" not in data and b"paydb" not in data
    assert stat.S_IMODE((tmp_path / "v").stat().st_mode) == 0o600
    with pytest.raises(ConfigError, match="doesn't open"):
        FileCredentialVault(tmp_path / "v", master_key=b"x" * 32, tenant_id=TENANT).all()
    with pytest.raises(ConfigError, match="doesn't open"):
        FileCredentialVault(tmp_path / "v", master_key=master_key, tenant_id="other").all()
    assert vault.remove("r1") and not vault.remove("r1")
    assert [c.request for c in vault.all()] == ["r2"]


# --- what a bound credential changes ---------------------------------------------------


def test_a_bound_oracle_credential_serves_the_oracle_tools(clock: Any, ids: Any) -> None:
    config = load_config_offline(_LAB)
    assert config.tools.oracle is None

    def names(credentials: Mapping[str, Mapping[str, str]] | None) -> frozenset[str]:
        return build_tool_registry(
            config, events=RecordingEventSink(), ids=ids, clock=clock, credentials=credentials
        ).names

    assert "oracle.sessions" not in names(None)
    assert "oracle.sessions" in names({"first_party:oracle_ro": {"paydb": ORA_RO}})


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


def test_the_requests_page_shows_bound_and_refused_credentials(  # noqa: PLR0913, PLR0917
    binding: AccessBinding,
    db: Any,
    clock: Any,
    ids: Any,
    master_key: bytes,
    oidc: FakeOIDC,
) -> None:
    binding.bind(request_id(*_REQUESTS[0]), ORA_RO, actor="user_ada")
    binding.bind(request_id(*_REQUESTS[2]), RABBIT_RW, actor="user_ada")
    client: TestClient = make_client(db, clock, ids, master_key, oidc)
    login(client, oidc, ADA)
    page = client.get("/ui/onboarding/requests").text
    assert "proven read-only, in the credential vault" in page
    assert "Its credential was refused, and nothing was stored" in page
    assert "it can publish a message" in page
    assert f"supportagent onboard bind {request_id(*_REQUESTS[1])}" in page  # approved, waiting
    assert "rw-pass-1" not in page and "ro-pass-1" not in page
    activity = client.get("/ui/onboarding").text
    assert "bound, proven read-only" in activity
