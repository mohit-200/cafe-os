#!/usr/bin/env bash
# CAFÉ OS: Phase 2 infrastructure bootstrap. Idempotent: safe to re-run; existing resources are skipped.
#
# Usage:
#   BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX ./infra/setup.sh   # billing account is required (kept out of the repo)
#   STAFF_PIN=482913 ./infra/setup.sh      # choose the staff PIN (otherwise a random 6-digit PIN is generated)
#   BUDGET_AMOUNT=1300INR ./infra/setup.sh # override if the billing account currency is not USD/INR
#
# Creates: APIs, 2 service accounts (runtime + build) with least-privilege roles, a Cloud SQL for PostgreSQL
# instance (db-f1-micro, always on, paid from hackathon credits) with database + app user, Artifact Registry repo
# with cleanup policy, a build-source bucket, 3 secrets, and a $15/month budget on GROSS cost (before free credits)
# with alerts at 50/90/100%, so credit burn stays visible (expected spend ~$10-12/month, mostly Cloud SQL).
# Never uses the default compute service account or Owner.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-builder-pop-up-515511}"
REGION="${REGION:-asia-south1}"
BILLING_ACCOUNT="${BILLING_ACCOUNT:?Set BILLING_ACCOUNT=<your billing account ID> (gcloud billing accounts list)}"
REPO="cafe-os"
RUN_SA_NAME="cafe-os-run"
BUILD_SA_NAME="cafe-os-build"
RUN_SA="${RUN_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA="${BUILD_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SOURCE_BUCKET="gs://${PROJECT_ID}-cafe-os-src"
SQL_INSTANCE="cafe-os-db"
DB_NAME="cafeos"
DB_USER="cafeos"
BUDGET_NAME="cafe-os-monthly-gross"
HERE="$(cd "$(dirname "$0")" && pwd)"
GCLOUD="${GCLOUD:-gcloud}"
command -v "$GCLOUD" >/dev/null || GCLOUD="$HOME/google-cloud-sdk/bin/gcloud"

# Print a one-line comment and the exact command, then run it.
run() {
  local comment="$1"; shift
  printf '\n\033[1;33m# %s\033[0m\n$ %s\n' "$comment" "$*"
  "$@"
}
# Same, but skipped when the existence check (first arg, a command string) succeeds.
ensure() {
  local check="$1" comment="$2"; shift 2
  if eval "$check" >/dev/null 2>&1; then
    printf '\n\033[1;32m# %s: already exists, skipping\033[0m\n' "$comment"
  else
    run "$comment" "$@"
  fi
}

echo "Project: $PROJECT_ID | Region: $REGION | Billing account: $BILLING_ACCOUNT"
ACCOUNT="$("$GCLOUD" config get-value account 2>/dev/null || true)"
[ -n "$ACCOUNT" ] || { echo "Not signed in. Run: gcloud auth login"; exit 1; }
echo "Signed in as: $ACCOUNT"

# ---------- project defaults (local config only) ----------
run "Set default project for this machine" "$GCLOUD" config set core/project "$PROJECT_ID" --quiet
run "Set default Cloud Run region for this machine" "$GCLOUD" config set run/region "$REGION" --quiet

# ---------- APIs ----------
run "Enable only the APIs CAFÉ OS uses (Run, Build, Artifact Registry, Cloud SQL, Secret Manager, Vertex AI, budgets, IAM, logging, monitoring)" \
  "$GCLOUD" services enable \
    run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    sqladmin.googleapis.com secretmanager.googleapis.com aiplatform.googleapis.com \
    billingbudgets.googleapis.com iam.googleapis.com cloudresourcemanager.googleapis.com \
    logging.googleapis.com monitoring.googleapis.com storage.googleapis.com \
    --project="$PROJECT_ID" --quiet

# ---------- service accounts ----------
ensure "$GCLOUD iam service-accounts describe $RUN_SA --project=$PROJECT_ID" \
  "Create the runtime service account used by the Cloud Run service" \
  "$GCLOUD" iam service-accounts create "$RUN_SA_NAME" --display-name="CAFÉ OS runtime" \
    --description="Cloud Run runtime identity for CAFÉ OS" --project="$PROJECT_ID" --quiet
ensure "$GCLOUD iam service-accounts describe $BUILD_SA --project=$PROJECT_ID" \
  "Create the build service account used by Cloud Build (instead of the default compute SA)" \
  "$GCLOUD" iam service-accounts create "$BUILD_SA_NAME" --display-name="CAFÉ OS build" \
    --description="Cloud Build identity for CAFÉ OS" --project="$PROJECT_ID" --quiet

# ---------- runtime IAM (least privilege) ----------
run "Runtime SA: connect to Cloud SQL through the built-in Cloud Run connector" \
  "$GCLOUD" projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUN_SA" \
    --role="roles/cloudsql.client" --condition=None --quiet --format=none
run "Runtime SA: call Gemini on Vertex AI" \
  "$GCLOUD" projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUN_SA" \
    --role="roles/aiplatform.user" --condition=None --quiet --format=none

# ---------- build IAM (least privilege) ----------
run "Build SA: write build logs" \
  "$GCLOUD" projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$BUILD_SA" \
    --role="roles/logging.logWriter" --condition=None --quiet --format=none
run "Build SA: deploy new Cloud Run revisions (used by CI/CD)" \
  "$GCLOUD" projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$BUILD_SA" \
    --role="roles/run.developer" --condition=None --quiet --format=none
run "Build SA: allowed to attach the runtime SA to the service it deploys (on that SA only)" \
  "$GCLOUD" iam service-accounts add-iam-policy-binding "$RUN_SA" --member="serviceAccount:$BUILD_SA" \
    --role="roles/iam.serviceAccountUser" --project="$PROJECT_ID" --quiet --format=none

# ---------- Cloud SQL (PostgreSQL) ----------
ensure "$GCLOUD sql instances describe $SQL_INSTANCE --project=$PROJECT_ID" \
  "Create Cloud SQL PostgreSQL 17 (Enterprise, db-f1-micro shared core, 10 GB SSD, zonal, daily backups, deletion protection). Takes ~5-10 min" \
  "$GCLOUD" sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_17 --edition=enterprise \
    --tier=db-f1-micro --region="$REGION" --availability-type=zonal --storage-type=SSD --storage-size=10 \
    --no-storage-auto-increase --deletion-protection --project="$PROJECT_ID" --quiet
ensure "$GCLOUD sql databases describe $DB_NAME --instance=$SQL_INSTANCE --project=$PROJECT_ID" \
  "Create the application database" \
  "$GCLOUD" sql databases create "$DB_NAME" --instance="$SQL_INSTANCE" --project="$PROJECT_ID" --quiet

# ---------- Artifact Registry ----------
ensure "$GCLOUD artifacts repositories describe $REPO --location=$REGION --project=$PROJECT_ID" \
  "Create the Docker repository for CAFÉ OS images" \
  "$GCLOUD" artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" \
    --description="CAFÉ OS container images" --project="$PROJECT_ID" --quiet
run "Cleanup policy: keep the latest 3 images, delete older than 7 days (stays inside the 0.5 GB free tier)" \
  "$GCLOUD" artifacts repositories set-cleanup-policies "$REPO" --location="$REGION" \
    --policy="$HERE/ar-cleanup-policy.json" --project="$PROJECT_ID" --quiet --format=none
run "Build SA: push images to this repository only" \
  "$GCLOUD" artifacts repositories add-iam-policy-binding "$REPO" --location="$REGION" \
    --member="serviceAccount:$BUILD_SA" --role="roles/artifactregistry.writer" --project="$PROJECT_ID" --quiet --format=none
run "Runtime SA: pull images from this repository only" \
  "$GCLOUD" artifacts repositories add-iam-policy-binding "$REPO" --location="$REGION" \
    --member="serviceAccount:$RUN_SA" --role="roles/artifactregistry.reader" --project="$PROJECT_ID" --quiet --format=none

# ---------- build source bucket ----------
ensure "$GCLOUD storage buckets describe $SOURCE_BUCKET --project=$PROJECT_ID" \
  "Create a private regional bucket for build sources (objects auto-delete after 7 days)" \
  "$GCLOUD" storage buckets create "$SOURCE_BUCKET" --location="$REGION" --uniform-bucket-level-access \
    --public-access-prevention --lifecycle-file="$HERE/source-bucket-lifecycle.json" --project="$PROJECT_ID" --quiet
run "Build SA: read uploaded build sources from this bucket only" \
  "$GCLOUD" storage buckets add-iam-policy-binding "$SOURCE_BUCKET" --member="serviceAccount:$BUILD_SA" \
    --role="roles/storage.objectViewer" --project="$PROJECT_ID" --quiet --format=none

# ---------- secrets ----------
gen_key() { python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")'; }
gen_pin() { python3 -c 'import secrets; print(f"{secrets.randbelow(900000)+100000}", end="")'; }
NEW_PIN=""
NEW_DB_PASSWORD=""
for SECRET in cafe-os-secret-key cafe-os-staff-pin cafe-os-db-password; do
  if "$GCLOUD" secrets describe "$SECRET" --project="$PROJECT_ID" >/dev/null 2>&1; then
    printf '\n\033[1;32m# Secret %s: already exists, keeping current value\033[0m\n' "$SECRET"
  else
    run "Create secret $SECRET (replicated only in $REGION)" \
      "$GCLOUD" secrets create "$SECRET" --replication-policy=user-managed --locations="$REGION" \
        --project="$PROJECT_ID" --quiet
    case "$SECRET" in
      cafe-os-secret-key) VALUE="$(gen_key)" ;;
      cafe-os-staff-pin) VALUE="${STAFF_PIN:-$(gen_pin)}"; NEW_PIN="$VALUE" ;;
      cafe-os-db-password) VALUE="$(gen_key)"; NEW_DB_PASSWORD="1" ;;
    esac
    printf '\n\033[1;33m# Add first version of %s (value piped via stdin, never printed or written to disk)\033[0m\n$ gcloud secrets versions add %s --data-file=-\n' "$SECRET" "$SECRET"
    printf '%s' "$VALUE" | "$GCLOUD" secrets versions add "$SECRET" --data-file=- --project="$PROJECT_ID" --quiet
    unset VALUE
  fi
  run "Runtime SA: read secret $SECRET (this secret only)" \
    "$GCLOUD" secrets add-iam-policy-binding "$SECRET" --member="serviceAccount:$RUN_SA" \
      --role="roles/secretmanager.secretAccessor" --project="$PROJECT_ID" --quiet --format=none
