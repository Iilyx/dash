# dash

1. `.mrt` → `catalog_pages/N.json`
2. `/ask` — route page → переменные из вопроса (или дефолты) → fill SQL → Postgres MCP → ответ

## Запуск

```bash
cp .env.example .env
# LLM_MODEL=...
# POSTGRES_* или DATABASE_URL

docker compose up -d
.venv/bin/pip install -r requirements.txt
.venv/bin/python host.py
```

Thinking (Qwen 3.5) — через `.env`:

| переменная | пример | смысл |
|---|---|---|
| `LLM_ENABLE_THINKING` | `true` / `false` | вкл/выкл thinking |
| `LLM_REASONING_EFFORT` | `low` / `medium` / `high` / `xhigh` / `none` | глубина |
| `LLM_THINKING_BUDGET` | `512` | лимит токенов thinking (вместо effort) |

```bash
curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько единиц на складе по категории 8"}'
```

План `/ask` — куски тянем **полными `{}` из catalog JSON**, не собираем свои схемы:

1. полный **page `{}`**
2. полные `variables[]` с `"category": "Фильтры"` + полные `sources[].parameters[]` этой page → значение из вопроса или дефолт
3. полные `sources[]` по name с page → `sql_filled`
4. MCP `execute_sql`
5. ответ

В логах: `STEP 1/5` … `STEP 5/5`. В ответе: `page`, `filters`, `sources`, `variables`, `queries`, `sql_results`, `steps`, `trace`.
