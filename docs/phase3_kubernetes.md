# Phase 3 — Kubernetes & Delivery

## Overview

Package and deploy Hermes Agent on AKS using a multi-stage non-root Docker
image and a Helm chart. Three separate workloads: API (HPA), gateway (single
replica), and cron (single replica). CI/CD via GitHub Actions. Observability
via Datadog, Prometheus, structured JSON logs.

**Warp plan ID:** `a78f381d-f92e-43d6-b64e-823b7f3eff83`

---

## Prerequisites

- Phase 1 and Phase 2 complete and merged
- External infra provisioned (outside this chart):
  - Azure DB for PostgreSQL Flexible Server
  - Azure Container Registry (ACR)
  - AKS cluster with:
    - External Secrets Operator installed
    - cert-manager installed
    - nginx ingress controller installed
    - Datadog cluster agent with admission controller enabled (optional)
    - Prometheus operator installed (optional)

---

## Step-by-step implementation

### Step 1 — Replace `Dockerfile`

Replace the existing root `Dockerfile` with a two-stage build.

**Stage 1 — builder:**

```dockerfile
FROM python:3.12.9-slim AS builder
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY . .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install \
      ".[web,postgres,cron,messaging,mcp,pty]"
```

**Stage 2 — runtime:**

```dockerfile
FROM python:3.12.9-slim AS runtime
RUN useradd --uid 1000 --gid 1000 --no-create-home hermes
COPY --from=builder /install /usr/local
COPY --from=builder /build /app
WORKDIR /app
USER hermes:hermes
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/v1/health')"
CMD ["hermes-api"]
```

Key rules:
- Pin the base image to an exact minor version (`python:3.12.9-slim`) — never `latest`
- Non-root user `uid=1000 gid=1000`
- `CMD` defaults to API; gateway and cron override via Helm `args`

**Create `.dockerignore`:**

```
tests/
docs/
ui-tui/node_modules/
.git/
venv/
*.egg-info/
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.mypy_cache/
```

**Validate:**

```bash
docker build -t hermes-agent:local .
docker run --rm -e HERMES_DB_URL=sqlite:/// hermes-agent:local python -c "import hermes_api"
```

**Commit after this step.**

---

### Step 2 — `charts/platform-agent/Chart.yaml`

```yaml
apiVersion: v2
name: platform-agent
description: Hermes AI Agent — API, gateway, and cron
type: application
version: 0.1.0
appVersion: "0.10.0"
```

---

### Step 3 — `charts/platform-agent/values.yaml`

```yaml
image:
  repository: <your-acr>.azurecr.io/hermes-agent
  tag: "0.10.0"
  pullPolicy: IfNotPresent

replicaCount:
  api: 2

serviceAccount:
  create: true
  name: platform-agent

postgres:
  # Populated via ExternalSecret — do not set here
  urlSecretRef:
    name: hermes-secrets
    key: HERMES_DB_URL

hermesHome:
  storageClassName: azurefile-csi-premium
  size: 10Gi

api:
  port: 8000
  resources:
    requests:
      cpu: 200m
      memory: 512Mi
    limits:
      cpu: 1000m
      memory: 1Gi
  hpa:
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70
  ingress:
    enabled: true
    className: nginx
    host: hermes-api.example.com
    tls:
      enabled: true
      secretName: hermes-api-tls

gateway:
  enabled: true
  resources:
    requests:
      cpu: 100m
      memory: 256Mi

cron:
  enabled: true
  resources:
    requests:
      cpu: 50m
      memory: 128Mi

datadog:
  enabled: false
  env: production
  service: hermes-agent

externalSecrets:
  enabled: true
  secretStoreRef:
    name: azure-keyvault
    kind: SecretStore
  remoteRefs:
    HERMES_DB_URL: hermes-db-url
    HERMES_API_KEY: hermes-api-key
    ANTHROPIC_API_KEY: anthropic-api-key
    OPENAI_API_KEY: openai-api-key
```

---

### Step 4 — `charts/platform-agent/templates/_helpers.tpl`

```
{{- define "hermes.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "hermes.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}
```

---

