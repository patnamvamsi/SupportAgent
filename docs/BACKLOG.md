# Backlog

Known gaps, deferred deliverables and follow-ups found while reviewing the merged work packages. Each item names the WP it came from and the WP that should pick it up. When an item is done, delete its row and reference the backlog ID in the PR.

**Priority:** **P1** fix before the code path is wired into a running service (WP-011 / WP-016); **P2** fix within v0.0; **P3** later or opportunistic.

## Outbox and executor

| ID | P | Item | From | Pick up in |
|---|---|---|---|---|
| BL-003 | P3 | `FakeServiceNow.find_marker` matches the marker anywhere in the text, not only on the last line (01 §7.5). Its `ExternalRef.system` is the table name rather than `servicenow`. | WP-006 | any |
| BL-004 | P2 | No `CommsRepository` for the `comms` table (01 §3 lists `uow.comms`). The table exists (migration 0003); nothing reads or writes it yet. | WP-005 | first comms WP (v0.1) |

## LLM boundary

| ID | P | Item | From | Pick up in |
|---|---|---|---|---|
| BL-007 | P2 | **`TracingLLMProvider`** decorator (`adapters/llm/tracing.py`) is missing. `LLMClient` emits the `llm` event itself, so decide whether to add the decorator or update the 10-wp deliverable list. | WP-007 | WP-010 |
| BL-009 | P3 | `PromptRegistry` doesn't validate prompt front matter, and `StrictUndefined` raises at render time rather than at load validation (10-wp WP-007 wording). | WP-007 | WP-010 |
| BL-010 | P3 | The `llm` event records only `resolved_model`; 10-wp lists both the requested `model` and `resolved_model`. | WP-007 | any |

## ServiceNow and sync

| ID | P | Item | From | Pick up in |
|---|---|---|---|---|
| BL-012 | P2 | **Recorded fixtures** `tests/data/sn/*.json` and the `make record-sn` docs (WP-008 DoD). This needs a ServiceNow developer instance. | WP-008 | before WP-016 |
| BL-013 | P3 | Journal entries created inside the 2-minute overlap window can reach `on_changes` twice. Harmless so far: WP-101's lifecycle acts only on `sys_audit` field changes (deduped by checkpoint), and journal entries only add a `sync` event. Dedup by journal entry `sys_id` (add it to the journal `sysparm_fields`) before anything acts on journal changes (WP-102 cadence reads human-actor values from the history). | WP-008 | WP-102 |

## Wiring, tenancy, housekeeping

