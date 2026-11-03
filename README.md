# dash

FastAPI-хост: вопрос по Stimulsoft `.mrt` → параметры → SQL через Postgres MCP → ответ с логикой дашборда.

## Пайплайн `/ask`

1. `.mrt` → `extract_mrt` → `catalog.json` + `catalog_pages/`
2. Из вопроса достать `{Name}`; если нет — дефолт из `variables`
3. Router LLM выбирает pages (index), host грузит packs
4. Planner LLM выбирает SQL (`sql_filled`)
5. Host выполняет `execute_sql` через **Postgres MCP**
6. Answerer LLM применяет логику виджетов (meters / фильтры / TopN) к rows

Трасса цикла: `ask_trace.txt`.

## Запуск

```bash
cp .env.example .env
# задай LLM_MODEL (и LLM_BASE_URL если не :8080)

uv venv --python 3.12   # или уже есть .venv
.venv/bin/pip install -r requirements.txt

docker compose up -d
docker pull crystaldba/postgres-mcp

.venv/bin/python host.py
# или: .venv/bin/uvicorn host:app --host 127.0.0.1 --port 8090
```

- API: `http://127.0.0.1:8090`
- LLM: `http://127.0.0.1:8080/v1` (OpenAI-compatible)
- Postgres: `127.0.0.1:5432` (`admin` / `admin123` / `app`)

```bash
curl -s http://127.0.0.1:8090/health | jq .

curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько единиц на складе"}'
```

## Только catalog

```bash
python extract_mrt.py SalesOfProducts.mrt
```

Пишет `catalog.json` и `catalog_pages/` (index + pack на page). Полный catalog в промпт не кладётся.

## Compose

Только Postgres. MCP поднимает сам `host.py` через:

```bash
docker run -i --rm \
  -e DATABASE_URI=postgresql://admin:admin123@host.docker.internal:5432/app \
  crystaldba/postgres-mcp \
  --access-mode=restricted
```
