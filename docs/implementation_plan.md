# Hermes Agent — Transformation Implementation Plan

## 🎯 Decisions Locked

| # | Decision |
|---|---------|
| API scope | **Full** — chat, sessions, skills, memory, config, cron, tools, platforms |
| Auth | None for now — reserve `X-Api-Key` middleware stub |
| Multi-tenancy | **Schema-ready, not enforced** — `user_id` column everywhere (default `"default"`), indexed; no auth gate yet |
| Streaming | **SSE** — simpler than WS, HTTP/2 friendly, trivial behind nginx ingress |
| Filesystem state | **PVCs** (Azure Files RWX) |
| Infra | **AKS + Helm chart** (no Helmfile) |
| Gateway | **Preserved** — runs as separate `Deployment` |
| Search | **pgvector** for semantic + `pg_trgm` for lexical fallback |

---

## 📦 Phase 1 — Postgres Backend (with pgvector, multi-user-ready schema)

### 1.1 Abstraction

`hermes_state.py` refactored into a `StateBackend` Protocol:

```
hermes_state/
├── __init__.py          # SessionDB facade, backend selection via HERMES_DB_URL
├── backend.py           # StateBackend Protocol
├── sqlite_backend.py    # current implementation moved here (default)
├── postgres_backend.py  # new
└── migrations/          # alembic — Postgres only
```

Backend selection:
- `HERMES_DB_URL` unset or `sqlite:///…` → SQLiteBackend (backward compatible, zero migration)
- `postgresql+asyncpg://…` → PostgresBackend

### 1.2 Schema (Postgres)

