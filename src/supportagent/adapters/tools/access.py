"""Read-only proofs for a candidate credential (ADR-0019, WP-O5b): Oracle, MongoDB, RabbitMQ.

Each verifier runs an access contract's probes against the target with the
credential being bound (``ports.access``). Nothing here writes:

- a ``can`` probe is the read itself (a ``SELECT``, a command, a ``GET``);
- a ``cannot`` probe asks the system what the credential may do (Oracle's
  ``SESSION_PRIVS``, MongoDB's ``connectionStatus``), or makes an operation
  that changes nothing (RabbitMQ: a publish routed to no queue, a purge of a
  queue that doesn't exist, refused or not before it is looked up).

An unknown probe, or a system that can't be asked, is an ``error``: the
verdict (``domain.access_verify``) never takes it for a refusal.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from supportagent.adapters.tools.oracle import OracledbReader, OracleReader
from supportagent.domain.access_verify import ProbeOutcome, ProbeResult
from supportagent.domain.errors import ToolError
from supportagent.ports.access import AccessVerifier

_DETAIL = 300


def _cut(text: str) -> str:
    return text[:_DETAIL]


def _all(kind: str, probes: Sequence[str], outcome: ProbeOutcome, detail: str) -> list[ProbeResult]:
    return [ProbeResult(kind, p, outcome, detail) for p in probes]  # type: ignore[arg-type]


# --- Oracle ---------------------------------------------------------------------------

#: The account was refused: no such view for it (no grant), no privilege, bad login.
_ORA_REFUSED = re.compile(r"ORA-(00942|01031|01017|28000|01045)")
_SYSTEM_PRIVS = "SELECT privilege FROM session_privs"
_OBJECT_PRIVS = (
    "SELECT DISTINCT privilege FROM all_tab_privs WHERE grantee IN "
    "(SELECT role FROM session_roles UNION ALL SELECT USER FROM dual) "
    "AND privilege IN ('INSERT', 'UPDATE', 'DELETE', 'ALTER', 'INDEX')"
)
_OWNED = "SELECT object_type, count(*) AS n FROM user_objects GROUP BY object_type"
_DML = {"insert": "INSERT", "update": "UPDATE", "delete": "DELETE"}
_DDL_VERBS = frozenset({"CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "COMMENT"})
#: Session housekeeping, not schema changes.
_NOT_DDL = frozenset({"CREATE SESSION", "ALTER SESSION"})


@dataclass(frozen=True, slots=True)
class _OraclePrivileges:
    system: frozenset[str]
    objects: frozenset[str]
    owned: Mapping[str, int] = field(default_factory=dict)


class OracleAccessVerifier:
    """``verifier: oracle``. ``verify_can`` are ``SELECT`` statements; ``verify_cannot``
    are ``insert``, ``update``, ``delete`` and ``ddl``.

    A write is allowed when a system privilege (``INSERT ANY TABLE``, ``CREATE
    TABLE``), an object grant to the account or one of its roles, or an object it
    owns (it can write its own tables and drop what it owns) allows it. Grants to
    ``PUBLIC`` are every account's (Oracle's own ``PLAN_TABLE``), so they don't
    count against this one: they are the DBA's to audit.
    """

    name = "oracle"

    def __init__(
        self,
        reader: Callable[[Mapping[str, str]], OracleReader] = OracledbReader,
    ) -> None:
        self._reader = reader

    def verify(
        self,
        credential: str,
        target: str,
        *,
        can: Sequence[str],
        cannot: Sequence[str],
        timeout_s: float,
    ) -> list[ProbeResult]:
        reader = self._reader({target: credential})
        results: list[ProbeResult] = []
        for probe in can:
            try:
                reader.query(target, probe, timeout_s=timeout_s)
            except ToolError as exc:
                outcome: ProbeOutcome = "refused" if _ORA_REFUSED.search(str(exc)) else "error"
                results.append(ProbeResult("can", probe, outcome, _cut(str(exc))))
            else:
                results.append(ProbeResult("can", probe, "allowed"))
        if not cannot:
            return results
        try:
            privileges = _OraclePrivileges(
                system=frozenset(
                    str(r["privilege"])
                    for r in reader.query(target, _SYSTEM_PRIVS, timeout_s=timeout_s)
                ),
                objects=frozenset(
                    str(r["privilege"])
                    for r in reader.query(target, _OBJECT_PRIVS, timeout_s=timeout_s)
                ),
                owned={
                    str(r["object_type"]): int(str(r["n"]))
                    for r in reader.query(target, _OWNED, timeout_s=timeout_s)
                },
            )
        except ToolError as exc:
            return results + _all("cannot", cannot, "error", f"its privileges: {_cut(str(exc))}")
        return results + [_oracle_cannot(action, privileges) for action in cannot]


def _oracle_cannot(action: str, p: _OraclePrivileges) -> ProbeResult:  # noqa: PLR0911 — one per way a write is allowed
    def allowed(why: str) -> ProbeResult:
        return ProbeResult("cannot", action, "allowed", why)

    tables = p.owned.get("TABLE", 0)
    if (verb := _DML.get(action.lower())) is not None:
        if f"{verb} ANY TABLE" in p.system:
            return allowed(f"it has {verb} ANY TABLE")
        if verb in p.objects:
            return allowed(f"it, or a role it has, is granted {verb} on a table")
        if tables:
            return allowed(f"it owns {tables} table(s)")
        return ProbeResult("cannot", action, "refused", f"no privilege grants {verb}")
    if action.lower() == "ddl":
        ddl = sorted(s for s in p.system if s.split(" ", 1)[0] in _DDL_VERBS and s not in _NOT_DDL)
        if ddl:
            return allowed(f"it has {', '.join(ddl[:3])}")
        if granted := sorted(p.objects & {"ALTER", "INDEX"}):
            return allowed(f"it is granted {', '.join(granted)} on a table")
        if owned := sum(p.owned.values()):
            return allowed(f"it owns {owned} object(s), which it can drop")
        return ProbeResult("cannot", action, "refused", "no privilege grants DDL")
    return ProbeResult("cannot", action, "error", f"the oracle verifier has no probe {action!r}")


# --- MongoDB --------------------------------------------------------------------------


class AccessRefusedError(ToolError):
    """The system refused the credential or the operation (not unreachable)."""


class MongoCommands(Protocol):
    def command(
        self, database: str, command: Mapping[str, object], *, timeout_s: float
    ) -> Mapping[str, object]: ...

    def close(self) -> None: ...


#: Mongo's ``Unauthorized`` and ``AuthenticationFailed``.
_MONGO_REFUSED = frozenset({13, 18})


class PymongoCommands:
    """Commands over ``pymongo``. Not a core dependency: a deployment that binds
    MongoDB credentials installs it."""

    def __init__(self, uri: str) -> None:
        self._uri = uri
        self._client: Any = None

    def command(
        self, database: str, command: Mapping[str, object], *, timeout_s: float
    ) -> Mapping[str, object]:
        try:
            pymongo = importlib.import_module("pymongo")
        except ImportError as exc:
            raise ToolError("mongodb: pymongo is not installed") from exc  # noqa: TRY003
        errors = importlib.import_module("pymongo.errors")
        try:
            if self._client is None:
                ms = int(timeout_s * 1000)
                self._client = pymongo.MongoClient(
                    self._uri,
                    serverSelectionTimeoutMS=ms,
                    socketTimeoutMS=ms,
                    appname="supportagent-verify",
                )
            reply = self._client[database].command(dict(command))
        except errors.OperationFailure as exc:
            if exc.code in _MONGO_REFUSED:
                raise AccessRefusedError(
                    f"mongodb: {exc.code} {exc.details or ''}"[:_DETAIL]
                ) from exc
            raise ToolError(f"mongodb: {exc.code} {type(exc).__name__}") from exc  # noqa: TRY003
        except errors.PyMongoError as exc:
            raise ToolError(f"mongodb: {type(exc).__name__}") from exc  # noqa: TRY003
        return dict(reply)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


#: ``verify_can`` probes: the command each one names. ``None``: the target database.
_MONGO_CAN: Mapping[str, tuple[str | None, Mapping[str, object]]] = {
    "db.currentOp()": ("admin", {"currentOp": 1}),
    "db.serverStatus()": ("admin", {"serverStatus": 1}),
    "db.system.profile.find()": (None, {"find": "system.profile", "limit": 1}),
}
#: ``verify_cannot`` probes: the privilege actions any one of which allows it.
_MONGO_ACTIONS: Mapping[str, frozenset[str]] = {
    "insert": frozenset({"insert"}),
    "update": frozenset({"update"}),
    "delete": frozenset({"remove"}),
    "remove": frozenset({"remove"}),
    "createIndex": frozenset({"createIndex"}),
    "dropCollection": frozenset({"dropCollection"}),
    "dropDatabase": frozenset({"dropDatabase"}),
}


class MongoAccessVerifier:
    """``verifier: mongodb``. ``verify_can`` are the shell calls in ``_MONGO_CAN``;
    ``verify_cannot`` the actions in ``_MONGO_ACTIONS``, judged from the user's
    privileges as the server reports them (``connectionStatus``). The target is
    the database name."""

    name = "mongodb"

    def __init__(self, connect: Callable[[str], MongoCommands] = PymongoCommands) -> None:
        self._connect = connect

    def verify(
        self,
        credential: str,
        target: str,
        *,
        can: Sequence[str],
        cannot: Sequence[str],
        timeout_s: float,
    ) -> list[ProbeResult]:
        conn = self._connect(credential)
        try:
            results = [self._can(conn, target, probe, timeout_s) for probe in can]
            return results + self._cannot(conn, cannot, timeout_s)
        finally:
            conn.close()

    @staticmethod
    def _can(conn: MongoCommands, target: str, probe: str, timeout_s: float) -> ProbeResult:
        known = _MONGO_CAN.get(probe)
        if known is None:
            return ProbeResult(
                "can", probe, "error", f"the mongodb verifier has no probe {probe!r}"
            )
        database, command = known
        try:
            conn.command(database or target, command, timeout_s=timeout_s)
        except AccessRefusedError as exc:
            return ProbeResult("can", probe, "refused", _cut(str(exc)))
        except ToolError as exc:
            return ProbeResult("can", probe, "error", _cut(str(exc)))
        return ProbeResult("can", probe, "allowed")

    @staticmethod
    def _cannot(conn: MongoCommands, cannot: Sequence[str], timeout_s: float) -> list[ProbeResult]:
        if not cannot:
            return []
        try:
            status = conn.command(
                "admin", {"connectionStatus": 1, "showPrivileges": True}, timeout_s=timeout_s
            )
        except ToolError as exc:
            return _all("cannot", cannot, "error", f"its privileges: {_cut(str(exc))}")
        auth = status.get("authInfo")
        info = auth if isinstance(auth, Mapping) else {}
        if not info.get("authenticatedUsers"):
            return _all(
                "cannot",
                cannot,
                "allowed",
                "the server runs without authentication: any connection can write",
            )
        raw = info.get("authenticatedUserPrivileges")
        privileges = [p for p in raw if isinstance(p, Mapping)] if isinstance(raw, list) else []
        out: list[ProbeResult] = []
        for action in cannot:
            wanted = _MONGO_ACTIONS.get(action)
            if wanted is None:
                out.append(
                    ProbeResult(
                        "cannot", action, "error", f"the mongodb verifier has no probe {action!r}"
                    )
                )
                continue
            granting = next(
                (p for p in privileges if wanted & {str(a) for a in p.get("actions") or ()}),
                None,
            )
            if granting is None:
                out.append(ProbeResult("cannot", action, "refused", "no privilege grants it"))
            else:
                out.append(
                    ProbeResult(
                        "cannot", action, "allowed", f"granted on {granting.get('resource')}"
                    )
                )
        return out


# --- RabbitMQ -------------------------------------------------------------------------

_GET = re.compile(r"^GET (/api/\S*)$")
_REFUSED_HTTP = frozenset({401, 403})
_OK_HTTP = frozenset({200, 201, 204})
_WRITING_TAGS = ("administrator", "policymaker")
_RABBIT_ACTIONS = ("publish a message", "purge a queue", "delete a queue")


class RabbitAccessVerifier:
    """``verifier: rabbitmq``, over the management API. The credential is the API's
    URL with the user in it (``https://user:password@host:15672``).

    ``verify_can`` are ``GET /api/...`` reads. ``verify_cannot``, in each vhost:

    - **publish a message**: to the default exchange, routed to a queue name that
      exists nowhere, so an allowed publish delivers nothing;
    - **purge a queue**, **delete a queue**: of that missing queue. A refusal is
      the vhost refusing the user; *not found* means the user got into a vhost,
      which the contract grants no permission on, so it counts as allowed.

    A user tagged ``administrator`` or ``policymaker`` can change the broker's
    users or policies: every write counts as allowed.
    """

    name = "rabbitmq"

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        probe_name: Callable[[], str] = lambda: token_hex(8),
    ) -> None:
        self._transport = transport
        self._probe_name = probe_name

    def verify(
        self,
        credential: str,
        target: str,
        *,
        can: Sequence[str],
        cannot: Sequence[str],
        timeout_s: float,
    ) -> list[ProbeResult]:
        try:
            url = httpx.URL(credential)
        except httpx.InvalidURL:
            return _all("can", can, "error", "the credential isn't a URL") + _all(
                "cannot", cannot, "error", "the credential isn't a URL"
            )
        port = f":{url.port}" if url.port else ""
        base = f"{url.scheme}://{url.host}{port}{url.path.rstrip('/')}"
        with httpx.Client(
            base_url=base,
            auth=(url.username, url.password),
            transport=self._transport,
            timeout=timeout_s,
        ) as client:
            results = [_rabbit_can(client, probe) for probe in can]
            return results + self._cannot(client, cannot)

    def _cannot(self, client: httpx.Client, cannot: Sequence[str]) -> list[ProbeResult]:
        if not cannot:
            return []
        try:
            me = _json(client.get("/api/whoami"))
            listed = _json(client.get("/api/vhosts"))
            vhosts = (
                [str(v["name"]) for v in listed if isinstance(v, Mapping)]
                if isinstance(listed, list)
                else []
            )
            vhosts = vhosts or ["/"]
        except (httpx.HTTPError, ToolError, KeyError, TypeError) as exc:
            return _all("cannot", cannot, "error", f"its tags and vhosts: {_cut(str(exc))}")
        tags = me.get("tags", []) if isinstance(me, Mapping) else []
        tag_list = tags.split(",") if isinstance(tags, str) else [str(t) for t in tags]
        if writing := [t for t in _WRITING_TAGS if t in tag_list]:
            return _all("cannot", cannot, "allowed", f"it is tagged {writing[0]}")
        queue = f"supportagent-verify-{self._probe_name()}"
        out: list[ProbeResult] = []
        for action in cannot:
            if action not in _RABBIT_ACTIONS:
                out.append(
                    ProbeResult(
                        "cannot", action, "error", f"the rabbitmq verifier has no probe {action!r}"
                    )
                )
                continue
            out.append(_rabbit_cannot(client, action, vhosts, queue))
        return out


def _rabbit_can(client: httpx.Client, probe: str) -> ProbeResult:
    match = _GET.match(probe)
    if match is None:
        return ProbeResult("can", probe, "error", "a rabbitmq read is GET /api/...")
    try:
        status = client.get(match.group(1)).status_code
    except httpx.HTTPError as exc:
        return ProbeResult("can", probe, "error", type(exc).__name__)
    if status == 200:  # noqa: PLR2004
        return ProbeResult("can", probe, "allowed")
    outcome: ProbeOutcome = "refused" if status in _REFUSED_HTTP else "error"
    return ProbeResult("can", probe, outcome, f"HTTP {status}")


def _rabbit_cannot(
    client: httpx.Client, action: str, vhosts: Sequence[str], queue: str
) -> ProbeResult:
    refused: list[str] = []
    for vhost in vhosts:
        vh = quote(vhost, safe="")
        try:
            if action == "publish a message":
                status = client.post(
                    f"/api/exchanges/{vh}/amq.default/publish",
                    json={
                        "properties": {},
                        "routing_key": queue,
                        "payload": "",
                        "payload_encoding": "string",
                    },
                ).status_code
                if status in _OK_HTTP:
                    return ProbeResult(
                        "cannot",
                        action,
                        "allowed",
                        f"it can publish in vhost {vhost!r} (the probe's message reached no queue)",
                    )
            else:
                if client.get(f"/api/queues/{vh}/{queue}").status_code in _OK_HTTP:
                    return ProbeResult(
                        "cannot", action, "error", f"the probe queue {queue!r} exists in {vhost!r}"
                    )
                path = f"/api/queues/{vh}/{queue}" + (
                    "/contents" if action == "purge a queue" else ""
                )
                status = client.delete(path).status_code
                if status in _OK_HTTP or status == 404:  # noqa: PLR2004
                    return ProbeResult(
                        "cannot",
                        action,
                        "allowed",
                        f"it has access to vhost {vhost!r}, which the contract grants none on",
                    )
        except httpx.HTTPError as exc:
            return ProbeResult("cannot", action, "error", f"{vhost!r}: {type(exc).__name__}")
        if status not in _REFUSED_HTTP:
            return ProbeResult("cannot", action, "error", f"{vhost!r}: HTTP {status}")
        refused.append(vhost)
    return ProbeResult("cannot", action, "refused", f"refused in {len(refused)} vhost(s)")


def _json(resp: httpx.Response) -> object:
    if resp.status_code != 200:  # noqa: PLR2004
        raise ToolError(f"HTTP {resp.status_code}")  # noqa: TRY003
    return resp.json()


def access_verifiers(*, transport: httpx.BaseTransport | None = None) -> dict[str, AccessVerifier]:
    """The built-in verifiers, by the name a contract's ``verifier`` gives."""
    verifiers: list[AccessVerifier] = [
        OracleAccessVerifier(),
        MongoAccessVerifier(),
        RabbitAccessVerifier(transport=transport),
    ]
    return {v.name: v for v in verifiers}


__all__ = [
    "AccessRefusedError",
    "MongoAccessVerifier",
    "MongoCommands",
    "OracleAccessVerifier",
    "PymongoCommands",
    "RabbitAccessVerifier",
    "access_verifiers",
]
