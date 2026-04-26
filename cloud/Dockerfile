FROM public.ecr.aws/lambda/nodejs:22

RUN dnf install -y python3 python3-pip tar gzip && dnf clean all

WORKDIR ${LAMBDA_TASK_ROOT}

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

RUN LD_LIBRARY_PATH=/usr/lib64:/lib64:/usr/lib:/lib \
    pip3 install --no-cache-dir boto3 requests pyyaml beautifulsoup4

COPY AGENTS.md CLAUDE.md GEMINI.md README.md WIKI.md ./
COPY bin ./bin
COPY cloud ./cloud
COPY config ./config
COPY hooks ./hooks
COPY templates ./templates
COPY tools ./tools

CMD ["cloud/node_lambda_wrapper.handler"]
