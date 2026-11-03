# dash

Пайплайн вопросов по Stimulsoft `.mrt`:

1. `.mrt` (`MRT_PATH`) → при старте `catalog.json` + `catalog_pages/` (pack на каждую page)
2. `/ask`: в LLM только **index всех pages**; нужные тянет через `load_page` (можно несколько/все)
3. фильтры `{Name}` из вопроса; иначе default из `variables`
4. LLM → **Postgres MCP** (`execute_sql`) → ответ

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