done

# ---------- Cloud SQL application user (password comes from Secret Manager, never echoed) ----------
DB_PASSWORD_VALUE="$("$GCLOUD" secrets versions access latest --secret=cafe-os-db-password --project="$PROJECT_ID" --quiet)"
if "$GCLOUD" sql users list --instance="$SQL_INSTANCE" --filter="name=$DB_USER" --format='value(name)' --limit=1 --project="$PROJECT_ID" | grep -q .; then
  if [ -n "$NEW_DB_PASSWORD" ]; then
    printf '\n\033[1;33m# DB user %s exists but the password secret is new: syncing password\033[0m\n$ gcloud sql users set-password %s --instance=%s --password=***\n' "$DB_USER" "$DB_USER" "$SQL_INSTANCE"
    "$GCLOUD" sql users set-password "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASSWORD_VALUE" --project="$PROJECT_ID" --quiet
  else
    printf '\n\033[1;32m# DB user %s: already exists, skipping\033[0m\n' "$DB_USER"
  fi
else
  printf '\n\033[1;33m# Create the application DB user (built-in auth, password from Secret Manager)\033[0m\n$ gcloud sql users create %s --instance=%s --password=***\n' "$DB_USER" "$SQL_INSTANCE"
  "$GCLOUD" sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASSWORD_VALUE" --project="$PROJECT_ID" --quiet
