FROM public.ecr.aws/lambda/nodejs:22

RUN dnf install -y \
      alsa-lib \
      at-spi2-atk \
      atk \
      cups-libs \
      gtk3 \
      libXcomposite \
      libXdamage \
      libXext \
      libXi \
      libXrandr \
      libXScrnSaver \
      libXtst \
      libdrm \
      libxkbcommon \
      mesa-libgbm \
      nss \
      pango \
      python3 \
      python3-pip \
      tar \
      gzip \
      xorg-x11-fonts-Type1 \
      xorg-x11-fonts-misc \
    && dnf clean all

RUN npm install -g bun@1.3.14 \
    && tmp_dir="$(mktemp -d)" \
    && npm pack @openai/codex@0.132.0-linux-arm64 --pack-destination "$tmp_dir" \
    && mkdir -p /opt/openai-codex \
    && tar -xzf "$tmp_dir"/openai-codex-0.132.0-linux-arm64.tgz -C /opt/openai-codex --strip-components=1 \
    && ln -sf /opt/openai-codex/vendor/aarch64-unknown-linux-musl/codex/codex /usr/local/bin/codex \
    && ln -sf /opt/openai-codex/vendor/aarch64-unknown-linux-musl/path/rg /usr/local/bin/rg \
    && rm -rf "$tmp_dir"
RUN LD_LIBRARY_PATH=/usr/lib64:/lib64:/usr/lib:/lib \
    python3 -m pip install --no-cache-dir uv==0.9.18

WORKDIR ${LAMBDA_TASK_ROOT}

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
ENV UV_NO_CACHE=1
ENV UV_LINK_MODE=copy
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
ENV VAULT_CODEX_BIN=/usr/local/bin/codex

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile \
    && bunx playwright install chromium \
    && chmod -R a+rX /opt/ms-playwright node_modules

COPY pyproject.toml uv.lock ./
RUN mkdir -p /opt/uv-python \
    && uv venv .venv \
    && uv pip install --python .venv/bin/python \
      beautifulsoup4==4.14.3 \
      boto3==1.43.15 \
      PyYAML==6.0.3 \
      requests==2.34.2 \
      pydantic==2.13.4 \
    && uv pip install --python .venv/bin/python --no-deps openai-codex==0.1.0b2 \
    && chmod -R a+rX /opt/uv-python .venv

COPY AGENTS.md CLAUDE.md GEMINI.md README.md WIKI.md ./
COPY bin ./bin
COPY cloud ./cloud
COPY config ./config
COPY hooks ./hooks
COPY templates ./templates
COPY tools ./tools

CMD ["cloud/node_lambda_wrapper.handler"]
