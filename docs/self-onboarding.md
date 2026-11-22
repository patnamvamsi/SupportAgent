# Pointing SupportAgent at a new system

SupportAgent learns a system by itself (ADR-0023). Setting it up is three things:

1. **Install it** from GitHub.
2. **Give it** a language model, a sign-in, and where the system is: its code, and optionally its signals and tickets.
3. **Review** what it proposes.

Everything else is the instance's job, and it runs in the instance: reading the code, building the system graph, writing a summary of each component, drafting the service catalog, measuring detectors, evaluating the ones you enable, and triaging what fires. Nothing is run from a laptop, and nobody writes per-system code.

## 1. Install

On a machine with Docker:

```
git clone https://github.com/patnamvamsi/SupportAgent && cd SupportAgent
./install.sh
```

The installer asks, in this order:

| Question | Give it | Why |
|---|---|---|
| Language model | An Anthropic or OpenAI key, or the URL of a local server (LM Studio, vLLM, llama.cpp, Ollama) | Summaries, triage and RCA. Text is redacted before it's sent. |
| The system | Its name, and where its code is: **a checkout on this machine** (mounted read-only) and/or **GitHub repositories** with a read-only token | The code is the most reliable description of a system. |
| Signals (optional) | Prometheus and Loki URLs, and whether an Alertmanager or Grafana will send alerts | Detectors are measured from this history. Alerts are an intake. |
| Tickets | A ServiceNow instance, the demo, or **none** | With none, alerts are the only intake (shadow mode). |
| Confluence, PagerDuty (optional) | A space, or a REST key | Docs as guidance, and notes on open pages. |
| Your sign-in | A user name and password | A bundled Keycloak. The first person to sign in is the admin. |

For an unattended install, put the same answers in a JSON file and run `./install.sh --answers answers.json`. Here's an alerts-only example:

```json
{
  "team_name": "Acme",
  "llm": {"kind": "local", "base_url": "http://localhost:1234/v1", "model_id": "qwen/qwen3.8-27b"},
  "servicenow": {"kind": "none"},
  "onboard": {"system_name": "Trading", "local_dir": "/home/ada/trading"},
  "signals": {"prometheus_url": "http://localhost:9090", "loki_url": "http://localhost:3100"},
  "admin_password": "change-me"
}
```

When it finishes, the installer hands the system to the instance and prints where to sign in.

## 2. What the instance does by itself

Open **Onboarding** (`/ui/onboarding`). Onboarding runs in the background, now and every 24 hours. You can also start it with **Run onboarding now**, which continues where the last run stopped. Its steps:

| Step | What it does |
|---|---|
| ingest | Reads the sources (read-only). Dependencies (virtualenvs, `node_modules`) and secret-named files (`.env`, keys, token and password files) are never read, whatever the paths say. Code is re-read hourly, and tickets and docs daily. |
| sense | Detects the stack from what it read: which plugins apply (Python, Postgres, Docker Compose, Kafka, …). |
| graph | Builds the system graph from the code and config, with every fact citing `file:line`: services, jobs, topics, tables and external APIs. The model writes a 2–4 sentence summary of each component, and every sentence must cite real facts or it's dropped. |
| catalog | Drafts a service entry for each component. |
| detectors | Measures 7 days of Prometheus and Loki and proposes detectors with measured thresholds. A signal with too little or too irregular a history isn't proposed, and the reason is shown. It also notes which services are running and which are only defined. |
| incidents, readiness, … | When there is a ticket history: which incident classes it can handle, and what it needs to handle more. |

What it needs from a person is listed under **Needed from you**, most valuable first: access to grant, facts to confirm, services to accept, detectors to enable.

## 3. Review

- **System** (`/ui/system`): every component with its summary, what it publishes, consumes, writes, reads, runs and calls, and its detectors and cases.
- **Review the graph:** confirm or reject facts. "Confirm all" accepts everything that came from code and config at once.
- **Catalog** (`/ui/onboarding/catalog`): accept the services the instance should know, each with a paging integration (`pagerduty:…`, `opsgenie:…`, or `none:<who>` when there's no pager). They take effect at once.
- **Detectors** (`/ui/onboarding/detectors`): enable the ones you want. The instance evaluates them itself every minute, and a firing one becomes a case. If your own Alertmanager already sends the same alerts, leave them off here, so nothing fires twice.

## Changing what it reads

Edit the manifest on **Onboarding → The system's sources** (`/ui/onboarding/system`). It names the system and its sources. Credentials stay in the instance's environment: the manifest names the variable (`token_env: GITHUB_TOKEN`) and never holds a secret, and a pasted secret is refused. For example:

```yaml
system:
  id: trading
  name: Trading platform
sources:
  git:
    - repo: file:/mnt/system            # the checkout mounted read-only
      paths: ["**/*.py", "**/*compose*.y*ml", "**/Dockerfile*", "**/*.sql"]
      exclude: ["backups/**"]           # an archived copy would read as a second system
    - repo: github:acme/*
      paths: ["**/*.py", "**/pom.xml"]
      token_env: GITHUB_TOKEN
budget:
  llm_tokens: 2000000                   # caps the model's work per run
```

A changed manifest takes effect on the next run.

## When it can't read something

A directory the instance's user may not open is skipped, never fatal. It's listed as an access need ("some sources can't be read by the instance"). Either give that user read access, or run the instance as a user who can read the checkout.
