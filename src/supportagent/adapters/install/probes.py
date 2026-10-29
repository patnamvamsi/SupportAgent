"""Connection probes for ``supportagent init``: read-only, one short request each.

The wizard checks every credential as it is typed, so a typo is fixed at the
prompt rather than found later by ``doctor``. Each probe returns what the next
question needs (the models a server offers, the CIs on recent incidents) or
raises ``ProbeError`` with a message for the person at the keyboard.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import httpx

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_ANTHROPIC_VERSION = "2023-06-01"


class ProbeError(Exception):
    """A probe failed; ``str(exc)`` says what to fix."""


@dataclass(frozen=True, slots=True)
class CiCount:
    name: str
    sys_id: str
    incidents: int


def list_models(
    kind: str,
    *,
    base_url: str | None,
    api_key: str | None,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """The model ids the provider offers, newest first where it says so."""
    with httpx.Client(timeout=_TIMEOUT, transport=transport) as http:
        if kind == "anthropic":
            r = _get(
                http,
                "https://api.anthropic.com/v1/models?limit=100",
                headers={"x-api-key": api_key or "", "anthropic-version": _ANTHROPIC_VERSION},
                what="Anthropic",
            )
            return [str(m["id"]) for m in r.json().get("data", [])]
        if kind == "openai":
            r = _get(
                http,
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key or ''}"},
                what="OpenAI",
            )
            return sorted(str(m["id"]) for m in r.json().get("data", []))
        if kind == "local":
            base = (base_url or "").rstrip("/")
            r = _get(
                http,
                f"{base}/models",
                headers={"Authorization": f"Bearer {api_key or 'local'}"},
                what=f"the server at {base}",
            )
            ids = [str(m["id"]) for m in r.json().get("data", [])]
            return [i for i in ids if "embed" not in i.lower()]
        if kind == "ollama":
            base = (base_url or "").rstrip("/")
            r = _get(http, f"{base}/api/tags", headers={}, what=f"Ollama at {base}")
            return [str(m["name"]) for m in r.json().get("models", [])]
    raise ProbeError(f"unknown provider {kind!r}")


def servicenow_auth(  # noqa: PLR0913 — basic or OAuth credentials, plus the test seam
    instance_url: str,
    *,
    user: str | None,
    password: str | None,
    client_id: str | None,
    client_secret: str | None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, str]:
    """Headers for the Table API: basic auth, or an OAuth client-credentials token."""
    if client_id is None:
        token = httpx.BasicAuth(user or "", password or "")
        request = token.auth_flow(httpx.Request("GET", instance_url))
        return {"Authorization": next(request).headers["Authorization"]}
    with httpx.Client(timeout=_TIMEOUT, transport=transport) as http:
        try:
            r = http.post(
                f"{instance_url.rstrip('/')}/oauth_token.do",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret or "",
                },
            )
        except httpx.HTTPError as exc:
            raise ProbeError(f"can't reach {instance_url}: {exc}") from exc
    if r.status_code != httpx.codes.OK or "access_token" not in r.json():
        raise ProbeError(
            f"ServiceNow refused the OAuth client (HTTP {r.status_code}); check the client "
            "id and secret, and that the OAuth application allows client credentials"
        )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def servicenow_cis(
    instance_url: str,
    *,
    headers: dict[str, str],
    integration_user: str,
    transport: httpx.BaseTransport | None = None,
) -> list[CiCount]:
    """Check the credentials and the integration user; the CIs on recent incidents."""
    base = instance_url.rstrip("/")
    with httpx.Client(timeout=_TIMEOUT, transport=transport, headers=headers) as http:
        users = _get(
            http,
            f"{base}/api/now/table/sys_user",
            params={
                "sysparm_query": f"user_name={integration_user}",
                "sysparm_fields": "sys_id",
                "sysparm_limit": "1",
            },
            headers={"Accept": "application/json"},
            what="ServiceNow",
        )
        if not users.json().get("result"):
            raise ProbeError(
                f"no user {integration_user!r} on {base}; create it (role itil) or give "
                "the name of the user SupportAgent signs in as"
            )
        rows = (
            _get(
                http,
                f"{base}/api/now/table/incident",
                params={
                    "sysparm_query": "cmdb_ciISNOTEMPTY^ORDERBYDESCsys_updated_on",
                    "sysparm_fields": "cmdb_ci,cmdb_ci.name",
                    "sysparm_display_value": "false",
                    "sysparm_limit": "500",
                },
                headers={"Accept": "application/json"},
                what="ServiceNow incidents",
            )
            .json()
            .get("result", [])
        )
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        ci = row.get("cmdb_ci")
        sys_id = ci.get("value") if isinstance(ci, dict) else ci
        name = row.get("cmdb_ci.name") or sys_id
        if sys_id:
            counts[(str(name), str(sys_id))] += 1
    return [CiCount(name, sid, n) for (name, sid), n in counts.most_common()]


def confluence_space(
    base_url: str,
    *,
    space_key: str,
    user: str,
    token: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """The space's name, read with the account email and API token."""
    site = base_url.rstrip("/").removesuffix("/wiki")
    with httpx.Client(
        timeout=_TIMEOUT, transport=transport, auth=httpx.BasicAuth(user, token)
    ) as http:
        r = _get(
            http,
            f"{site}/wiki/api/v2/spaces",
            params={"keys": space_key},
            headers={"Accept": "application/json"},
            what="Confluence",
        )
    spaces = r.json().get("results", [])
    if not spaces:
        raise ProbeError(f"no space with key {space_key!r} that this account can read")
    return str(spaces[0].get("name") or space_key)


def _get(
    http: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    what: str,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        r = http.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise ProbeError(f"can't reach {what} ({url}): {exc}") from exc
    if r.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        raise ProbeError(f"{what} refused the credentials (HTTP {r.status_code})")
    if r.status_code != httpx.codes.OK:
        raise ProbeError(f"{what} answered HTTP {r.status_code} for {url}")
    try:
        r.json()
    except ValueError as exc:
        raise ProbeError(f"{what} did not answer JSON at {url}; is the URL right?") from exc
    return r
