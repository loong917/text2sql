FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unixodbc-dev \
    gnupg2 \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock requirements.lock ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --require-hashes -r requirements.lock \
    && pip install --no-deps .

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p logs vanna_knowledge_db vanna_agent_memory \
    && chown -R appuser:appuser /app

FROM base AS trainer

COPY --chown=appuser:appuser evaluation ./evaluation
COPY --chown=appuser:appuser knowledge ./knowledge

USER appuser
ENTRYPOINT ["text2sql-train"]

FROM base AS server

COPY --chown=appuser:appuser knowledge ./knowledge

USER appuser

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fs http://localhost:8090/readyz || exit 1

ENTRYPOINT ["text2sql-server"]
