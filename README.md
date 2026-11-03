# dash

Пайплайн вопросов по Stimulsoft `.mrt`:

1. `.mrt` → `catalog.json` + `catalog_pages/` (pack на page)
2. фильтры `{Name}` из вопроса / default
3. **router** LLM: какие pages (по полному index)
4. host грузит packs этих pages
5. **planner** LLM: какой SQL
6. host → MCP `execute_sql`
7. **answerer** LLM: ответ по строкам + виджеты

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
# задай LLM_MODEL; MRT_PATH уже = SalesOfProducts.mrt

docker compose up -d
.venv/bin/python host.py
# при старте сам: .mrt → catalog.json → MCP
```

- API: `http://127.0.0.1:8090`
- LLM: `http://127.0.0.1:8080/v1`
- Postgres: `127.0.0.1:5432`

В `/ask` только вопрос. Параметры из текста; иначе дефолты из `.mrt`.

```bash
curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько на складе"}'
```

`extract_mrt.py` нужен только если хочешь каталог вручную, без host.
