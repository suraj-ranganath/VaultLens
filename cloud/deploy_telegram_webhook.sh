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

STACK_NAME="${STACK_NAME:-vault-lens-telegram}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
VAULT_AGENT_MODEL="${VAULT_AGENT_MODEL:-gpt-5.4}"
VAULT_AGENT_REASONING_EFFORT="${VAULT_AGENT_REASONING_EFFORT:-medium}"
TELEGRAM_ALLOWED_CHAT_IDS="${TELEGRAM_ALLOWED_CHAT_IDS:-}"
HEARTBEAT_ENABLED="${HEARTBEAT_ENABLED:-false}"
TELEGRAM_HEARTBEAT_CHAT_ID="${TELEGRAM_HEARTBEAT_CHAT_ID:-}"
HEARTBEAT_SCHEDULE="${HEARTBEAT_SCHEDULE:-cron(0 8 * * ? *)}"
HEARTBEAT_SCHEDULE_TIMEZONE="${HEARTBEAT_SCHEDULE_TIMEZONE:-${HEARTBEAT_TIMEZONE:-America/Los_Angeles}}"
VAULT_CALENDAR_ID="${VAULT_CALENDAR_ID:-${VAULT_BRIEF_CALENDAR_ID:-primary}}"
AWS_ACCOUNT_ID="$(
  aws sts get-caller-identity \
    --query Account \
    --output text \
    --region "$AWS_REGION" \
    --cli-connect-timeout 5 \
    --cli-read-timeout 20 \
    --no-cli-pager
)"
ECR_REPO="${STACK_NAME}-lambda"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

if [ -z "${TELEGRAM_WEBHOOK_SECRET:-}" ]; then
  TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 32)"
  {
    printf "\n"
    printf "TELEGRAM_WEBHOOK_SECRET=%s\n" "$TELEGRAM_WEBHOOK_SECRET"
  } >> .env.local
  printf "Generated TELEGRAM_WEBHOOK_SECRET and appended it to .env.local\n"
fi

export OPENAI_API_KEY
export TELEGRAM_BOT_TOKEN
export TELEGRAM_WEBHOOK_SECRET
export TELEGRAM_ALLOWED_CHAT_IDS
export VAULT_AGENT_MODEL
export VAULT_AGENT_REASONING_EFFORT
export HEARTBEAT_ENABLED
export TELEGRAM_HEARTBEAT_CHAT_ID
export HEARTBEAT_SCHEDULE
export HEARTBEAT_SCHEDULE_TIMEZONE
export VAULT_CALENDAR_ID
export STACK_NAME
export AWS_REGION
export ECR_URI

sam build --template-file cloud/template.yaml

ensure_ecr_repo() {
  local repo_name="$1"
  aws ecr describe-repositories \
    --repository-names "$repo_name" \
    --region "$AWS_REGION" \
    --cli-connect-timeout 5 \
    --cli-read-timeout 20 \
    --no-cli-pager >/dev/null 2>&1 && return 0

  aws ecr create-repository \
    --repository-name "$repo_name" \
    --region "$AWS_REGION" \
    --image-scanning-configuration scanOnPush=false \
    --cli-connect-timeout 5 \
    --cli-read-timeout 20 \
    --no-cli-pager >/dev/null
}

configure_ecr_lifecycle() {
  local repo_name="$1"
  aws ecr put-lifecycle-policy \
    --repository-name "$repo_name" \
    --region "$AWS_REGION" \
    --lifecycle-policy-text '{
      "rules": [
        {
          "rulePriority": 1,
          "description": "Expire untagged deployment layers quickly",
          "selection": {
            "tagStatus": "untagged",
            "countType": "sinceImagePushed",
            "countUnit": "days",
            "countNumber": 1
          },
          "action": { "type": "expire" }
        },
        {
          "rulePriority": 2,
          "description": "Keep only the latest deployment images",
          "selection": {
            "tagStatus": "any",
            "countType": "imageCountMoreThan",
            "countNumber": 8
          },
          "action": { "type": "expire" }
        }
      ]
    }' \
    --cli-connect-timeout 5 \
    --cli-read-timeout 20 \
    --no-cli-pager >/dev/null
}

