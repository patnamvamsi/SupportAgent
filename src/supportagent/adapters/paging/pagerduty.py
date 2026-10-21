"""PagerDuty (ARCH §14, WP-112): find the open incident, add a note. Nothing else.

``find_open_page`` lists the service's ``triggered`` / ``acknowledged``
incidents filtered by ``incident_key`` (the dedup key the customer's
monitoring sent) and matches the key exactly. Notes need a ``From`` header
naming a PagerDuty user.
"""

from __future__ import annotations

import httpx

from supportagent.adapters.paging._http import call_json
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.paging import PageRef

PROVIDER = "pagerduty"


class PagerDutyPaging:
    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def supports(self, integration: str) -> bool:
        return integration.startswith(PROVIDER + ":")

    def find_open_page(self, integration: str, source_ref: str) -> PageRef | None:
        service_id = integration.removeprefix(PROVIDER + ":")
        if not service_id or not source_ref:
            return None
        body = call_json(
            self._client,
            "GET",
            "/incidents",
            params={
                "service_ids[]": [service_id],
                "statuses[]": ["triggered", "acknowledged"],
                "incident_key": source_ref,
            },
        )
        incidents = body.get("incidents", []) if isinstance(body, dict) else []
        for incident in incidents:
            if isinstance(incident, dict) and incident.get("incident_key") == source_ref:
                return PageRef(PROVIDER, str(incident["id"]), incident.get("html_url"))
        return None

    def find_note(self, page: PageRef, marker: str) -> ExternalRef | None:
        body = call_json(self._client, "GET", f"/incidents/{page.page_id}/notes")
        notes = body.get("notes", []) if isinstance(body, dict) else []
        for note in notes:
            if isinstance(note, dict) and marker in str(note.get("content", "")):
                return ExternalRef(system=PROVIDER, ref=f"{page.page_id}/notes/{note.get('id')}")
        return None

    def add_note(self, page: PageRef, text: str) -> ExternalRef:
        body = call_json(
            self._client,
            "POST",
            f"/incidents/{page.page_id}/notes",
            body={"note": {"content": text}},
        )
        note = body.get("note", {}) if isinstance(body, dict) else {}
        note_id = note.get("id", "unknown") if isinstance(note, dict) else "unknown"
        return ExternalRef(system=PROVIDER, ref=f"{page.page_id}/notes/{note_id}")


__all__ = ["PROVIDER", "PagerDutyPaging"]