All tables carry `user_id TEXT NOT NULL DEFAULT 'default'` + composite indexes `(user_id, …)`. Ready for multi-user with zero schema change later — only auth layer to add.

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE sessions (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL DEFAULT 'default',
    platform      TEXT NOT NULL,
    title         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_sessions_user_updated ON sessions (user_id, updated_at DESC);

CREATE TABLE messages (
    id            BIGSERIAL PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL DEFAULT 'default',
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    reasoning     TEXT,
    tool_calls    JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding     vector(1536),
    content_tsv   tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
);
CREATE INDEX idx_messages_session ON messages (session_id, created_at);
CREATE INDEX idx_messages_user ON messages (user_id, created_at DESC);
CREATE INDEX idx_messages_tsv ON messages USING GIN (content_tsv);
CREATE INDEX idx_messages_trgm ON messages USING GIN (content gin_trgm_ops);
CREATE INDEX idx_messages_embedding ON messages USING hnsw (embedding vector_cosine_ops);

CREATE TABLE memory_entries (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT NOT NULL DEFAULT 'default',
    kind          TEXT NOT NULL,            -- 'memory' | 'user_profile'
    content       TEXT NOT NULL,
    embedding     vector(1536),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_memory_user_kind ON memory_entries (user_id, kind);
CREATE INDEX idx_memory_embedding ON memory_entries USING hnsw (embedding vector_cosine_ops);

CREATE TABLE cron_jobs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT NOT NULL DEFAULT 'default',
    schedule      TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    deliver       TEXT,
    enabled       BOOLEAN NOT NULL DEFAULT true,
    last_run_at   TIMESTAMPTZ,
    next_run_at   TIMESTAMPTZ,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_cron_due ON cron_jobs (enabled, next_run_at) WHERE enabled;
```

### 1.3 Dependencies

```toml
[project.optional-dependencies]
postgres = [
  "sqlalchemy[asyncio]>=2.0",
  "asyncpg>=0.29",
  "pgvector>=0.3",
  "alembic>=1.13",
]
```

### 1.4 Deliverables

- PR #1: `hermes db migrate-from-sqlite` CLI, parametrized tests across both backends, Alembic baseline migration.
- No behavior change when `HERMES_DB_URL` unset.

---

## 🌐 Phase 2 — HTTP REST API (FastAPI + SSE)

### 2.1 Layout

```
hermes_api/
├── main.py              # FastAPI app + lifespan (DB pool, agent registry)
├── deps.py              # get_agent, get_db, auth stub
├── middleware.py        # request-id, structured logging, ddtrace
├── errors.py            # domain → HTTP mapping
└── routers/
    ├── conversations.py # chat + SSE streaming
    ├── sessions.py      # list/search/summaries
    ├── skills.py        # CRUD, hub search, install
    ├── memory.py        # memory & user profile CRUD
    ├── config.py        # get/set config values
    ├── cron.py          # jobs CRUD
    ├── tools.py         # introspection, invoke
    ├── platforms.py     # gateway status, send_message
    └── health.py        # /health, /ready, /metrics
```

### 2.2 Endpoint Surface (full)

```
POST   /v1/conversations                           create session
GET    /v1/conversations                           list (paginated, ?user_id=…)
GET    /v1/conversations/{id}                      get with messages
DELETE /v1/conversations/{id}
POST   /v1/conversations/{id}/messages             send (stream=true → SSE)
POST   /v1/conversations/{id}/compress             /compress
POST   /v1/conversations/{id}/reset                /new
GET    /v1/sessions/search?q=…                     semantic+FTS hybrid
GET    /v1/sessions/{id}/summary

GET    /v1/skills                                  list
POST   /v1/skills                                  create
GET    /v1/skills/{name}
PUT    /v1/skills/{name}
DELETE /v1/skills/{name}
GET    /v1/skills/hub/search?q=…
POST   /v1/skills/hub/install

GET    /v1/memory?kind=memory|user_profile
POST   /v1/memory
PUT    /v1/memory/{id}
DELETE /v1/memory/{id}

GET    /v1/config                                  effective config (redacted)
PATCH  /v1/config                                  set values
POST   /v1/config/model                            switch model

GET    /v1/cron
POST   /v1/cron
PUT    /v1/cron/{id}
DELETE /v1/cron/{id}

GET    /v1/tools                                   schemas
POST   /v1/tools/{name}/invoke                     direct call (admin)

GET    /v1/platforms                               gateway status
POST   /v1/platforms/{platform}/send               send_message tool

GET    /v1/health                                  liveness (no deps)
GET    /v1/ready                                   readiness (DB + pool)
GET    /metrics                                    Prometheus
```

### 2.3 Streaming (SSE)

`POST /v1/conversations/{id}/messages?stream=true` returns `text/event-stream` with events: `delta`, `tool_call`, `tool_result`, `done`, `error`. Reuses `AIAgent.run_conversation` via callbacks converted to an async generator.

### 2.4 Platform Integration

Per `ADDING_A_PLATFORM.md`, register `"api"` as a platform:
- `Platform.API = "api"` in `gateway/config.py`
- `PLATFORM_HINTS["api"]` — plain text, markdown OK, no emoji restrictions
- Toolset `hermes-api` in `toolsets.py`

### 2.5 Deliverables

- PR #2: FastAPI app, OpenAPI at `/docs`, 90%+ test coverage on routers, auth middleware stub returning 200 (behavior switchable via `HERMES_API_AUTH_ENABLED=true` later).

---

## ☸️ Phase 3 — Kubernetes (Docker + Helm chart)

### 3.1 Dockerfile (multi-stage, non-root, pinned)

```
charts/platform-agent/
├── Chart.yaml
├── values.yaml
├── values-dev.yaml
├── values-prod.yaml
└── templates/
    ├── _helpers.tpl
    ├── serviceaccount.yaml
    ├── configmap.yaml
    ├── secret-external.yaml          # ExternalSecret (Azure Key Vault)
    ├── pvc-hermes-home.yaml          # Azure Files RWX — skills/memory/cron files
    ├── api-deployment.yaml
    ├── api-service.yaml
    ├── api-hpa.yaml
    ├── api-ingress.yaml              # nginx + cert-manager
    ├── api-servicemonitor.yaml
    ├── gateway-deployment.yaml       # single replica, preserves messaging adapters
    ├── gateway-service.yaml
    ├── cron-deployment.yaml          # scheduler (single replica, leader-elected)
    ├── migration-job.yaml            # alembic upgrade on install/upgrade
    ├── networkpolicy.yaml
    └── pdb.yaml
Dockerfile
.dockerignore
```

### 3.2 Topology

| Component | Replicas | Storage | Why |
|----------|---------|---------|-----|
| `hermes-api` | 2+ HPA | PVC `hermes-home` (RWX) for skills/memory files | stateless HTTP, scales horizontally |
| `hermes-gateway` | 1 | shares `hermes-home` | messaging sessions are long-lived, platform adapters hold connections |
| `hermes-cron` | 1 | shares `hermes-home` | leader elected, avoid double-firing jobs |
| `migration` (Job) | 1 | — | alembic upgrade head, `helm.sh/hook: pre-upgrade,pre-install` |

All three consume the same Postgres (Azure DB for PostgreSQL Flexible Server — provisioned outside chart).

### 3.3 Probes, Shutdown, Observability

- **Liveness** `/v1/health` — process-only.
- **Readiness** `/v1/ready` — Postgres `SELECT 1` + PVC writable check.
- **Graceful**: `terminationGracePeriodSeconds: 60`, FastAPI lifespan drains active SSE streams.
- **Datadog**: Admission controller auto-injection (cluster standard); APM via `DD_*` env; logs JSON on stdout; custom metrics at `/metrics` scraped by DD cluster agent.
- **Secrets**: `ExternalSecret` CRD pulling from Azure Key Vault (DB creds, LLM provider keys, messaging tokens).

### 3.4 CI/CD (GitHub Actions)

```yaml
.github/workflows/
├── ci.yml           # lint, pytest (w/ postgres service), build image, push to ACR, helm lint, helm template diff
└── release.yml      # tag → build+push versioned image, package+push chart to OCI registry
```

Chart deployment itself remains manual `helm upgrade --install` per env for now (Helmfile out of scope).

### 3.5 Deliverables

- PR #3: Dockerfile, Helm chart (lint + template tested), GitHub Actions, README with deploy instructions.

---

## 🗓️ Sequencing & PR Plan

| Week | PR | Content | Risk |
|------|----|---------|------|
| 1–2 | PR #1 | Postgres backend + pgvector + migrations + sqlite→pg migration CLI | FTS parity regression |
| 3–4 | PR #2 | FastAPI full surface + SSE + `"api"` platform registration | Agent loop coupling to sync callbacks |
| 5 | PR #3a | Dockerfile + local `docker compose` (api + gateway + postgres) | — |
| 6 | PR #3b | Helm chart + CI + AKS deploy docs | PVC RWX perf with Azure Files |

---