ensure_ecr_repo "$ECR_REPO"
configure_ecr_lifecycle "$ECR_REPO"
aws ecr get-login-password \
  --region "$AWS_REGION" \
  --cli-connect-timeout 5 \
  --cli-read-timeout 20 \
  --no-cli-pager | docker login \
    --username AWS \
    --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null

SAM_CONFIG_FILE="$(mktemp -t vault-lens-sam-config).toml"
cleanup() {
  rm -f "$SAM_CONFIG_FILE"
}
trap cleanup EXIT

python3 - "$SAM_CONFIG_FILE" <<'PY'
import json
import os
import sys

def toml_string(value):
    return json.dumps(str(value))

overrides = {
    "OpenAIApiKey": os.environ["OPENAI_API_KEY"],
    "TelegramBotToken": os.environ["TELEGRAM_BOT_TOKEN"],
    "TelegramWebhookSecret": os.environ["TELEGRAM_WEBHOOK_SECRET"],
    "VaultAgentModel": os.environ["VAULT_AGENT_MODEL"],
    "VaultAgentReasoningEffort": os.environ["VAULT_AGENT_REASONING_EFFORT"],
    "HeartbeatEnabled": os.environ["HEARTBEAT_ENABLED"],
    "HeartbeatSchedule": os.environ["HEARTBEAT_SCHEDULE"],
    "HeartbeatScheduleTimezone": os.environ["HEARTBEAT_SCHEDULE_TIMEZONE"],
    "VaultCalendarId": os.environ["VAULT_CALENDAR_ID"],
}
allowed = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
if allowed:
    overrides["TelegramAllowedChatIds"] = allowed
credentials_json = os.environ.get("GOOGLE_WORKSPACE_CLI_CREDENTIALS_JSON", "").strip()
if credentials_json:
    overrides["GoogleWorkspaceCliCredentialsJson"] = credentials_json
heartbeat_chat_id = os.environ.get("TELEGRAM_HEARTBEAT_CHAT_ID", "").strip()
if heartbeat_chat_id:
    overrides["TelegramHeartbeatChatId"] = heartbeat_chat_id

parameter_overrides = " ".join(f"{key}={value!r}" for key, value in overrides.items())
ecr_uri = os.environ["ECR_URI"]

content = f"""version = 0.1
[default.deploy.parameters]
stack_name = {toml_string(os.environ["STACK_NAME"])}
region = {toml_string(os.environ["AWS_REGION"])}
capabilities = "CAPABILITY_IAM"
resolve_s3 = true
confirm_changeset = false
image_repositories = [
  "ReceiverFunction={ecr_uri}",
  "ProcessorFunction={ecr_uri}",
]
parameter_overrides = {toml_string(parameter_overrides)}
"""

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write(content)
PY

sam deploy --config-file "$SAM_CONFIG_FILE"

WEBHOOK_URL="$(
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='ReceiverFunctionUrl'].OutputValue" \
    --cli-connect-timeout 5 \
    --cli-read-timeout 20 \
    --output text
)"

if [ -z "$WEBHOOK_URL" ] || [ "$WEBHOOK_URL" = "None" ]; then
  echo "Could not resolve ReceiverFunctionUrl from CloudFormation output." >&2
  exit 1
fi

export WEBHOOK_URL
python3 - <<'PY'
import json
import os
import urllib.parse
import urllib.request

payload = urllib.parse.urlencode(
    {
        "url": os.environ["WEBHOOK_URL"],
        "secret_token": os.environ["TELEGRAM_WEBHOOK_SECRET"],
        "allowed_updates": json.dumps(
            ["message", "edited_message", "channel_post", "edited_channel_post", "callback_query"]
        ),
    }
).encode("utf-8")
request = urllib.request.Request(
    f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/setWebhook",
    data=payload,
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    body = json.loads(response.read().decode("utf-8"))
if not body.get("ok"):
    raise SystemExit(f"Telegram setWebhook failed: {body}")
PY

printf "\nTelegram webhook installed: %s\n" "$WEBHOOK_URL"
printf "State bucket: "
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='VaultStateBucketName'].OutputValue" \
  --cli-connect-timeout 5 \
  --cli-read-timeout 20 \
  --output text
printf "\n"
