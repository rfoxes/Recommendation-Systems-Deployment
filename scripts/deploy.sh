#!/usr/bin/env bash
# Deploy to Cloud Run on the free tier.   Usage: scripts/deploy.sh   (reads .env.cloud; see .env.cloud.example)
#
#  - Secrets go to Secret Manager (a new version only when a value changed; older versions are destroyed to
#    stay inside the free tier), never into the image or the service's plain env vars.
#  - One container runs the API and the Temporal worker: scales to zero, at most 1 instance, CPU kept while
#    an instance is alive so the worker can poll (instance-based billing, inside the always-free tier).
#  - The image is built by Cloud Build (amd64) from the Dockerfile, which downloads the pinned CTR model.
set -euo pipefail
export CLOUDSDK_ACTIVE_CONFIG_NAME=simula-personal  # this project's personal gcloud config, never the default
cd "$(dirname "$0")/.."
set -a; source .env.cloud; set +a

: "${GCP_PROJECT:?set GCP_PROJECT in .env.cloud}" "${MONGO_URI:?}" "${REDIS_URL:?}" "${CLICK_API_KEY:?}"
: "${TEMPORAL_ADDRESS:?}" "${TEMPORAL_NAMESPACE:?}" "${TEMPORAL_API_KEY:?}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-simula-api}"
LLM_PROVIDER="${LLM_PROVIDER:-gemini}"
gc() { gcloud --project "$GCP_PROJECT" --quiet "$@"; }

echo "== Enabling APIs"
gc services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

echo "== Secrets"
RUNTIME_SA="$(gc projects describe "$GCP_PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
SECRET_FLAGS=()
for var in MONGO_URI REDIS_URL TEMPORAL_API_KEY CLICK_API_KEY GEMINI_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY; do
  value="${!var:-}"
  [ -z "$value" ] && continue
  id="$(echo "$var" | tr 'A-Z_' 'a-z-')"
  gc secrets describe "$id" >/dev/null 2>&1 || gc secrets create "$id" --replication-policy=automatic >/dev/null
  if [ "$(gc secrets versions access latest --secret="$id" 2>/dev/null || true)" != "$value" ]; then
    printf '%s' "$value" | gc secrets versions add "$id" --data-file=- >/dev/null
    for old in $(gc secrets versions list "$id" --filter='state=ENABLED' --format='value(name)' --sort-by=~createTime | tail -n +2); do
      gc secrets versions destroy "$old" --secret="$id" >/dev/null
    done
    echo "  $id: updated"
  fi
  gc secrets add-iam-policy-binding "$id" --member="serviceAccount:$RUNTIME_SA" --role=roles/secretmanager.secretAccessor >/dev/null
  SECRET_FLAGS+=("$var=$id:latest")
done

echo "== Deploying $SERVICE to $REGION"
gc run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
  --min-instances 0 --max-instances 1 --no-cpu-throttling --cpu 1 --memory 1Gi --cpu-boost \
  --concurrency 80 --timeout 30 \
  --set-env-vars "MONGO_DB=${MONGO_DB:-simula},LLM_PROVIDER=$LLM_PROVIDER,TEMPORAL_ADDRESS=$TEMPORAL_ADDRESS,TEMPORAL_NAMESPACE=$TEMPORAL_NAMESPACE${LLM_MODEL:+,LLM_MODEL=$LLM_MODEL}" \
  --set-secrets "$(IFS=,; echo "${SECRET_FLAGS[*]}")"

URL="$(gc run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
if [ "$(gc run services describe "$SERVICE" --region "$REGION" --format='value(spec.template.spec.containers[0].env)' | grep -o "API_URL[^;]*" || true)" != *"$URL"* ]; then
  echo "== Pointing the ads' click tracking at $URL"
  gc run services update "$SERVICE" --region "$REGION" --update-env-vars "API_URL=$URL" >/dev/null
fi

echo "== Keeping only the 2 newest images (Artifact Registry free tier is 0.5 GB)"
cat > /tmp/simula-cleanup.json <<'JSON'
[{"name": "keep-newest", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
 {"name": "delete-older", "action": {"type": "Delete"}, "condition": {"tagState": "ANY"}}]
JSON
gc artifacts repositories set-cleanup-policies cloud-run-source-deploy --location "$REGION" \
  --policy /tmp/simula-cleanup.json --no-dry-run >/dev/null || echo "  (cleanup policy not set; set it in the console)"

echo
echo "Deployed: $URL"
echo "Seed data:  set -a; source .env.cloud; set +a; uv run python -m app.seed"
echo "Smoke test: uv run python scripts/smoke_test.py $URL --click-key \"\$CLICK_API_KEY\""
echo "Live demo:  $URL/demo"
