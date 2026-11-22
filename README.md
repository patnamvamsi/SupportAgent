# SupportAgent

An application support analyst that works your ServiceNow queue. It triages incidents, runs bounded, read-only root-cause checks, drafts communications and proposes actions. Anything that changes state or reaches a customer goes through an approval that the database enforces. It starts in **shadow mode**: it reads tickets and records what it would have done, and never writes to ServiceNow until you promote it.

## Install on your laptop

You need **Docker**, plus either a model API key or a model running locally. Everything else runs in containers.

| | macOS | Linux | Windows |
|---|---|---|---|
| Docker | [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/) | [Docker Engine](https://docs.docker.com/engine/install/) + the Compose plugin (v2.20+) | Docker Desktop with WSL2; run the commands inside WSL |
| Memory for Docker | 4 GB | 4 GB | 4 GB |
| Disk | about 3 GB for the images | | |

```bash
git clone https://github.com/patnamvamsi/SupportAgent.git
cd SupportAgent
./install.sh
```

`./install.sh` takes it from there:

1. **Checks this machine:** Docker is installed and running, Compose is new enough, and there's memory, disk and free ports. Each problem it finds comes with the fix.
2. **Builds the SupportAgent image** (a few minutes the first time).
3. **Asks the setup questions** and checks each credential as you type it, so a typo gets fixed at the prompt:

   | Question | What you need | Notes |
   |---|---|---|
   | Language model | **Anthropic** or **OpenAI** API key, or the URL of a **local server**: LM Studio, vLLM, llama.cpp (OpenAI-compatible) or **Ollama** | It lists the models the server offers and you pick one. `localhost` works: the containers reach your laptop through `host.docker.internal`. |
   | The system | Its name, and where its code is: a **checkout on this machine** (mounted read-only) and/or **GitHub repositories** (`github:acme/*`, with a read-only token). Optionally a plugin catalogue: a directory, or a git URL and a full commit id | Writes `local/system.yaml`. After setup the instance reads the code itself (dependencies and secret files are never read) and builds its picture of the system. |
   | Signals (optional) | Your **Prometheus** and **Loki** URLs, and whether an **Alertmanager** or Grafana will send alerts | Read-only. SupportAgent measures what normal looks like from their history, proposes detectors and evaluates the ones you enable. |
   | Tickets | A ServiceNow instance URL and an integration user (basic auth or OAuth client credentials), **the demo instance**, or **none** | For a real instance it reads your recent incidents and offers their CIs as the services to watch, and can read the system's past incidents for onboarding. The demo needs nothing and sends a sample incident every 5 minutes. With none, alerts are the intake (shadow mode). |
   | Confluence (optional) | The space URL, your Atlassian email, an [API token](https://id.atlassian.com/manage-profile/security/api-tokens) | Paste the space URL from your browser; it finds the space key itself. |
   | PagerDuty (optional) | A REST API key and a From email | SupportAgent only attaches notes to pages that are already open. It never creates one. |
   | Your sign-in | A user name and a password (or let it generate one) | A bundled Keycloak handles sign-in. The first person to sign in becomes the admin. |

4. **Calibrates your model** on 27 built-in cases. This takes about a minute with a hosted API, and up to about 15 minutes with a large local model.
5. **Starts everything** and hands the system to the instance (ADR-0023). The instance onboards it by itself in the background, read-only, and again every day: it reads the code, builds the system graph with a summary of each component, measures detectors, and lists what it needs from you on **Onboarding** (access to grant, facts and detectors to review). Then the installer prints where to sign in:

   ```
   Open      http://localhost:8800/ui
   Sign in   admin / <your password>
   ```

Sign in with **Chrome, Edge or Firefox**. Keycloak's sign-in cookies are marked secure, and those browsers accept secure cookies on `http://localhost`. Safari may not.

Your answers and secrets are written to `local/` (git-ignored). Secrets live only in `local/.env`, which is private to you (mode 600). All ports bind to `127.0.0.1`, so nothing is reachable from your network.

### Scripted install

To skip the questions, pass the answers as a JSON file. Leave the secrets out of the file in shared environments.

```json
{
  "team_name": "Acme Support",
  "llm": {"kind": "local", "base_url": "http://localhost:1234/v1", "model_id": "qwen/qwen3.8-27b"},
  "servicenow": {"kind": "demo"},
  "admin_user": "ada"
}
```

```bash
./install.sh --answers answers.json
```

- `llm.kind` is `anthropic`, `openai`, `local` or `ollama`, plus `api_key` for the first two.
- A real ServiceNow is `{"kind": "basic", "instance_url": "...", "user": "...", "password": "..."}` or `{"kind": "oauth", ..., "client_id": "...", "client_secret": "..."}`, with a `services` list of `{"name", "resources": [CI names], "cmdb_ci": [CI sys_ids]}`.

## Day to day

| Command | What it does |
|---|---|
| `./install.sh doctor` | Checks everything and prints a fix (→) under each problem (see below) |
| `./install.sh status` | Container status |
| `./install.sh logs [app\|keycloak\|postgres\|demo-servicenow]` | Follows the logs |
| `./install.sh stop` / `start` / `restart` | Stops it or brings it back. Your data stays. |
| `./install.sh reconfigure` | Asks the questions again, with your current answers as the defaults. Generated secrets are kept, so your data stays readable. |
| `./install.sh calibrate` | Runs the calibration again: do this after changing the model |
| `./install.sh onboard` | Onboards the system in `local/system.yaml` again (read-only) and lists what it still needs. Re-run it after you grant access or add a plugin. |
| `./install.sh uninstall [--purge]` | Deletes the containers, database and sign-in data. `--purge` deletes `local/` too. |

## When something is wrong: `./install.sh doctor`

The doctor checks, in order:

1. **This machine:** Docker, Compose, the credential helper, memory and disk.
2. **Install files:** `local/` is complete, `.env` is private, the model calibration and the image exist.
3. **Containers:** each one is running and healthy, and the database migrations ran.
4. **Endpoints:** SupportAgent and Keycloak answer on their ports.
5. **SupportAgent's own checks:** the config, the database, ServiceNow (sign-in, the integration user's role, history access, work-note visibility, rate limits), the model, sign-in, the knowledge sources, paging and packs.

Each problem comes with what to do about it. For example:

```
  ✗ llm: the model did not answer: connection refused
    → is the model server running and reachable from Docker? A local server must listen on
    → all interfaces (LM Studio: Developer > Settings > Serve on local network; Ollama:
    → OLLAMA_HOST=0.0.0.0). Wrong key or model: ./install.sh reconfigure
```

A warning (!) is something optional you haven't set up, such as PagerDuty.

### Connecting a real ServiceNow

Create a dedicated integration user, for example `svc_supportagent`, with the `itil` role and nothing broader. Sync also reads each incident's history, so the user needs **read access to `sys_audit` and `sys_journal_field`**, which ServiceNow restricts by default. [docs/quickstart.md](docs/quickstart.md#the-servicenow-integration-user) shows the ACL. The doctor's `servicenow.history_access` check fails until this is in place.

### Pointing it at your system

Describe the system once in a `system.yaml`: its ServiceNow scope, Confluence spaces and repositories (see `evals/onboarding/system-a/system.yaml`). Then:

```bash
supportagent onboard run --system system.yaml --workspace ./onboarding   # re-run to continue
supportagent onboard status --workspace ./onboarding
```

It reads the system read-only, works out which plugins it needs, maps the system and its incident history, asks for read-only access, and proves per incident class whether it can support it. It ends with what it needs from you: access to grant, plugins to add, facts to confirm. Act on those, re-run, and it continues where it stopped. The review pages are under `/ui/onboarding`. How it works: [ADR-0015](docs/adr/0015-system-onboarding-and-plugins.md), [ADR-0016](docs/adr/0016-plugins-are-data.md), [ADR-0017](docs/adr/0017-onboarding-autopilot.md). What comes next: [docs/next-steps.md](docs/next-steps.md).

### Going further

- **Shadow → assisted:** once the shadow run's agreement is good enough (`/ui/stats`), set `mode: assisted` in `local/supportagent.yaml`. Raise `calibration.min_score` before you do this: the local install uses a trial floor of 0. See [docs/quickstart.md](docs/quickstart.md) and [ADR-0008](docs/adr/0008-assisted-mode-in-v0-1.md).
- **Read-only evidence for RCA** (logs, metrics, Postgres, Kubernetes, GitHub): add a `tools:` section to `local/supportagent.yaml`. [supportagent.example.yaml](supportagent.example.yaml) is the full reference.
- **Your own stack:** skill packs, probes and lab scenarios. See [packs/README.md](packs/README.md) and the [extension walkthrough](docs/walkthroughs/wp-m09-disk-full.md).

## For developers

```bash
uv sync                                  # Python 3.12 and the locked dependencies
make check                               # ruff, ruff format, mypy --strict, import contracts
make test                                # unit and property tests
make ci-local                            # every CI job locally, both database engines
uv run supportagent --help
```

[docs/tech-spec/](docs/tech-spec/README.md) has the contracts, schema, test strategy and work packages. Decisions made after the spec freeze are in [docs/adr/](docs/adr/).

## Documentation

| If you want to… | Read |
|---|---|
| Understand the system visually in ten minutes | [docs/visual/blueprint.html](docs/visual/blueprint.html) (open in a browser) |
| Know the guarantees, contracts and schema (normative) | [docs/spec/ARCHITECTURE.md](docs/spec/ARCHITECTURE.md) |
| See the capability registry, tiers and release map | [docs/spec/CAPABILITIES.md](docs/spec/CAPABILITIES.md) |
| See phases, exit criteria, risks and metrics | [docs/spec/ROADMAP.md](docs/spec/ROADMAP.md) |
| Deploy outside a laptop (your own config, IdP, Postgres) | [docs/quickstart.md](docs/quickstart.md) |
| Understand why it wins (positioning, demo story) | [docs/PITCH.md](docs/PITCH.md) |
| Build it (contracts, schema, tests, standards, work packages) | [docs/tech-spec/](docs/tech-spec/README.md) |

Where documents disagree, **ARCHITECTURE.md wins**.

## Deploying for a team

Run the signed image from GitHub's container registry next to a managed Postgres, behind your identity provider and TLS: [server and cloud deployment](docs/deploy/cloud.md) (Docker Compose on a VM, Kubernetes, ECS, Cloud Run, Container Apps). [Releasing](docs/ops/releasing.md) explains how each release is checked before it's published.

## Security, data and contributing

- [Data handling](docs/data-handling.md): what SupportAgent reads, keeps and sends (only to the model you configure, after redaction).
- [Security policy](SECURITY.md): report vulnerabilities privately.
- [Contributing](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE).
