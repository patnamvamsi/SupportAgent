# Backlog

Known gaps, deferred deliverables and follow-ups found while reviewing the merged work packages. Each item names the WP it came from and the WP that should pick it up. When an item is done, delete its row and reference the backlog ID in the PR.

**Priority:** **P1** fix before the code path is wired into a running service (WP-011 / WP-016); **P2** fix within v0.0; **P3** later or opportunistic.

## Outbox and executor

| ID | P | Item | From | Pick up in |
|---|---|---|---|---|
| BL-001 | P1 | **Claim lease shorter than the ServiceNow retry budget.** `DEFAULT_LEASE_S = 60` (`app/outbox.py`), but one `ServiceNowHTTP` call can take ≈ 5 tries × (10 s connect + 30 s read) + backoff ≈ 200 s. A second worker can reclaim a row that is still mid-send, and both may write the journal entry. Derive the claim lease from the sink's worst-case budget, or make it config with a startup check `lease_s > sink budget`. | WP-006 / WP-008 | first assisted-mode WP (v0.1); shadow never builds a sink |
| BL-002 | P2 | `outbox.execute` job lease TTL is 10 s (`entrypoints/worker/jobs.py`), but a 50-row drain with HTTP calls can run much longer, so a second replica can start draining. Safe (claims are CAS), but wasteful. Renew the lease per row, or size the TTL to the drain budget. | WP-006 | first assisted-mode WP (v0.1) |
| BL-003 | P3 | `FakeServiceNow.find_marker` matches the marker anywhere in the text, not only on the last line (01 §7.5). Its `ExternalRef.system` is the table name rather than `servicenow`. | WP-006 | any |
| BL-004 | P2 | No `CommsRepository` for the `comms` table (01 §3 lists `uow.comms`). The table exists (migration 0003); nothing reads or writes it yet. | WP-005 | first comms WP (v0.1) |

## LLM boundary

| ID | P | Item | From | Pick up in |
|---|---|---|---|---|
| BL-005 | P1 | **Fixture provider** (`adapters/llm/providers/fixture.py`) is missing. ARCH §9 relies on fixture mode for deterministic code tests, and the WP-016 eval harness runs with `fixture` for wiring. | WP-007 | WP-010 |
| BL-006 | P2 | **Ollama provider** (`providers/ollama.py`) is missing (air-gap requirement, ARCH §9). | WP-007 | WP-010 |
| BL-007 | P2 | **`TracingLLMProvider`** decorator (`adapters/llm/tracing.py`) is missing. `LLMClient` emits the `llm` event itself, so decide whether to add the decorator or update the 10-wp deliverable list. | WP-007 | WP-010 |
| BL-008 | P2 | **Provider-native JSON mode:** `ProviderRequest.json_schema` is built but neither the Anthropic nor the OpenAI provider sends it. JSON output currently depends on the prompt alone. | WP-007 | WP-010 |
| BL-009 | P3 | `PromptRegistry` doesn't validate prompt front matter, and `StrictUndefined` raises at render time rather than at load validation (10-wp WP-007 wording). | WP-007 | WP-010 |
| BL-010 | P3 | The `llm` event records only `resolved_model`; 10-wp lists both the requested `model` and `resolved_model`. | WP-007 | any |

## ServiceNow and sync

| ID | P | Item | From | Pick up in |
|---|---|---|---|---|
| BL-012 | P2 | **Recorded fixtures** `tests/data/sn/*.json` and the `make record-sn` docs (WP-008 DoD). This needs a ServiceNow developer instance. | WP-008 | before WP-016 |
| BL-013 | P2 | Journal entries created inside the 2-minute overlap window can reach `on_changes` twice. Harmless while v0.0 only logs `sync` events. Dedup by journal entry `sys_id` (add it to the journal `sysparm_fields`) before lifecycle effects act on changes. | WP-008 | WP-101 |

## Wiring, tenancy, housekeeping

