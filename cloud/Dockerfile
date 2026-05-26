FROM public.ecr.aws/lambda/nodejs:22

RUN dnf install -y python3 python3-pip tar gzip && dnf clean all
RUN npm install -g bun@1.3.14
RUN LD_LIBRARY_PATH=/usr/lib64:/lib64:/usr/lib:/lib \
    python3 -m pip install --no-cache-dir uv==0.9.18

WORKDIR ${LAMBDA_TASK_ROOT}

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV UV_NO_CACHE=1
ENV UV_LINK_MODE=copy

COPY package.json bun.lock ./
RUN bun install --production --frozen-lockfile

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY AGENTS.md CLAUDE.md GEMINI.md README.md WIKI.md ./
COPY bin ./bin
COPY cloud ./cloud
COPY config ./config
COPY hooks ./hooks
COPY templates ./templates
COPY tools ./tools

CMD ["cloud/node_lambda_wrapper.handler"]
