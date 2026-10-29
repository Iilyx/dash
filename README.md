# dash

Пайплайн вопросов по Stimulsoft `.mrt`:

1. `extract_mrt` → `catalog.json`
2. каталог и дефолтные параметры → системный промпт
3. локальная LLM находит SQL
4. локальный Postgres MCP (`query`, read-only) → строки
5. логика дашборда → ответ

## Инфраструктура (compose)

Пока в compose только **БД** и **MCP**:

```bash
docker compose up -d db
docker pull mcp/postgres
```

MCP — сервис `mcp` (образ `mcp/postgres`), в одной сети с `db`.  
Хост поднимает его локально по stdio:

```bash
docker compose run --rm -i mcp
```

## Хост и модель — на машине

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
cp .env.example .env
# задай LLM_MODEL
docker compose up -d db
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

## Каталог

```bash
.venv/bin/python extract_mrt.py SalesOfProducts.mrt -o catalog.json
```
