# dash

Пайплайн вопросов по Stimulsoft `.mrt`:

1. `extract_mrt` → `catalog.json`
2. SQL из `.mrt` — шаблоны; `@Name` / `{Name}` — переменные
3. значение из вопроса / `parameters`, иначе дефолт из mrt
4. **Postgres MCP Pro** (`crystaldba/postgres-mcp`, `execute_sql`, restricted)
5. логика виджетов → ответ

## Compose

Только Postgres:

```bash
docker compose up -d
docker pull crystaldba/postgres-mcp
```

## MCP (как в статье / Claude Desktop)

```bash
docker run -i --rm \
  -e DATABASE_URI=postgresql://admin:admin123@host.docker.internal:5432/app \
  crystaldba/postgres-mcp \
  --access-mode=restricted
```

Это поднимает сам `host.py` по stdio.

## Запуск

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

В `/ask` только вопрос. Параметры вытаскиваются из текста; если не указаны — дефолты из `.mrt`.

```bash
curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько на складе"}'

curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько на складе в категории 3"}'
```

## Каталог

```bash
.venv/bin/python extract_mrt.py SalesOfProducts.mrt -o catalog.json
```
