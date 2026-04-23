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

for directory in dashboards imports items outputs projects raw topics; do
  if [ -d "$directory" ]; then
    aws s3 sync "$directory" "s3://${BUCKET}/state/${directory}" --delete
  fi
done

for file in hot.md index.md log.md; do
  if [ -f "$file" ]; then
    aws s3 cp "$file" "s3://${BUCKET}/state/${file}"
  fi
done

printf "Synced local ignored vault state to s3://%s/state/\n" "$BUCKET"