| ID | P | Item | From | Pick up in |
|---|---|---|---|---|
| BL-020 | P3 | `TriageService` mints `trace_id` with `ULID()` directly instead of the injected `IdGenerator`, so trace ids aren't deterministic under `SequentialIdGenerator` in tests. Add a trace id prefix and use `ids`. | WP-011 | any |
| BL-023 | P2 | **WP-012 DoD, staging leg:** login against one real IdP in staging. The Authlib/joserfc adapter is covered by a mock IdP (discovery, PKCE, nonce, iss/aud/exp, signature, key rotation); a real IdP run needs staging. | WP-012 | before WP-016 |
| BL-026 | P3 | The admin UI uses plain HTML forms with no JavaScript, so HTMX is not loaded yet. When in-place updates are wanted (e.g. the TTL countdown), self-host `htmx.min.js` under `/static` so CSP stays `script-src 'self'`. | WP-013 | any |
| BL-029 | P3 | The `sync.poll` lease isn't renewed during a run; its 900 s TTL bounds one poll. A slower poll (a large backlog where every record is an LLM call) can overlap the next holder, which `sync_seen` and INV-02 dedup absorb. Renew per page if overlaps show up in the `sync` events. | shadow worker PR | any |
| BL-030 | P2 | **Check the doctor's ServiceNow heuristics against a real instance.** The ACL checks don't evaluate ACL conditions or scripts (a role-less `work_notes` read ACL counts as exposed). The rate-limit recommendation assumes hourly limits and `CALLS_PER_POLL = 3` (prov.). `sys_audit` retention is read from `sys_auto_flush.age`. Every probe runs against FakeServiceNowHTTP only. | WP-015 | with BL-012 (developer instance), before the WP-016 exit demo |
| BL-031 | P3 | `/ui/setup` should show the `doctor` status (02 §3); it shows only the grants. A doctor run makes network calls, so the page needs a cached last report (e.g. written by `doctor` to the DB) rather than running checks per request. | WP-015 | any |
| BL-032 | P3 | Takeover state values are ServiceNow's incident defaults (`6` resolved, `7` closed, `8` canceled; anything else open). An instance with custom states needs a `servicenow.state_map` config entry. | WP-101 | when a partner needs it |
| BL-033 | P2 | **Internal comms plans.** The trigger's plan path (ARCH §6.2 path 3) authorizes customer kinds only, so v0.1 plans are customer plans (`servicenow_comment` → `customer_comment`). An internal cadence would post work notes, which need the T1 grant path or a widened trigger (ADR). | WP-102 | when an internal cadence is wanted |
| BL-036 | P3 | **Case-memory text index.** BM25 is computed in the application over the tenant's resolved cases (identical on both engines, fine for a v0.1 corpus). Past a few thousand cases, pre-filter with SQLite FTS5 / a Postgres `tsvector` GIN index, keeping the in-app scorer for the final ranking. Also: `resolved_by` edges (case → resolution action) aren't derived yet, so `graph_similarity` uses `similar` edges only; the v0.2 cross-encoder reranker is not started. | WP-108 | when the corpus grows / v0.2 |
| BL-037 | P1 | **The v0.1 holdout.** The quality harness is built (WP-104), but the 150-250-case holdout (>= 60 P1/P2, >= 10 per routed label, `source:` required, split by time and signature in `evals/splits.yaml`) must come from the design partner's corpus. Tag those cases `holdout`. Also: a nightly live quality run needs a model key and a config in CI secrets; citation faithfulness joins the report with RCA claims (WP-110). | WP-104 | when partner data arrives |
| BL-038 | P1 | **The one v0.1 change source** (ARCH §7, CAP §6 item 7): exactly one of Argo CD, Terraform Cloud runs, Consul, or ServiceNow change requests, whichever the design partner already has. Until it lands, `recent_change_push` can be at best INCONCLUSIVE (ARCH §5.5). Needs the partner's answer. | WP-107 | when the partner chooses |
| BL-039 | P1 | **Citation-faithfulness eval.** RCA-lite (WP-110) runs every claim through the `entail` slot and fails closed, but the checker itself isn't regression-tested yet: ARCH §5.4/§11 want a citation-faithfulness rate in the quality suite over curated claim/evidence pairs (release gate >= 0.95, prov.). Curate the pairs from the partner's incidents (with BL-037), and add `entail` to the quality run. Also: a runbook's `comms_if_supported` (internal) is recorded on the artifact but drafts nothing until internal comms plans exist (BL-033). | WP-110 | with BL-037, before the v0.1 tag |
| BL-040 | P2 | **Richer `enrich_page` content.** v0.1 attaches a fixed template of case fields (case id, service, severity, classification). ARCH §14 also lists the triage summary, top hypotheses (the RCA-lite artifact, WP-110), suggested actions and who is on call (`oncall.who`). Add them as allowlisted fields, never LLM free text. Also: `enrich_page` executes only once `outbox.execute` is scheduled (BL-034). | WP-112 | with BL-034 |
| BL-041 | P1 | **Assisted treatment/control split** (ARCH §8.4, C22). v0.1 assisted mode (ADR-0008) applies to every catalog service. The assisted-delta value number needs a treatment set (assisted) and a control set (human-only) agreed with the partner, and `stats` reporting the delta with confidence intervals. | ADR-0008 | v0.2 |
| BL-015 | P2 | `trace_events` replay filters by `trace_id` only, not tenant. Safe today because trace IDs are globally unique. | WP-004 | WP-305 |
