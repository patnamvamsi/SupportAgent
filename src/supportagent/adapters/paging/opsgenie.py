"""Opsgenie (ARCH §14, WP-112): find the open alert by alias, add a note. Nothing else.

The alias is Opsgenie's dedup key, so ``find_open_page`` reads the alert by
alias and requires it to be open and to belong to the integration's team
(``opsgenie:<team>``: a responder or a tag of that name).
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from supportagent.adapters.paging._http import call_json
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.paging import PageRef

PROVIDER = "opsgenie"


class OpsgeniePaging:
    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def supports(self, integration: str) -> bool:
        return integration.startswith(PROVIDER + ":")

    def find_open_page(self, integration: str, source_ref: str) -> PageRef | None:
        team = integration.removeprefix(PROVIDER + ":")
        if not team or not source_ref:
            return None
        body = call_json(
            self._client,
            "GET",
            f"/v2/alerts/{quote(source_ref, safe='')}",
            params={"identifierType": "alias"},
        )
        alert = body.get("data") if isinstance(body, dict) else None
        if not isinstance(alert, dict) or alert.get("status") != "open":
            return None
        teams = {
            str(r.get("name") or r.get("id"))
            for r in alert.get("responders", []) or []
            if isinstance(r, dict) and r.get("type") == "team"
        }
        if team not in teams and team not in (alert.get("tags") or []):
            return None
        return PageRef(PROVIDER, str(alert["id"]))

    def find_note(self, page: PageRef, marker: str) -> ExternalRef | None:
        body = call_json(self._client, "GET", f"/v2/alerts/{page.page_id}/notes")
        notes = body.get("data", []) if isinstance(body, dict) else []
        for note in notes:
            if isinstance(note, dict) and marker in str(note.get("note", "")):
                return ExternalRef(system=PROVIDER, ref=f"{page.page_id}/notes")
        return None

    def add_note(self, page: PageRef, text: str) -> ExternalRef:
        body = call_json(
            self._client, "POST", f"/v2/alerts/{page.page_id}/notes", body={"note": text}
        )
        request_id = body.get("requestId", "accepted") if isinstance(body, dict) else "accepted"
        return ExternalRef(system=PROVIDER, ref=f"{page.page_id}/notes/{request_id}")


__all__ = ["PROVIDER", "OpsgeniePaging"]
