FROM python:3.12-slim

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

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . && pip install --no-cache-dir pyodbc

COPY . .

RUN mkdir -p logs vanna_knowledge_db vanna_memory_db

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD curl -s http://localhost:8090/health || exit 1

ENTRYPOINT ["text2sql-server"]
