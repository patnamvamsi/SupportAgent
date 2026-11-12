# Deploying on a server or in the cloud

`./install.sh` is for a laptop. For a team, run the published image next to a managed Postgres, behind your identity provider and TLS. This page covers what any platform needs, then the deployment as Docker Compose on a VM and as Kubernetes.

## What the image is

`ghcr.io/patnamvamsi/supportagent:<version>`, built for linux/amd64 and linux/arm64. Each release is signed, with an SBOM and build provenance. Check it before deploying:

```bash
cosign verify ghcr.io/patnamvamsi/supportagent:<version> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity https://github.com/patnamvamsi/SupportAgent/.github/workflows/release.yml@refs/tags/v<version>
```

The image runs as uid 10001 and listens on port 8000. It carries everything the product ships: taxonomy, packs, plugins, prompts, runbooks, eval cases and the calibration artifact. You supply your config, your catalog and your secrets.

## What you provide

| | What | Notes |
|---|---|---|
| Database | Postgres 16 | Managed is easiest (RDS, Cloud SQL, Azure Database for PostgreSQL). `SUPPORTAGENT_DB_URL=postgresql+psycopg://user:pass@host:5432/db`. SQLite only suits one container with a persistent disk. |
| Config | `supportagent.yaml` at `/app/supportagent.yaml` | Start from [`supportagent.example.yaml`](../../supportagent.example.yaml). It names environment variables (`*_env`) and never holds secrets. |
| Catalog | your services at `/app/catalog/services.yaml` | Start from [`catalog/services.example.yaml`](../../catalog/services.example.yaml). |
| Secrets | environment variables from your secret manager | The ones your config names, plus `SA_MASTER_KEY_ENV` (64 hex characters, `openssl rand -hex 32`). Keep the master key out of database backups; without it, sealed credentials can't be read. |
| A writable volume | `/app/local`, persistent, writable by uid 10001 | It holds the credential vault, the onboarding workspace, installed plugins and fetched plugin catalogues. A few MB. |
| Sign-in | an OIDC provider | Keycloak, Okta, Entra ID, Google. Set the redirect URL to `https://<your host>/auth/callback`. |
| TLS | your ingress or load balancer | Serve only over HTTPS. With an `https:` redirect URL, session cookies are marked secure. |
| A model | a hosted API key, or an OpenAI-compatible or Ollama server the container can reach | See [data handling](../data-handling.md) for what reaches it. |

## Run it

1. **Migrate first, as a one-off job,** on every upgrade too: `supportagent migrate --config supportagent.yaml`. The app never migrates itself, and `/readyz` returns 503 until the database is at the image's head revision.
2. **Then start one container** running the default command (`run`): the API, the admin UI and the worker in one process.
3. **Probes:** liveness `GET /healthz`, readiness `GET /readyz`. `/readyz` returns 200 with the version and revision when the database is reachable and migrated.
4. **Keep it always on.** The worker polls ServiceNow and runs scheduled jobs, so a platform that scales to zero or throttles CPU between requests stops it. On Cloud Run, set the minimum instances to 1 and turn CPU throttling off. On Azure Container Apps, set the minimum replicas to 1.

**One instance per system** (ADR-0015). Run a single replica. Jobs take database leases, so a second copy wouldn't double-process, but more replicas aren't a supported or tested setup in 0.x.

## Docker Compose on a VM (any cloud)

```yaml
# compose.yaml next to supportagent.yaml, catalog/services.yaml and .env (chmod 600)
services:
  migrate:
    image: ghcr.io/patnamvamsi/supportagent:<version>
    command: ["migrate", "--config", "supportagent.yaml"]
    env_file: .env
    volumes: ["./supportagent.yaml:/app/supportagent.yaml:ro", "./catalog:/app/catalog:ro"]
  app:
    image: ghcr.io/patnamvamsi/supportagent:<version>
    depends_on: {migrate: {condition: service_completed_successfully}}
    env_file: .env
    ports: ["127.0.0.1:8000:8000"] # your TLS proxy (Caddy, nginx) in front
    volumes:
      - ./supportagent.yaml:/app/supportagent.yaml:ro
      - ./catalog:/app/catalog:ro
      - sa-local:/app/local
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz')"]
      interval: 30s
    restart: unless-stopped
volumes:
  sa-local:
```

## Kubernetes

The same shape: a Job (or an init container) for `migrate`, then a single-replica Deployment.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: supportagent}
spec:
  replicas: 1
  strategy: {type: Recreate} # one instance at a time
  selector: {matchLabels: {app: supportagent}}
  template:
    metadata: {labels: {app: supportagent}}
    spec:
      securityContext: {runAsUser: 10001, runAsGroup: 10001, fsGroup: 10001, runAsNonRoot: true}
      initContainers:
        - name: migrate
          image: ghcr.io/patnamvamsi/supportagent:<version>
          args: ["migrate", "--config", "supportagent.yaml"]
          envFrom: [{secretRef: {name: supportagent-env}}]
          volumeMounts: &mounts
            - {name: config, mountPath: /app/supportagent.yaml, subPath: supportagent.yaml}
            - {name: catalog, mountPath: /app/catalog}
            - {name: local, mountPath: /app/local}
      containers:
        - name: app
          image: ghcr.io/patnamvamsi/supportagent:<version>
          ports: [{containerPort: 8000}]
          envFrom: [{secretRef: {name: supportagent-env}}]
          volumeMounts: *mounts
          livenessProbe: {httpGet: {path: /healthz, port: 8000}, periodSeconds: 30}
          readinessProbe: {httpGet: {path: /readyz, port: 8000}, periodSeconds: 10}
          resources: {requests: {cpu: 250m, memory: 512Mi}, limits: {memory: 1Gi}}
      volumes:
        - {name: config, configMap: {name: supportagent-config}}
        - {name: catalog, configMap: {name: supportagent-catalog}}
        - {name: local, persistentVolumeClaim: {claimName: supportagent-local}}
```

Put a Service and your Ingress (with TLS) in front of port 8000.

## Managed container platforms

| Platform | Database | The `/app/local` volume | Watch out for |
|---|---|---|---|
| AWS ECS on Fargate | RDS for PostgreSQL | EFS | Run `migrate` as a one-off task before updating the service. |
| Google Cloud Run | Cloud SQL (Postgres) | A Cloud Storage or Filestore volume mount | Minimum instances 1, CPU always allocated. Run `migrate` as a Cloud Run job. |
| Azure Container Apps | Azure Database for PostgreSQL | Azure Files | Minimum replicas 1. Run `migrate` as a Container Apps job. |

## Upgrading

1. Read the release notes.
2. Back up the database ([restore](../ops/restore.md)).
3. Run `migrate` with the new image, then roll the app to it.

Migrations only add (06 §2), and CI checks every release against its predecessor's data on SQLite and Postgres (`scripts/upgrade-test.sh`). Rolling the app back to an older image after migrating isn't supported: restore the backup instead.