### Step 5 — PVC for `hermes-home`

`templates/pvc-hermes-home.yaml`:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "hermes.fullname" . }}-home
  labels: {{ include "hermes.labels" . | nindent 4 }}
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: {{ .Values.hermesHome.storageClassName }}
  resources:
    requests:
      storage: {{ .Values.hermesHome.size }}
```

All three workloads mount this PVC at `/home/hermes/.hermes` (or
`HERMES_HOME` env var value).

---

### Step 6 — ExternalSecret

`templates/secret-external.yaml`:

```yaml
{{- if .Values.externalSecrets.enabled }}
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: {{ include "hermes.fullname" . }}-secrets
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: {{ .Values.externalSecrets.secretStoreRef.name }}
    kind: {{ .Values.externalSecrets.secretStoreRef.kind }}
  target:
    name: hermes-secrets
    creationPolicy: Owner
  data:
  {{- range $key, $remoteRef := .Values.externalSecrets.remoteRefs }}
  - secretKey: {{ $key }}
    remoteRef:
      key: {{ $remoteRef }}
  {{- end }}
{{- end }}
```

---

### Step 7 — Migration Job

`templates/migration-job.yaml`:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "hermes.fullname" . }}-migrate
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-1"
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  template:
    spec:
      serviceAccountName: {{ .Values.serviceAccount.name }}
      restartPolicy: OnFailure
      containers:
        - name: migrate
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          command: ["alembic", "upgrade", "head"]
          envFrom:
            - secretRef:
                name: hermes-secrets
```

---

### Step 8 — API Deployment

`templates/api-deployment.yaml` — key sections:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "hermes.fullname" . }}-api
spec:
  replicas: {{ .Values.replicaCount.api }}
  selector:
    matchLabels:
      app.kubernetes.io/component: api
  template:
    metadata:
      labels:
        app.kubernetes.io/component: api
        {{- if .Values.datadog.enabled }}
        admission.datadoghq.com/enabled: "true"
        {{- end }}
    spec:
      serviceAccountName: {{ .Values.serviceAccount.name }}
      containers:
        - name: api
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          ports:
            - containerPort: {{ .Values.api.port }}
          envFrom:
            - secretRef:
                name: hermes-secrets
          env:
            - name: HERMES_RUN_MODE
              value: api
            - name: HERMES_HOME
              value: /hermes-home
            {{- if .Values.datadog.enabled }}
            - name: DD_ENV
              value: {{ .Values.datadog.env }}
            - name: DD_SERVICE
              value: {{ .Values.datadog.service }}
            - name: DD_VERSION
              value: {{ .Values.image.tag }}
            {{- end }}
          resources: {{ .Values.api.resources | toYaml | nindent 12 }}
          livenessProbe:
            httpGet:
              path: /v1/health
              port: {{ .Values.api.port }}
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /v1/ready
              port: {{ .Values.api.port }}
            initialDelaySeconds: 5
            periodSeconds: 10
          volumeMounts:
            - name: hermes-home
              mountPath: /hermes-home
      terminationGracePeriodSeconds: 60
      volumes:
        - name: hermes-home
          persistentVolumeClaim:
            claimName: {{ include "hermes.fullname" . }}-home
```

---

### Step 9 — Gateway and Cron Deployments

`templates/gateway-deployment.yaml` — same structure as API deployment but:

```yaml
replicas: 1
containers:
  - name: gateway
    command: ["hermes", "gateway"]   # or HERMES_RUN_MODE=gateway
```

`templates/cron-deployment.yaml` — same but:

```yaml
replicas: 1
containers:
  - name: cron
    command: ["hermes", "cron"]   # or HERMES_RUN_MODE=cron
```

Both mount the same `hermes-home` PVC.

---

### Step 10 — HPA, Ingress, ServiceMonitor, PDB, NetworkPolicy

**`api-hpa.yaml`:**

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "hermes.fullname" . }}-api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "hermes.fullname" . }}-api
  minReplicas: {{ .Values.api.hpa.minReplicas }}
  maxReplicas: {{ .Values.api.hpa.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.api.hpa.targetCPUUtilizationPercentage }}
```