| ID | P | Item | From | Pick up in |
|---|---|---|---|---|
| BL-018 | P2 | **Calibration rejection at startup.** `load_calibration` raises `ConfigError` on a grade/model mismatch, while 01 §7.7 says "rejected at load, and every label then scores 0". Bootstrap must choose: fail startup, or load an empty table (everything safe-defaults) and surface it in `doctor`. Also decide whether `severity_escalated` treating a severity-less case as escalated is intended for case skeletons (WP-009). | WP-010 | WP-011 |
| BL-019 | P2 | **Re-triage doesn't re-route.** On severity escalation `TriageService` re-decides in place and records the decision, but proposes nothing; ARCH §4.1 says escalation re-runs classification *and routing*. Decide whether a re-triage submits new proposals (and how they relate to pending ones for the same scope). | WP-011 | first assisted-mode WP (v0.1); shadow keeps the first record |
| BL-020 | P3 | `TriageService` mints `trace_id` with `ULID()` directly instead of the injected `IdGenerator`, so trace ids aren't deterministic under `SequentialIdGenerator` in tests. Add a trace id prefix and use `ids`. | WP-011 | any |
| BL-023 | P2 | **WP-012 DoD, staging leg:** login against one real IdP in staging. The Authlib/joserfc adapter is covered by a mock IdP (discovery, PKCE, nonce, iss/aud/exp, signature, key rotation); a real IdP run needs staging. | WP-012 | before WP-016 |
| BL-024 | P1 | **Grant-authorized proposals never reach the outbox.** `GateService.submit` moves a grant-covered proposal straight to `approved`, but nothing enqueues its outbox row (the WP-011 DoD test enqueues by hand). Harmless in shadow mode, which never submits; it must be wired before assisted mode, ideally in the triage UoW like the approval path. | WP-011 / WP-013 | first assisted-mode WP (v0.1) |
| BL-026 | P3 | The admin UI uses plain HTML forms with no JavaScript, so HTMX is not loaded yet. When in-place updates are wanted (e.g. the TTL countdown), self-host `htmx.min.js` under `/static` so CSP stays `script-src 'self'`. | WP-013 | any |
| BL-028 | P2 | **The other v0.0 worker jobs.** The worker schedules only `sync.poll.<table>`. `proposals.expire` (`GateService.expire_due` exists) has nothing to expire in shadow mode, so it waits for assisted mode. `retention.sweep` needs a design for deleting from the append-only `events` table (03 §2.4: through a dedicated repository method, with a `retention` summary event). Nothing ages out within the first 90 days of a pilot. | shadow worker PR | v0.1, before the first pilot passes 90 days |
| BL-029 | P3 | The `sync.poll` lease isn't renewed during a run; its 900 s TTL bounds one poll. A slower poll (a large backlog where every record is an LLM call) can overlap the next holder, which `sync_seen` and INV-02 dedup absorb. Renew per page if overlaps show up in the `sync` events. | shadow worker PR | any |
| BL-030 | P2 | **Check the doctor's ServiceNow heuristics against a real instance.** The ACL checks don't evaluate ACL conditions or scripts (a role-less `work_notes` read ACL counts as exposed). The rate-limit recommendation assumes hourly limits and `CALLS_PER_POLL = 3` (prov.). `sys_audit` retention is read from `sys_auto_flush.age`. Every probe runs against FakeServiceNowHTTP only. | WP-015 | with BL-012 (developer instance), before the WP-016 exit demo |
| BL-031 | P3 | `/ui/setup` should show the `doctor` status (02 §3); it shows only the grants. A doctor run makes network calls, so the page needs a cached last report (e.g. written by `doctor` to the DB) rather than running checks per request. | WP-015 | any |
| BL-015 | P2 | `trace_events` replay filters by `trace_id` only, not tenant. Safe today because trace IDs are globally unique. | WP-004 | WP-305 |
