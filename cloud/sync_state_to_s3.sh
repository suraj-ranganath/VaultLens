#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

STACK_NAME="${STACK_NAME:-my-vault-telegram}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"

BUCKET="$(
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='VaultStateBucketName'].OutputValue" \
    --output text
)"

if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
  echo "Could not resolve VaultStateBucketName for stack $STACK_NAME in $AWS_REGION." >&2
  exit 1
fi

ARCHIVE="$(mktemp -t my-vault-state.XXXXXX.tar.gz)"
cleanup() {
  rm -f "$ARCHIVE"
}
trap cleanup EXIT

INCLUDES=()
for path in .vault dashboards imports items outputs projects raw topics hot.md index.md log.md; do
  if [ -e "$path" ]; then
    INCLUDES+=("$path")
  fi
done

if [ "${#INCLUDES[@]}" -eq 0 ]; then
  echo "No ignored vault state paths found to upload." >&2
  exit 1
fi

tar -czf "$ARCHIVE" "${INCLUDES[@]}"
aws s3 cp "$ARCHIVE" "s3://${BUCKET}/state/vault-state.tar.gz"

printf "Synced local ignored vault state bundle to s3://%s/state/vault-state.tar.gz\n" "$BUCKET"