**`api-ingress.yaml`** — nginx class, cert-manager annotation for TLS.

**`api-servicemonitor.yaml`** — Prometheus ServiceMonitor scraping `/metrics`
on port `{{ .Values.api.port }}`.

**`pdb.yaml`** — `minAvailable: 1` for the API deployment.

**`networkpolicy.yaml`** — allow ingress from nginx namespace and Postgres
egress; deny all else by default.

**Commit after this step.**

---

### Step 11 — Values files

`values-dev.yaml`:

```yaml
replicaCount:
  api: 1
api:
  hpa:
    minReplicas: 1
    maxReplicas: 1
  ingress:
    enabled: false
datadog:
  enabled: false
externalSecrets:
  enabled: false
```

`values-prod.yaml`:

```yaml
datadog:
  enabled: true
  env: production
externalSecrets:
  enabled: true
```

---

### Step 12 — CI workflow (`.github/workflows/ci.yml`)

```yaml
name: CI
on:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_DB: hermes
          POSTGRES_USER: hermes
          POSTGRES_PASSWORD: hermes
        ports: ["5432:5432"]
        options: --health-cmd "pg_isready -U hermes" --health-interval 5s --health-retries 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev,web,postgres]"
      - run: ruff check .
      - run: scripts/run_tests.sh
        env:
          HERMES_TEST_PG_URL: postgresql+asyncpg://hermes:hermes@localhost:5432/hermes

  docker:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t hermes-agent:ci .

  helm:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: azure/setup-helm@v4
      - run: helm lint charts/platform-agent/
      - run: helm template charts/platform-agent/ -f charts/platform-agent/values-dev.yaml
```

---

### Step 13 — Release workflow (`.github/workflows/release.yml`)

```yaml
name: Release
on:
  push:
    tags: ["v*"]

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: azure/login@v2
        with:
          creds: ${{ secrets.AZURE_CREDENTIALS }}
      - run: az acr login --name ${{ secrets.ACR_NAME }}
      - name: Build and push image
        run: |
          IMAGE="${{ secrets.ACR_NAME }}.azurecr.io/hermes-agent:${GITHUB_REF_NAME}"
          docker build -t "$IMAGE" .
          docker push "$IMAGE"
      - uses: azure/setup-helm@v4
      - name: Package and push chart
        run: |
          helm package charts/platform-agent/
          helm push platform-agent-*.tgz oci://${{ secrets.ACR_NAME }}.azurecr.io/helm
```

**Required GitHub secrets:** `AZURE_CREDENTIALS`, `ACR_NAME`

**Commit after this step.**

---

### Step 14 — Final validation

```bash
# Lint and template
helm lint charts/platform-agent/
helm template charts/platform-agent/ -f charts/platform-agent/values-dev.yaml > /dev/null

# Local Docker build
docker build -t hermes-agent:local .

# CI-parity test run (no Postgres needed for default suite)
scripts/run_tests.sh
```

---

## Deployment (manual per environment)

```bash
# First install
helm upgrade --install platform-agent charts/platform-agent/ \
  -f charts/platform-agent/values-dev.yaml \
  --namespace hermes --create-namespace \
  --set image.tag=<version>

# Upgrade
helm upgrade platform-agent charts/platform-agent/ \
  -f charts/platform-agent/values-prod.yaml \
  --set image.tag=<new-version>
```

---

## Commit checklist

1. `Dockerfile` (multi-stage) + `.dockerignore`
2. `charts/platform-agent/Chart.yaml` + `values.yaml`
3. `_helpers.tpl` + `pvc-hermes-home.yaml`
4. `secret-external.yaml`
5. `migration-job.yaml`
6. `api-deployment.yaml` + `api-service.yaml`
7. `gateway-deployment.yaml` + `cron-deployment.yaml`
8. `api-hpa.yaml` + `api-ingress.yaml` + `api-servicemonitor.yaml` + `pdb.yaml` + `networkpolicy.yaml`
9. `values-dev.yaml` + `values-prod.yaml`
10. `.github/workflows/ci.yml`
11. `.github/workflows/release.yml`
