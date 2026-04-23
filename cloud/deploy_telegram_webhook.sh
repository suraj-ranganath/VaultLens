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

: "${OPENAI_API_KEY:?Missing OPENAI_API_KEY. Put it in .env.local or export it.}"
: "${TELEGRAM_BOT_TOKEN:?Missing TELEGRAM_BOT_TOKEN. Put it in .env.local or export it.}"

STACK_NAME="${STACK_NAME:-my-vault-telegram}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
VAULT_AGENT_MODEL="${VAULT_AGENT_MODEL:-gpt-5.4}"
VAULT_AGENT_REASONING_EFFORT="${VAULT_AGENT_REASONING_EFFORT:-medium}"
TELEGRAM_ALLOWED_CHAT_IDS="${TELEGRAM_ALLOWED_CHAT_IDS:-}"

if [ -z "${TELEGRAM_WEBHOOK_SECRET:-}" ]; then
  TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 32)"
  {
    printf "\n"
    printf "TELEGRAM_WEBHOOK_SECRET=%s\n" "$TELEGRAM_WEBHOOK_SECRET"
  } >> .env.local
  printf "Generated TELEGRAM_WEBHOOK_SECRET and appended it to .env.local\n"
fi

sam build --template-file cloud/template.yaml

sam deploy \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_IAM \
  --resolve-s3 \
  --resolve-image-repos \
  --no-confirm-changeset \
  --parameter-overrides \
    OpenAIApiKey="$OPENAI_API_KEY" \
    TelegramBotToken="$TELEGRAM_BOT_TOKEN" \
    TelegramWebhookSecret="$TELEGRAM_WEBHOOK_SECRET" \
    TelegramAllowedChatIds="$TELEGRAM_ALLOWED_CHAT_IDS" \
    VaultAgentModel="$VAULT_AGENT_MODEL" \
    VaultAgentReasoningEffort="$VAULT_AGENT_REASONING_EFFORT"

WEBHOOK_URL="$(
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='ReceiverFunctionUrl'].OutputValue" \
    --output text
)"

if [ -z "$WEBHOOK_URL" ] || [ "$WEBHOOK_URL" = "None" ]; then
  echo "Could not resolve ReceiverFunctionUrl from CloudFormation output." >&2
  exit 1
fi

curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=${WEBHOOK_URL}" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  -d 'allowed_updates=["message","edited_message","channel_post","edited_channel_post"]'

printf "\nTelegram webhook installed: %s\n" "$WEBHOOK_URL"
printf "State bucket: "
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='VaultStateBucketName'].OutputValue" \
  --output text
printf "\n"
