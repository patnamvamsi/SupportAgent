# 0018. Live evidence capture: replay a real system's history from its own tools, as of each incident

- **Status:** accepted (2026-11-02; roadmap item 1.1 in `docs/next-steps.md`)
- **Date:** 2026-11-02
- **Code PR:** WP-E1
- **Builds on:** ADR-0015 §5 (readiness from history), ADR-0017 (the onboarding autopilot)

## Context

Readiness replays each past incident from the evidence captured at the time (`sources/evidence/<incident>.yaml`). The reference systems carry that evidence as fixtures. A real workspace has none, so a real system ends every `onboard run` with *no evidence captured*, whatever its plugins.

The evidence still exists in most systems: log stores, metric stores, deployment and CI history all keep the past for a while. What doesn't exist is a way to ask the agent's own read-only tools for it **as of** an incident, and to be honest about what they can't see.

## Decision

### 1. A tool can answer as of a time, or it can't

A tool that can read the past implements `run_as_of(args, *, at, timeout_s)` beside `run`. It is the same read, ending at `at` instead of now:

| Capability | As of `at` |
|---|---|
| `logs.search` (Loki) | the window ends at `at` |
| `metrics.query` (Prometheus) | the instant query is evaluated at `at` |
| `changes.recent` | changes in the window ending at `at`, none after |
| `gha.recent_runs` | runs created before `at` |

Every other tool is **point-in-time**: it reads current locks, sessions, queue depth or pods. Its evidence for a past incident is marked *not replayable*, never captured from today's state. MCP-served capabilities are point-in-time in this ADR. A binding that maps a time argument is a follow-up.

Oracle's AWR and ASH views (`DBA_HIST_*`) do keep history for sessions, locks and tablespace use. Reading them needs the Diagnostics Pack licence. There is none (maintainer, 2026-11-02), so they are left out.

### 2. Retention is declared, never guessed

An empty answer from a store whose data has aged out looks like "nothing was wrong". Each replayable capability therefore has a retention, in days: `onboarding.retention_days`, which defaults to:
- `logs.search` 30;
- `metrics.query` 15;
- `changes.recent` 90;
- `gha.recent_runs` 400.

For an incident older than a capability's retention, that capability is *aged out*: reported, not called.

### 3. What is captured

For each past incident in a class with a cause, capture reads the probes the RCA would plan for it:
- the runbooks whose `applies_when` matches the class's label, from the packs the incident's service selects (its stack, from the graph);
- the cause's probe.

Each capability is read once with the first step's arguments, because a world holds one result per capability. The file records:
- `world`: each result, or the tool's error (a refused read is evidence of nothing, so it is kept as an error);
- `not_replayable` and `aged_out`, by capability;
- `captured: live`, and `captured_at` (the incident's open time).

If **any** of the cause's own probes couldn't be read, the world is left empty. Readiness then doesn't replay that incident and says why: its cause is visible only live, or its evidence aged out. Replaying without the decisive read isn't fair to the cause. In System A's lock class, with the Oracle reads point-in-time, the remaining logs and metrics led the RCA to the pool exhaustion the locks caused, six false causes. Live triage, which reads the present locks, wouldn't make that mistake. A class with nothing replayable can still be proven later in shadow (roadmap 1.3).

Every read goes through the tool registry, so each one is a `tool_call` event like any other.

### 4. Where it runs

`onboard evidence --system --workspace` runs capture, and `onboard run` gains an `evidence` step before `readiness`. That step only runs on a live workspace, one with tools configured: a workspace that already has evidence (a fixture, or an earlier capture) keeps it. Re-capturing needs `--recapture`.

## Consequences

- A real system's readiness becomes measurable for every class whose cause shows in logs, metrics, deploys or CI history. Classes whose cause only current state shows (locks, sessions, queue depth) are reported as *visible only live* instead of being guessed or silently failed.
- The reference systems don't change: their fixtures are evidence already.
- Done when a workspace whose tools are fakes with history captures evidence that reaches the same readiness as its fixture. A point-in-time tool and an aged-out store are each reported as such.
