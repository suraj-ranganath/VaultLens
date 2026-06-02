#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ -f .env.local ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.local
  set +a
fi

STACK_NAME="${STACK_NAME:-vault-lens-telegram}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
VAULT_CODEX_AUTH_S3_KEY="${VAULT_CODEX_AUTH_S3_KEY:-codex-auth/auth.json}"
VAULT_CODEX_AUTH_S3_KEY="${VAULT_CODEX_AUTH_S3_KEY#/}"
DEFAULT_CODEX_HOME="${CODEX_HOME:-${HOME:-}/.codex}"
CODEX_AUTH_JSON="${CODEX_AUTH_JSON:-${DEFAULT_CODEX_HOME}/auth.json}"

if [ ! -f "$CODEX_AUTH_JSON" ]; then
  echo "Missing local Codex auth file: $CODEX_AUTH_JSON" >&2
  echo "Run 'codex login --device-auth' locally, then retry." >&2
  exit 1
fi

python3 - "$CODEX_AUTH_JSON" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("Codex auth file must contain a JSON object")
if "tokens" not in payload:
    raise SystemExit("Codex auth file is missing the expected 'tokens' field")
PY

BUCKET="${VAULT_STATE_BUCKET:-}"
if [ -z "$BUCKET" ]; then
  BUCKET="$(
    aws cloudformation describe-stacks \
      --stack-name "$STACK_NAME" \
      --region "$AWS_REGION" \
      --query "Stacks[0].Outputs[?OutputKey=='VaultStateBucketName'].OutputValue" \
      --cli-connect-timeout 5 \
      --cli-read-timeout 20 \
      --output text
  )"
fi

if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
  echo "Could not resolve VaultStateBucketName for stack $STACK_NAME in $AWS_REGION." >&2
  exit 1
fi

aws s3 cp "$CODEX_AUTH_JSON" "s3://${BUCKET}/${VAULT_CODEX_AUTH_S3_KEY}" \
  --region "$AWS_REGION" \
  --sse AES256 \
  --content-type application/json \
  --no-cli-pager >/dev/null

printf "Synced Codex ChatGPT auth to s3://%s/%s\n" "$BUCKET" "$VAULT_CODEX_AUTH_S3_KEY"
