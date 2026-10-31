# 0017. Onboarding autopilot: any system, one command; install what exists, name what doesn't, resume when it arrives

- **Status:** accepted (2026-10-31; the maintainer's direction, quoted below)
- **Date:** 2026-10-31
- **Code PR:** WP-A1 to WP-A5 (below), each in its own PR
- **Builds on:** ADR-0015 (onboarding, plugins, readiness, drift), ADR-0016 (plugins are data)

## Context

The maintainer, after Systems A, B and C:

> we need not keep accounting for system a, b, c. given any new system tomorrow, the framework should auto calibrate, sense the gaps, install if available else let the user know about the gaps. once the user provides the appropriate plugins, it can start working.

The pieces exist, but a person drives them, and some only work for what someone already thought of:

| The loop | After ADR-0015/0016 | What's missing |
|---|---|---|
| Read the system | `onboard ingest` | nothing |
| Sense which plugins apply | detection, over the plugins in `plugins/` only | detection over **every plugin available**, not only installed ones |
| Install what's available | a person copies a plugin in and re-pins it | **install** from a catalogue, gated on the plugin's own lab passing here |
| Sense gaps nothing covers | classes no request unlocks; untestable classes; drift's "uncovered" | **the unknown**: technology no plugin claims, CMDB classes nothing maps, incident clusters with no known cause, causes with no evaluated knowledge |
| Tell the user | separate CLIs and pages | **one list of what's blocked and on whom** |
| Calibrate | fixed readiness gates; the classifier's calibration table is configured, not fitted | **fitted from the system's own replayed history** |
| Pick up what the user provides | re-run each command by hand | **one re-runnable command** that resumes where it was blocked |

The reference systems were how the framework got built. From here they are its regression tests: no per-system code, and no new fixture unless a stage needs a new kind of input.

## Decision

### 1. One command, one state machine

`supportagent onboard run --system system.yaml --workspace <dir>` runs every stage in order (`onboard` is the command group). A **step** is one of ingest, sense, install, graph, incidents, requests, readiness or calibrate. Each step reads the workspace and writes its artifact plus a line in `onboarding.json`:

```json
{"step": "install", "state": "blocked", "on": "user", "detail": "2 plugins need approval", "at": "..."}
```

- A step is `done`, `blocked` (on the user, on access, or on a missing plugin) or `failed` (retryable).
- A re-run skips `done` steps whose inputs haven't changed (input digests), and retries `blocked` and `failed` ones.
- **Re-running is how the loop resumes.** A plugin dropped into the catalogue, an approved request or a confirmed graph fact each changes a step's input, and the next run picks it up.
- **Steps never block each other unnecessarily.** The graph and incident map run without any access; readiness runs for the classes it can, and names the rest.
- After the first full run, a re-run is drift (ADR-0015 P7). It diffs against the last accepted snapshot and replays only what changed.

### 2. A plugin catalogue, and install

- **Two sets.** *Installed* plugins are `plugins/` with its pinned index, as today. *Available* plugins come from **catalogues**:

  ```yaml
  plugins:
    catalogs:
      - /srv/supportagent/catalog                       # a directory laid out like plugins/
      # a git catalogue (url and pinned ref) is a follow-up; a checkout is a directory
    auto_install: first-party                           # none | first-party
  ```

  Each catalogue has its own `index.yaml`, with each plugin's version and sha256.
- **Sense runs every detector**, installed and available. Detectors are data, so evaluating them is safe.
- **Install** copies a detected, available plugin into `plugins/` and pins it, but only after:
  - its manifest loads, so the ADR-0015/0016 load-time rules hold;
  - its pack's lab passes here, with the instance's own model.

  A first-party plugin installs automatically when `auto_install: first-party` is set. A third-party plugin, or any plugin when auto-install is off, becomes a **pending approval**.
- **A failed lab doesn't block onboarding.** The plugin installs as `skeleton`: its contracts and detectors work, so requests are still made, but its classes can't be ready (ADR-0015 §2).
- **Uninstall is never automatic.** A plugin that's no longer detected is a drift finding.

### 3. Sensing the unknown

Beyond "a plugin matches", the sense step reports what **nothing** covers. Each finding carries its evidence, and a scaffold to start from (`supportagent pack new`, `plugin new`, from the extend skill):

| Gap | Evidence |
|---|---|
| **Unclaimed technology** | a manifest no plugin's detector or extractor claims: `go.mod`, `Gemfile`, `*.csproj`, `Cargo.toml`, `requirements.txt` without a Python plugin. A table of known manifest names stays in core, since it describes formats, not stacks |
| **Unmapped CMDB class** | an installed CI class no built-in or declared extractor maps, with how many CIs have it |
| **Uncaused incidents** | incident clusters the map couldn't give a known cause: the no-cause class's tickets, clustered by CI and text, weighted like any class |
| **Untestable cause** | a class whose cause no evaluated pack has a probe for |
| **Unknown capability** | a pack probe or runbook naming a capability no installed tool or declared schema provides |

### 4. One status: what's blocked, and on whom

`supportagent onboard status --workspace <dir>`, and the top of `/ui/onboarding`, show:
- each step's state;
- one list of **what's needed from a person**, ranked by the incident weight it holds back:
  - approve these plugins;
  - provide a plugin for these gaps;
  - grant these access requests;
  - confirm these graph facts;
  - settle these questions.
- the readiness verdict against the bar.

### 5. Calibration fitted per system

The calibrate step fits per-system thresholds from the replayed history:
- the classifier's per-label calibration table (agree and abstain rows, ADR-0002) from the readiness replays' classifications;
- per-class readiness gates within the ADR-0003 bounds. A class can't be gated below `min_scenarios`, and the false-cause gate stays at zero.

They're written to the workspace and the database. Routing uses them from the next start, like the readiness report.

## Delivery

| WP | What | Done when |
|---|---|---|
| WP-A1 (*delivered*: `onboard run`, `onboard status`, `domain/autopilot.py`) | `onboard run` (one resumable command) and `onboard status`; `onboarding.json` steps with input digests | A reference system goes from `system.yaml` to readiness in one command; an interrupted run resumes; a no-change re-run does nothing |
| WP-A2 (*delivered*: `plugins.catalogs`, `plugin install`, embedded packs and `tree_sha256`; git catalogues are a follow-up) | Plugin catalogues; sense over installed and available; install gated on the plugin's lab; pending approvals | System C onboards from a catalogue holding its three plugins, with `plugins/` lacking them, and reaches the same readiness |
| WP-A3 | Sensing the unknown (§3) with scaffolds | A fixture with a Go service and an unmapped CMDB class reports both gaps, with evidence |
| WP-A4 | The status page: what's needed, ranked by weight | `/ui/onboarding` shows one ranked list; acting on an item clears it on the next run |
| WP-A5 | Calibration fitted per system | The calibration table and gates are fitted from replay, and routing uses them |

## Consequences

- **Only classes with a cause count toward the bar** (WP-A1). The no-cause class can be ready, since abstaining is right, but a run whose readings all failed would otherwise look ready.
- **A step the model couldn't finish is blocked on the model, not done.** Its re-run retries the unanswered part (cached answers are kept).

- **The fixtures become tests.** The proof for WP-A2 is that System C needs nothing outside the catalogue. Systems A and B regress-test that nothing changes for them.
- **A catalogue is a supply-chain boundary.** It's pinned like the local index, and third-party plugins wait for approval (ADR-0015 decision 2). Install never runs code: plugins are data (ADR-0016), and MCP servers are registered by the deployment, never by a plugin.
- **The loop is only as good as its gap list.** An unknown that sense doesn't see stays silent. The table in §3 is meant to grow, and drift (ADR-0015 P7) re-runs sense on every pass.
