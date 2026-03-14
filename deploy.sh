#!/bin/bash
# SocratiDesk — Automated Cloud Deployment Script
# This script deploys the live-server to Google Cloud Run
# and sets up Firestore + GCS resources.
#
# Usage: ./deploy.sh <PROJECT_ID> <GEMINI_API_KEY> [REGION]
#
# For Gemini Live Agent Challenge — proves automated Cloud deployment (bonus points)

set -euo pipefail

PROJECT_ID="${1:?Usage: ./deploy.sh <PROJECT_ID> <GEMINI_API_KEY> [REGION]}"
GEMINI_API_KEY="${2:?Usage: ./deploy.sh <PROJECT_ID> <GEMINI_API_KEY> [REGION]}"
REGION="${3:-us-central1}"

SERVICE_NAME="socratidesk-live-server"
GCS_BUCKET="${PROJECT_ID}-socratidesk-textbooks"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "═══════════════════════════════════════════"
echo "  SocratiDesk — Cloud Deployment"
echo "  Project:  ${PROJECT_ID}"
echo "  Region:   ${REGION}"
echo "  Bucket:   ${GCS_BUCKET}"
echo "═══════════════════════════════════════════"

# 1. Enable required APIs
echo "[1/6] Enabling APIs..."
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  cloudbuild.googleapis.com \
  --project="${PROJECT_ID}" --quiet

# 2. Create GCS bucket for textbook uploads
echo "[2/6] Creating GCS bucket..."
gsutil mb -p "${PROJECT_ID}" -l "${REGION}" "gs://${GCS_BUCKET}" 2>/dev/null || echo "  Bucket already exists"

# 3. Create Firestore database (if not exists)
echo "[3/6] Setting up Firestore..."
gcloud firestore databases create \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --type=firestore-native 2>/dev/null || echo "  Firestore already exists"

# 4. Build container
echo "[4/6] Building container..."
cd live-server
gcloud builds submit \
  --tag "${IMAGE}" \
  --project="${PROJECT_ID}" \
  --quiet

# 5. Deploy to Cloud Run
echo "[5/6] Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" \
  --platform managed \
  --region "${REGION}" \
  --project="${PROJECT_ID}" \
  --allow-unauthenticated \
  --timeout 3600 \
  --memory 1Gi \
  --cpu 1 \
  --max-instances 5 \
  --set-env-vars "GEMINI_API_KEY=${GEMINI_API_KEY},GCS_BUCKET=${GCS_BUCKET},VOICE_NAME=Kore" \
  --quiet

# 6. Get service URL
echo "[6/6] Getting service URL..."
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(status.url)")

echo ""
echo "═══════════════════════════════════════════"
echo "  Deployment complete!"
echo ""
echo "  HTTP:      ${SERVICE_URL}"
echo "  WebSocket: ${SERVICE_URL/https/wss}/live"
echo "  Upload:    curl -F 'file=@textbook.pdf' ${SERVICE_URL}/upload-textbook"
echo ""
echo "  Update pi-device/.env:"
echo "    SOCRATIDESK_WS=${SERVICE_URL/https/wss}/live"
echo "    SOCRATIDESK_HTTP=${SERVICE_URL}"
echo "═══════════════════════════════════════════"