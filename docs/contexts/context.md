# platform-agent — Agent Context

**Date:** 2026-05-02
**Branch:** `main`
**Scope:** Full project — all 3 implementation phases complete

---

## What this project is

A fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) that adds:
- A **Postgres backend** (Phase 1) alongside the existing SQLite backend
- A **FastAPI REST API with SSE streaming** (Phase 2) exposing all agent surfaces over HTTP
- **Docker + Helm chart** for deployment on AKS (Phase 3)

The upstream hermes-agent codebase is preserved untouched. All new surfaces are purely additive.

---

## Naming conventions

| Thing | Name |
|---|---|
| Upstream project | `hermes-agent` |
| This repo / Python package | `platform-agent` |
| Helm chart | `platform-agent` (`charts/platform-agent/`) |
| Docker image | `hermes-agent` (unchanged — matches ACR image name) |
| Python entry point | `hermes-api` (FastAPI server) |

The Helm chart was renamed from `hermes-agent` → `platform-agent` on 2026-05-02.
The Docker image name in `values.yaml` (`platformcrprd.azurecr.io/hermes-agent`) was intentionally kept.

---

## Implementation status

All phases are complete and on `main`.

| Phase | What | Key files |
|---|---|---|
| 1 | Postgres backend + Alembic migrations | `hermes_state/`, `alembic.ini`, `docker/compose.dev.yaml` |
| 2 | FastAPI REST API + SSE streaming | `hermes_api/` |
| 3 | Dockerfile + Helm chart + GitHub Actions CI/CD | `Dockerfile.k8s`, `charts/platform-agent/`, `.github/workflows/ci.yml`, `.github/workflows/release.yml` |

Detailed step-by-step docs per phase:
- `docs/phase1_postgres_backend.md`
- `docs/phase2_http_api.md`
- `docs/phase3_kubernetes.md`
- `docs/implementation_plan.md` — original 3-phase plan with decisions locked

---

## Key architecture decisions (locked)

- **Backend selection:** `HERMES_DB_URL` unset or `sqlite://…` → `SQLiteBackend` (default, zero migration). `postgresql+asyncpg://…` → `PostgresBackend`. The public name `SessionDB` is preserved — all callers unchanged.
- **Postgres engine:** async SQLAlchemy + asyncpg, running in a dedicated background thread. Public API is synchronous (wraps `asyncio.run_coroutine_threadsafe`) to avoid forcing async on the rest of the codebase.
- **API auth:** off by default. Gate: `HERMES_API_AUTH_ENABLED=true`. Validates `X-Api-Key` header. See `hermes_api/deps.py::require_auth()`.
- **Streaming:** SSE only (`text/event-stream`). Events: `delta`, `tool_call`, `tool_result`, `done`, `error`. Agent loop runs in a thread pool executor.
- **Platform registration:** the API registers as the `"api"` platform. `Platform.API` in `gateway/config.py`, `PLATFORM_HINTS["api"]` in `agent/prompt_builder.py`, toolset `hermes-api` in `toolsets.py`.
- **K8s state:** PVCs (Azure Files RWX) for `HERMES_HOME` (skills, memory, cron files). Postgres for sessions/messages/memory/cron.
- **Multi-tenancy:** `user_id TEXT NOT NULL DEFAULT 'default'` on all tables. No auth gate yet — schema-ready.

---

## Local dev setup

```bash
source venv/bin/activate   # always activate first (see AGENTS.md)

# SQLite (default — no infra needed)
uvicorn hermes_api.main:app --reload

# Postgres (local)
docker compose -f docker/compose.dev.yaml up -d
HERMES_DB_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes" \
  uvicorn hermes_api.main:app --reload

# Run DB migration
HERMES_DB_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes" \
  alembic upgrade head

# Migrate existing SQLite data to Postgres
HERMES_DB_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes" \
  hermes db migrate-from-sqlite --dry-run
```

---

## Testing

Always use the wrapper (see `AGENTS.md` Testing section):

```bash
scripts/run_tests.sh                         # full suite, SQLite
HERMES_TEST_PG_URL="postgresql+asyncpg://hermes:hermes@localhost:5432/hermes" \
  scripts/run_tests.sh tests/test_hermes_state.py  # dual-backend state tests
```

---

## CI/CD

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/ci.yml` | Pull request | pytest + ruff, docker build, `helm lint charts/platform-agent/`, `helm template` |
| `.github/workflows/release.yml` | `v*` tag push | Build + push `hermes-agent:<tag>` to ACR, package + push `platform-agent` chart to OCI registry |

---

## Helm chart quick reference

Chart: `charts/platform-agent/`

```bash
# Validate locally
helm lint charts/platform-agent/
helm template charts/platform-agent/ -f charts/platform-agent/values-dev.yaml

# Deploy to AKS
helm upgrade --install platform-agent charts/platform-agent/ \
  -f charts/platform-agent/values-dev.yaml \
  --set image.tag=<version>
```

Three workloads: `hermes-api` (HPA, min 2), `hermes-gateway` (single replica), `hermes-cron` (single replica).
All share one PVC (`hermes-home`, Azure Files RWX) and one Postgres instance (provisioned outside chart).

External secrets pulled from Azure Key Vault via External Secrets Operator.
See `charts/platform-agent/templates/secret-external.yaml` and `values.yaml::externalSecrets`.

---

## Files to read before changing anything

1. `AGENTS.md` — dev guide: file dependency chain, agent loop, tool registration, profiles, known pitfalls
2. `hermes_state/backend.py` — `StateBackend` Protocol (all backends must implement this)
3. `hermes_api/main.py` — FastAPI app lifespan, router registration
4. `charts/platform-agent/Chart.yaml` — chart identity (`name: platform-agent`, `version: 0.1.0`)
