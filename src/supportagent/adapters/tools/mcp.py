"""MCP as a tool backend (ADR-0011 §2, WP-M06; absorbs WP-204).

A deployment registers an MCP server and binds a capability to one of its
tools. ``McpHttpClient`` speaks the streamable-HTTP transport (JSON-RPC 2.0;
a reply is either ``application/json`` or an SSE stream) synchronously over
``httpx``. ``McpBoundTool`` is the ``Tool`` the registry sees: it carries the
**capability's** name, result schema and literal-argument keys, so a pack
check runs the same on the first-party adapter and on MCP.

The registration contract:

- **Declared tools only.** The client calls only the tools the operator
  declared for the server; anything else is refused before any request
  (``test_undeclared_mcp_tool_denied``). A declared tool the server doesn't
  list fails ``verify``.
- **Trust class at registration.** Class and tier come from the operator's
  config, never from the server. A server that annotates a tool as not
  read-only (or destructive) can't back a ``read`` registration.
- **Untrusted output.** A reply is data, never instructions: it is size
  capped, must parse into the capability's shape through a named *profile*
  (``passthrough`` or ``grafana.query_prometheus``), and is then validated
  against the capability schema by the registry; anything outside that shape
  is dropped. Values inside it (a label, a log line) are evidence: checks read
  them as typed fields, and they reach a model only as ``entail`` evidence
  inside the escaped untrusted block, where they can demote a claim but never
  make one SUPPORTED (``test_mcp_output_injection_canary_not_followed``).
- **Read-only by default.** Config accepts only ``read`` / T0 registrations
  until the action gate covers MCP tools (``config.schema.McpServer``).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from supportagent.adapters.tools.capabilities import Capability
from supportagent.adapters.tools.prometheus import render_query, vector_result
from supportagent.domain.errors import ConfigError, ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

PROTOCOL_VERSION = "2025-06-18"
_MAX_REPLY_BYTES = 1_000_000
_MAX_PAGES = 20
_RETRYABLE = {429, 500, 502, 503, 504}
_CLIENT_INFO = {"name": "supportagent", "version": "0.1"}


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    name: str
    read_only: bool | None
    destructive: bool | None


@dataclass(frozen=True, slots=True)
class McpReply:
    """A ``tools/call`` result: the text content joined, and ``structuredContent``."""

    text: str
    structured: Mapping[str, object] | None


class McpHttpClient:
    """One registered MCP server over streamable HTTP. Thread-safe; one session, lazily."""

    def __init__(
        self, *, server: str, url: str, client: httpx.Client, declared: frozenset[str]
    ) -> None:
        self.server = server
        self._url = url  # posted to as is: an httpx base_url would gain a trailing slash
        self.declared = declared
        self._client = client
        self._session: str | None = None
        self._ready = False
        self._next_id = 0
        self._lock = threading.Lock()
        self._verified: dict[TrustClass, tuple[str, ...]] = {}

    def registration_problems(
        self, trust_class: TrustClass, *, timeout_s: float
    ) -> tuple[str, ...]:
        """``verify_server``, once per class: checked before the first call, then cached."""
        if trust_class not in self._verified:
            self._verified[trust_class] = tuple(
                verify_server(self, trust_class, timeout_s=timeout_s)
            )
        return self._verified[trust_class]

    # -- the two calls the binding needs --------------------------------------------

    def list_tools(self, *, timeout_s: float) -> list[McpToolInfo]:
        out: list[McpToolInfo] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            result = self._call("tools/list", {"cursor": cursor} if cursor else {}, timeout_s)
            for raw in result.get("tools") or []:
                if isinstance(raw, dict) and isinstance(raw.get("name"), str):
                    notes_raw = raw.get("annotations")
                    notes = notes_raw if isinstance(notes_raw, dict) else {}
                    out.append(
                        McpToolInfo(
                            raw["name"],
                            _flag(notes, "readOnlyHint"),
                            _flag(notes, "destructiveHint"),
                        )
                    )
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return out

    def call_tool(
        self, name: str, arguments: Mapping[str, object], *, timeout_s: float
    ) -> McpReply:
        if name not in self.declared:
            raise ToolError(  # noqa: TRY003
                f"mcp {self.server}: tool {name!r} is not declared for this server (WP-M06)"
            )
        result = self._call("tools/call", {"name": name, "arguments": dict(arguments)}, timeout_s)
        texts = [
            str(part.get("text", ""))
            for part in result.get("content") or []
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = "\n".join(texts)
        if result.get("isError") is True:
            raise ToolError(f"mcp {self.server}.{name}: {text[:200]}")  # noqa: TRY003
        structured = result.get("structuredContent")
        return McpReply(text, structured if isinstance(structured, dict) else None)

    # -- JSON-RPC over streamable HTTP ---------------------------------------------

    def _call(self, method: str, params: Mapping[str, object], timeout_s: float) -> dict[str, Any]:
        with self._lock:
            if not self._ready:
                self._initialize(timeout_s)
            try:
                return self._rpc(method, params, timeout_s)
            except _SessionExpiredError:
                self._session, self._ready = None, False
                self._initialize(timeout_s)
                return self._rpc(method, params, timeout_s)

    def _initialize(self, timeout_s: float) -> None:
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        }
        self._rpc("initialize", params, timeout_s)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout_s)
        self._ready = True

    def _rpc(self, method: str, params: Mapping[str, object], timeout_s: float) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        resp = self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
            timeout_s,
        )
        if method == "initialize":
            self._session = resp.headers.get("mcp-session-id") or None
        message = _reply_for(resp, request_id, self.server)
        error = message.get("error")
        if isinstance(error, dict):
            raise ToolError(  # noqa: TRY003
                f"mcp {self.server}: {method} failed: {str(error.get('message', error))[:200]}"
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise ToolError(f"mcp {self.server}: {method} returned no result")  # noqa: TRY003
        return result

    def _post(self, body: Mapping[str, object], timeout_s: float) -> httpx.Response:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        try:
            resp = self._client.post(self._url, json=dict(body), headers=headers, timeout=timeout_s)
        except httpx.HTTPError as exc:
            raise ToolError(  # noqa: TRY003
                f"mcp {self.server}: {type(exc).__name__}", retryable=True
            ) from exc
        if resp.status_code == 404 and self._session and body.get("method") != "initialize":  # noqa: PLR2004
            raise _SessionExpiredError
        if resp.status_code >= 400:  # noqa: PLR2004
            raise ToolError(  # noqa: TRY003
                f"mcp {self.server}: HTTP {resp.status_code}",
                retryable=resp.status_code in _RETRYABLE,
            )
        if len(resp.content) > _MAX_REPLY_BYTES:
            raise ToolError(f"mcp {self.server}: reply over {_MAX_REPLY_BYTES} bytes")  # noqa: TRY003
        return resp


class _SessionExpiredError(Exception):
    """The server dropped the session (HTTP 404): initialize again, once."""


def _flag(notes: Mapping[str, object], key: str) -> bool | None:
    value = notes.get(key)
    return value if isinstance(value, bool) else None


def _reply_for(resp: httpx.Response, request_id: int, server: str) -> dict[str, Any]:
    """The JSON-RPC response to ``request_id``, from a JSON body or an SSE stream."""
    kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    messages: list[object] = []
    try:
        if kind == "text/event-stream":
            for event in resp.text.split("\n\n"):
                data = "\n".join(
                    line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                )
                if data:
                    messages.append(json.loads(data))
        else:
            body = resp.json()
            messages.extend(body if isinstance(body, list) else [body])
    except ValueError as exc:
        raise ToolError(f"mcp {server}: reply is not JSON-RPC") from exc  # noqa: TRY003
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise ToolError(f"mcp {server}: no reply to request {request_id}")  # noqa: TRY003


# --- profiles: capability args -> MCP args, MCP reply -> capability result ---------


@dataclass(frozen=True, slots=True)
class Profile:
    #: (bound capability args, the binding's fixed args) -> the MCP tool's arguments.
    to_args: Callable[[Mapping[str, object], Mapping[str, object]], dict[str, object]]
    #: (the MCP reply, the arguments sent) -> the capability result (still
    #: schema-checked by the registry).
    to_result: Callable[[McpReply, Mapping[str, object]], ToolResult]


def _passthrough_args(args: Mapping[str, object], fixed: Mapping[str, object]) -> dict[str, object]:
    return {**args, **fixed}


def _passthrough_result(reply: McpReply, _sent: Mapping[str, object]) -> ToolResult:
    """An MCP tool built to the capability's schema: structured content, or JSON text."""
    data: object = reply.structured
    if data is None:
        data = _json_text(reply)
    if not isinstance(data, dict):
        raise ToolError("mcp: passthrough reply is not a JSON object")  # noqa: TRY003
    return ToolResult(data)