fi
unset DB_PASSWORD_VALUE

# ---------- budget ----------
CURRENCY="$("$GCLOUD" billing accounts describe "$BILLING_ACCOUNT" --format='value(currencyCode)' 2>/dev/null || true)"
if [ -z "${BUDGET_AMOUNT:-}" ]; then
  case "$CURRENCY" in
    USD) BUDGET_AMOUNT="15USD" ;;
    INR) BUDGET_AMOUNT="1300INR" ;;  # ≈ $15
    *) echo "Billing currency is '$CURRENCY'. Re-run with BUDGET_AMOUNT=<≈\$15 in that currency>, e.g. BUDGET_AMOUNT=14EUR"; exit 1 ;;
  esac
fi
ensure "$GCLOUD billing budgets list --billing-account=$BILLING_ACCOUNT --filter=displayName=$BUDGET_NAME --format=value(name) --limit=1 | grep -q ." \
  "Budget $BUDGET_AMOUNT/month on GROSS cost (before free credits, so it tracks credit burn), alerts at 50%, 90%, 100% (alerts only, does not cap spend)" \
  "$GCLOUD" billing budgets create --billing-account="$BILLING_ACCOUNT" --display-name="$BUDGET_NAME" \
    --budget-amount="$BUDGET_AMOUNT" --calendar-period=month --filter-projects="projects/$PROJECT_ID" \
    --credit-types-treatment=exclude-all-credits \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 --quiet

echo
echo "================ Setup complete ================"
echo "Runtime SA : $RUN_SA"
echo "Build SA   : $BUILD_SA"
echo "Images     : $REGION-docker.pkg.dev/$PROJECT_ID/$REPO"
echo "Cloud SQL  : $PROJECT_ID:$REGION:$SQL_INSTANCE (db $DB_NAME, user $DB_USER)"
echo "Budget     : $BUDGET_AMOUNT / month with alerts at 50/90/100%"
if [ -n "$NEW_PIN" ]; then
  echo
  echo "STAFF PIN (shown once, also stored in Secret Manager as cafe-os-staff-pin): $NEW_PIN"
fi
