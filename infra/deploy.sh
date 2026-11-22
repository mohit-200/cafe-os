#!/usr/bin/env bash
# CAFÉ OS: Phase 5 deploy. Build the image with Cloud Build (as cafe-os-build), then deploy to Cloud Run
# (as cafe-os-run) connected to Cloud SQL, with secrets from Secret Manager and Gemini on Vertex AI.
#
# Usage: ./infra/deploy.sh            # DEMO_START=14:30 pins the café clock for demos
#        DEMO_START= ./infra/deploy.sh # real time (for actual café use)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT_ID="${PROJECT_ID:-builder-pop-up-515511}"
REGION="${REGION:-asia-south1}"
SERVICE="cafe-os"
TAG="${TAG:-v$(date +%Y%m%d-%H%M%S)}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/cafe-os/cafe-os:$TAG"
SQL_CONN="$PROJECT_ID:$REGION:cafe-os-db"
DEMO_START="${DEMO_START-14:30}"
GCLOUD="${GCLOUD:-gcloud}"
command -v "$GCLOUD" >/dev/null || GCLOUD="$HOME/google-cloud-sdk/bin/gcloud"

run() { local c="$1"; shift; printf '\n\033[1;33m# %s\033[0m\n$ %s\n' "$c" "$*"; "$@"; }

run "Build and push the container image ($TAG) with Cloud Build, as the dedicated build service account" \
  "$GCLOUD" builds submit . --config=cloudbuild.yaml --substitutions="_TAG=$TAG" --region="$REGION" \
    --service-account="projects/$PROJECT_ID/serviceAccounts/cafe-os-build@$PROJECT_ID.iam.gserviceaccount.com" \
    --gcs-source-staging-dir="gs://$PROJECT_ID-cafe-os-src/source" --project="$PROJECT_ID" --quiet

run "Deploy to Cloud Run: public, scales 0 to 1 instance, Cloud SQL socket, secrets from Secret Manager, Gemini on" \
  "$GCLOUD" run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" \
    --service-account="cafe-os-run@$PROJECT_ID.iam.gserviceaccount.com" \
    --add-cloudsql-instances="$SQL_CONN" \
    --set-secrets="SECRET_KEY=cafe-os-secret-key:latest,STAFF_PIN=cafe-os-staff-pin:latest,DB_PASSWORD=cafe-os-db-password:latest" \
    --set-env-vars="APP_ENV=prod,CAFE_ID=bean-byte,STORAGE=postgres,DB_HOST=/cloudsql/$SQL_CONN,DB_NAME=cafeos,DB_USER=cafeos,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GEMINI_BACKEND=vertex,GEMINI_MODEL=gemini-3.1-flash-lite,GEMINI_LOCATION=global,GEMINI_DAILY_CAP=300,DEMO_START=$DEMO_START,DEMO_CONTROLS=true" \
    --min-instances=0 --max-instances=1 --concurrency=80 --cpu=1 --memory=512Mi --timeout=60 \
    --allow-unauthenticated --project="$PROJECT_ID" --quiet

URL="$("$GCLOUD" run services describe "$SERVICE" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')"
printf '\n# Live check\n'
curl -fsS "$URL/health" && echo
echo "Customer app : $URL/"
echo "Staff board  : $URL/staff"
