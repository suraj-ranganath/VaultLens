FROM public.ecr.aws/lambda/python:3.12

RUN dnf install -y nodejs npm && dnf clean all

WORKDIR ${LAMBDA_TASK_ROOT}

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

RUN pip install --no-cache-dir boto3 requests pyyaml beautifulsoup4

COPY AGENTS.md CLAUDE.md GEMINI.md README.md WIKI.md ./
COPY bin ./bin
COPY cloud ./cloud
COPY config ./config
COPY hooks ./hooks
COPY templates ./templates
COPY tools ./tools

CMD ["cloud.telegram_webhook_lambda.handler"]
