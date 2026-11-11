FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VALIDATOR_PORT=4567 \
    VALIDATOR_HOST=0.0.0.0 \
    PACKAGE_CACHE_DIR=/var/cache/fhir-packages \
    PATH="/app/.venv/bin:$PATH"

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Dependencies first (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# R4 core definitions, preloaded IGs, application code
COPY data/profiles-resources.json data/profiles-types.json data/profiles-others.json data/profiles-core-extra.json \
     data/extension-definitions.json data/valuesets.json data/v2-tables.json data/v3-codesystems.json ./data/
COPY igs/ ./igs/
COPY app/ ./app/

# Downloaded IG packages are cached here (mount a volume to keep them across restarts)
RUN mkdir -p /var/cache/fhir-packages
VOLUME /var/cache/fhir-packages

EXPOSE 4567

# /resources only answers 200 once the validator has finished loading (503 before that)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/resources' % os.environ.get('VALIDATOR_PORT','4567'), timeout=4).status == 200 else 1)"

# One process: the loaded definitions live in RAM, so extra workers multiply memory use
CMD ["python", "-m", "app.main"]