def _grafana_prometheus_args(
    args: Mapping[str, object], fixed: Mapping[str, object]
) -> dict[str, object]:
    return {"queryType": "instant", "endTime": "now", **fixed, "expr": render_query(args)}


def _grafana_prometheus_result(reply: McpReply, sent: Mapping[str, object]) -> ToolResult:
    """``mcp-grafana`` ``query_prometheus`` (instant): ``{"data": [{metric, value}]}``."""
    structured = reply.structured or {}
    body = structured if "data" in structured else _json_text(reply)
    raw = body.get("data") if isinstance(body, dict) else body
    if not isinstance(raw, list):
        raise ToolError("mcp: query_prometheus reply has no vector")  # noqa: TRY003
    return vector_result(str(sent.get("expr", "")), raw)


def _json_text(reply: McpReply) -> object:
    try:
        return json.loads(reply.text)
    except ValueError as exc:
        raise ToolError("mcp: reply text is not JSON") from exc  # noqa: TRY003


PROFILES: Mapping[str, Profile] = {
    "passthrough": Profile(_passthrough_args, _passthrough_result),
    "grafana.query_prometheus": Profile(_grafana_prometheus_args, _grafana_prometheus_result),
}


# --- the bound tool ------------------------------------------------------------------


class McpBoundTool:
    """A capability served by a tool on a registered MCP server."""

    def __init__(  # noqa: PLR0913 — the binding's parts
        self,
        capability: Capability,
        *,
        client: McpHttpClient,
        tool: str,
        profile: str,
        fixed_args: Mapping[str, object],
        trust_class: TrustClass,
        tier: Tier,
    ) -> None:
        if tool not in client.declared:
            raise ConfigError(  # noqa: TRY003
                f"binding {capability.name}: tool {tool!r} is not declared for mcp server "
                f"{client.server!r}"
            )
        if profile not in PROFILES:
            raise ConfigError(f"binding {capability.name}: unknown profile {profile!r}")  # noqa: TRY003
        self.name = capability.name
        self.result_schema = capability.result_schema
        self.literal_args = capability.literal_args
        self.trust_class = trust_class
        self.tier = tier
        self.backend = f"mcp:{client.server}.{tool}"
        self._client = client
        self._tool = tool
        self._profile = PROFILES[profile]
        self._fixed = dict(fixed_args)

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        problems = self._client.registration_problems(self.trust_class, timeout_s=timeout_s)
        if problems:
            raise ToolError(  # noqa: TRY003
                f"mcp {self._client.server}: registration failed: {'; '.join(problems)[:200]}"
            )
        arguments = self._profile.to_args(args, self._fixed)
        reply = self._client.call_tool(self._tool, arguments, timeout_s=timeout_s)
        return self._profile.to_result(reply, arguments)


def verify_server(
    client: McpHttpClient, trust_class: TrustClass, *, timeout_s: float
) -> Sequence[str]:
    """Registration problems: declared tools the server lacks, or a read class it contradicts."""
    listed = {t.name: t for t in client.list_tools(timeout_s=timeout_s)}
    problems: list[str] = []
    for name in sorted(client.declared):
        info = listed.get(name)
        if info is None:
            problems.append(f"{name}: declared but not listed by the server")
        elif trust_class is TrustClass.READ and (info.read_only is False or info.destructive):
            problems.append(f"{name}: the server marks it as not read-only; can't register as read")
    return problems


__all__ = [
    "PROFILES",
    "PROTOCOL_VERSION",
    "McpBoundTool",
    "McpHttpClient",
    "McpReply",
    "McpToolInfo",
    "Profile",
    "verify_server",
]
