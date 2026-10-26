FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV HOST=0.0.0.0 \
    PORT=8080 \
    PATH="/app/.venv/bin:$PATH"

COPY requirements.txt ./
RUN uv venv --seed /app/.venv \
    && pip install -r requirements.txt

RUN npm install -g @modelcontextprotocol/server-postgres@0.6.2

COPY host.py catalog.json ./

EXPOSE 8080

CMD ["python", "host.py"]
