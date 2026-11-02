# dash

Пайплайн вопросов по Stimulsoft `.mrt` + **Postgres MCP Pro**, как в [статье на Хабре](https://habr.com/ru/articles/947818/):

1. `extract_mrt` → `catalog.json`
2. каталог → системный промпт
3. локальная LLM → SQL
4. `crystaldba/postgres-mcp` (`execute_sql`, `--access-mode=restricted`)
5. логика дашборда → ответ

## Compose

Только Postgres:

```bash
docker compose up -d
docker pull crystaldba/postgres-mcp
```

## MCP (как в Claude Desktop / статье)

```bash
docker run -i --rm \
  -e DATABASE_URI=postgresql://admin:admin123@host.docker.internal:5432/app \
  crystaldba/postgres-mcp \
  --access-mode=restricted
```

Это поднимает сам `host.py` по stdio. SSE нет.

## Хост

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
cp .env.example .env
# задай LLM_MODEL

docker compose up -d
.venv/bin/python host.py
```

- API: `http://127.0.0.1:8090`
- LLM: `http://127.0.0.1:8080/v1`
- Postgres: `127.0.0.1:5432`

```bash
curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько на складе"}'
```
