# 0012. The MCP client is a small synchronous client on httpx; the SDK is the reference server in tests

- **Status:** accepted
- **Date:** 2026-10-27
- **Code PR:** WP-M06 (tool bindings and MCP)
- **Spec sections changed:** ARCHITECTURE.md §7 (MCP extension), §20 (stack table)

## Context

ARCH §7 said the MCP extension would use the "official MCP Python SDK client". WP-M06 (ADR-0011 §2) brings MCP forward as a tool backend, and building it showed three problems with that choice:

- **The pipeline is synchronous.** Tools run inside `ToolRegistry.call` on worker threads. The SDK client is async (anyio) and session-scoped, so every call would need an event loop bridge, plus a background task to keep a session open.
- **The SDK is a large runtime dependency.** It pulls in starlette, sse-starlette, anyio, jsonschema and rpds, and a 2.x line whose API changed (FastMCP was renamed `MCPServer`). Each runtime dependency needs an ADR (05 §1), and this one would serve three JSON-RPC methods.
- **We use a narrow slice of the protocol.** That slice is `initialize`, `tools/list` and `tools/call` over streamable HTTP, with replies as JSON or an SSE stream and an `Mcp-Session-Id` session. We don't use resources, prompts, sampling, roots, elicitation or server-initiated requests, and the registration contract forbids most of them anyway.

## Decision

- **Runtime:** `adapters/tools/mcp.py` implements a synchronous MCP client on `httpx`, which is already a dependency. It covers streamable HTTP, protocol version `2025-06-18`, JSON or SSE replies, one lazy session per server, one re-initialize after a 404, and a 1 MB reply cap. There is **no new runtime dependency**.
- **Conformance:** the official SDK (`mcp`, pinned) is a **dev dependency**. The integration suite runs the client against the SDK's own streamable-HTTP server in both reply modes (`tests/integration/test_mcp_sdk_server.py`). An opt-in contract suite runs it against a real `mcp-grafana` (`tests/contract/test_mcp_grafana_live.py`).
- **Transport:** HTTP only for now. `stdio` servers (a subprocess per server) are BL-049.
- **New ARCH §7 text:** "MCP extension: a minimal synchronous streamable-HTTP client (ADR-0012), conformance-tested against the official SDK's server; registration is config (endpoint, declared tools, class and tier); undeclared tools are denied."

## Consequences

- **Better:**
  - no async bridge in the tool path;
  - no new runtime dependency tree;
  - the client is about 200 lines, simple to audit, and its failures are typed `ToolError`s like every other tool;
  - protocol conformance is still proven against the reference server on every PR.
- **Worse:** protocol evolution is ours to track. A server that requires a newer protocol version, or features we don't implement, fails `verify` and the `mcp` doctor check rather than degrading silently.
- **Tests:**
  - `test_handshake_listing_and_call_on_the_sdk_server[sse|json]`;
  - `test_same_lab_result_first_party_and_mcp[sse|json]`;
  - `test_mcp_output_injection_canary_not_followed[sse|json]`;
  - `test_sse_replies_and_expired_sessions`.
- **Watch:**
  - MCP protocol revisions;
  - whether a partner's server is stdio-only (BL-049);
  - when MCP tools ever need more than read access, revisit with the action gate rather than widening this client.
