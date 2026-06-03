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
FUNCTION_NAME="${VAULT_BROWSER_WORKER_FUNCTION_NAME:-}"

if [ -z "$FUNCTION_NAME" ]; then
  FUNCTION_NAME="$(
    aws cloudformation describe-stacks \
      --stack-name "$STACK_NAME" \
      --region "$AWS_REGION" \
      --query "Stacks[0].Outputs[?OutputKey=='BrowserWorkerFunctionName'].OutputValue" \
      --cli-connect-timeout 5 \
      --cli-read-timeout 20 \
      --output text
  )"
fi

if [ -z "$FUNCTION_NAME" ] || [ "$FUNCTION_NAME" = "None" ]; then
  echo "Could not resolve BrowserWorkerFunctionName from CloudFormation output." >&2
  exit 1
fi

PAYLOAD="$(
  python3 - <<'PY'
import json
import os

payload = {
    "browserEnrichment": True,
    "source": "manual-cli",
    "limit": int(os.environ.get("VAULT_BROWSER_ENRICH_LIMIT", "24")),
    "concurrency": int(os.environ.get("VAULT_BROWSER_ENRICH_CONCURRENCY", "2")),
    "lookbackDays": int(os.environ.get("VAULT_BROWSER_ENRICH_LOOKBACK_DAYS", "30")),
}
print(json.dumps(payload))
PY
)"

OUTPUT_FILE="$(mktemp -t vault-lens-browser-worker).json"
INVOCATION_TYPE="${VAULT_BROWSER_INVOKE_TYPE:-RequestResponse}"

aws lambda invoke \
  --function-name "$FUNCTION_NAME" \
  --invocation-type "$INVOCATION_TYPE" \
  --payload "$PAYLOAD" \
  --cli-binary-format raw-in-base64-out \
  --region "$AWS_REGION" \
  --cli-connect-timeout 5 \
  --cli-read-timeout 900 \
  --no-cli-pager \
  "$OUTPUT_FILE" >/dev/null

cat "$OUTPUT_FILE"
printf "\n"
rm -f "$OUTPUT_FILE"
