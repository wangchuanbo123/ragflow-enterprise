ARG NODE_IMAGE=node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c
ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS opencode
COPY --from=web /usr/local/bin/node /usr/local/bin/node
COPY --from=web /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update \
    && apt-get -o Acquire::Retries=3 install -y --no-install-recommends git ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY scripts/prepare_opencode.py scripts/prepare_opencode.py
COPY integrations/opencode/source.json integrations/opencode/source.json
RUN python -m scripts.prepare_opencode --install-runtime

FROM ${PYTHON_IMAGE} AS application
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 ANONYMIZED_TELEMETRY=False
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    ca-certificates git libmagic1 poppler-utils tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY constraints-server.txt ./
COPY requirements*.txt ./
ARG INSTALL_LEGACY=false
ARG INSTALL_EVALUATION=false
ARG INSTALL_TESTS=false
ARG INSTALL_LOCAL_MODELS=false
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$INSTALL_LOCAL_MODELS" = "true" ]; then pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu; fi \
    && set -- -r requirements-runtime.txt \
    && if [ "$INSTALL_LOCAL_MODELS" = "true" ]; then set -- "$@" -r requirements-local.txt; fi \
    && if [ "$INSTALL_LEGACY" = "true" ]; then set -- "$@" -r requirements-legacy.txt; fi \
    && if [ "$INSTALL_EVALUATION" = "true" ]; then set -- "$@" -r requirements-eval.txt; fi \
    && if [ "$INSTALL_TESTS" = "true" ]; then set -- "$@" -r requirements-test.txt; fi \
    && PIP_NO_CACHE_DIR=false pip install -c constraints-server.txt "$@" \
    && pip check
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
COPY . .
COPY --from=web /web/dist /app/web/dist
COPY --from=opencode /app/integrations/opencode/runtime /app/integrations/opencode/runtime
RUN groupadd --gid 10001 rag && useradd --uid 10001 --gid rag --create-home rag \
    && mkdir -p /app/data/docs /app/data/agents /app/data/search-cache && chown -R rag:rag /app/data /home/rag
USER rag
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
