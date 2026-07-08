#!/usr/bin/env bash
#
# Deploy the CodeDoc MCP server to Google Cloud Run.
#
# Usage (from project root):
#   ./mcp_server/deploy.sh
#
# Configurable via env vars (with sensible defaults):
#   PROJECT_ID                — GCP project (default: development-459201)
#   REGION                    — Cloud Run region (default: asia-northeast1)
#   SERVICE_NAME              — Cloud Run service (default: codedoc-mcp)
#   SERVICE_ACCOUNT_NAME      — SA short name (default: codedoc-mcp-sa)
#   SPANNER_INSTANCE          — default: java-codegraph
#   SPANNER_DATABASE          — default: java-codegraph-db
#   GRAPH_NAME                — default: code_graph_a
#   MIN_INSTANCES, MAX_INSTANCES, MEMORY, CPU, TIMEOUT, CONCURRENCY — Cloud Run sizing.
#
# Idempotent: re-running upgrades the service and re-binds roles without harm.

set -euo pipefail

# --- config -----------------------------------------------------------------
PROJECT_ID="${PROJECT_ID:-code-doc-graph}"
REGION="${REGION:-asia-northeast1}"
SERVICE_NAME="${SERVICE_NAME:-codedoc-mcp}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-codedoc-mcp-sa}"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

SPANNER_INSTANCE="${SPANNER_INSTANCE:-java-codegraph}"
SPANNER_DATABASE="${SPANNER_DATABASE:-java-codegraph-db}"
GRAPH_NAME="${GRAPH_NAME:-code_graph_a}"

MEMORY="${MEMORY:-2Gi}"
CPU="${CPU:-2}"
TIMEOUT="${TIMEOUT:-300}"
CONCURRENCY="${CONCURRENCY:-10}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-5}"

IMAGE="gcr.io/${PROJECT_ID}/codedoc-mcp:latest"

# --- preflight --------------------------------------------------------------
if [[ ! -f mcp_server/Dockerfile ]]; then
  echo "ERROR: run this script from the project root (mcp_server/Dockerfile not found at $(pwd)/mcp_server/Dockerfile)" >&2
  exit 1
fi

ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
if [[ -z "${ACTIVE_ACCOUNT}" ]]; then
  echo "ERROR: no active gcloud account. Run 'gcloud auth login' first." >&2
  exit 1
fi

echo "==> Deploying codedoc-mcp"
echo "    project:         ${PROJECT_ID}"
echo "    region:          ${REGION}"
echo "    service:         ${SERVICE_NAME}"
echo "    service account: ${SERVICE_ACCOUNT_EMAIL}"
echo "    image:           ${IMAGE}"
echo "    active gcloud:   ${ACTIVE_ACCOUNT}"

# --- enable required APIs (idempotent) --------------------------------------
echo "==> Ensuring required APIs are enabled"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  containerregistry.googleapis.com \
  aiplatform.googleapis.com \
  spanner.googleapis.com \
  --project "${PROJECT_ID}" \
  --quiet

# --- service account --------------------------------------------------------
if ! gcloud iam service-accounts describe "${SERVICE_ACCOUNT_EMAIL}" \
       --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "==> Creating service account ${SERVICE_ACCOUNT_EMAIL}"
  gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
    --project "${PROJECT_ID}" \
    --display-name "CodeDoc MCP server"
else
  echo "==> Service account ${SERVICE_ACCOUNT_EMAIL} already exists"
fi

echo "==> Binding IAM roles"
for role in roles/aiplatform.user roles/spanner.databaseUser; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${SERVICE_ACCOUNT_EMAIL}" \
    --role "${role}" \
    --condition=None \
    --quiet >/dev/null
  echo "    bound ${role}"
done

# --- build ------------------------------------------------------------------
# Cloud Build only auto-populates SHORT_SHA when triggered by a push, not when
# invoked via `gcloud builds submit`. Compute it locally from git so the image
# gets a real sha-tagged version (falls back to "manual" outside a git checkout).
SHORT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"

echo "==> Building image via Cloud Build (tag: ${SHORT_SHA})"
gcloud builds submit . \
  --project "${PROJECT_ID}" \
  --config mcp_server/cloudbuild.yaml \
  --substitutions "SHORT_SHA=${SHORT_SHA}"

# --- deploy -----------------------------------------------------------------
echo "==> Deploying to Cloud Run"
gcloud run deploy "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${SERVICE_ACCOUNT_EMAIL}" \
  --no-allow-unauthenticated \
  --session-affinity \
  --memory "${MEMORY}" \
  --cpu "${CPU}" \
  --timeout "${TIMEOUT}" \
  --concurrency "${CONCURRENCY}" \
  --min-instances "${MIN_INSTANCES}" \
  --max-instances "${MAX_INSTANCES}" \
  --set-env-vars "^##^GOOGLE_CLOUD_PROJECT=${PROJECT_ID}##GOOGLE_GENAI_USE_VERTEXAI=true##SPANNER_INSTANCE=${SPANNER_INSTANCE}##SPANNER_DATABASE=${SPANNER_DATABASE}##GRAPH_NAME=${GRAPH_NAME}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" --region "${REGION}" --format='value(status.url)')"

# --- post-deploy instructions -----------------------------------------------
cat <<EOF

==============================================================================
Deploy complete.

  Service:  ${SERVICE_NAME}
  URL:      ${SERVICE_URL}
  Auth:     IAM (no public access)

Grant yourself the Cloud Run Invoker role (one-time, per principal):

  gcloud run services add-iam-policy-binding ${SERVICE_NAME} \\
    --project ${PROJECT_ID} \\
    --region ${REGION} \\
    --member user:${ACTIVE_ACCOUNT} \\
    --role roles/run.invoker

Then expose it to your local Claude Code via the gcloud proxy:

  gcloud run services proxy ${SERVICE_NAME} \\
    --project ${PROJECT_ID} \\
    --region ${REGION} \\
    --port 8080

…and add this to your .mcp.json (the URL is the same as the local-dev URL):

  {
    "mcpServers": {
      "codedoc": {
        "type": "http",
        "url": "http://127.0.0.1:8080/mcp"
      }
    }
  }
==============================================================================
EOF
