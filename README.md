# dash

`.mrt` → `catalog_pages/N.json` → `/ask` спрашивает LLM какую page выбрать.

```bash
cp .env.example .env   # LLM_MODEL=...
.venv/bin/pip install -r requirements.txt
.venv/bin/python host.py

curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько единиц на складе"}'
```

Пайплайн `/ask`:
1. LLM → page
2. LLM → `source_names` по ссылкам в pages
3. SQL этих sources → `{Name}` → `variables` по `name`
4. `filters` = все `variables` с `category` Фильтр (не только из `{}`)
5. `variable.dialog.source` → ещё sources по name
6. SQL dialog sources → MCP `execute_sql` → `dialog_results`
7. LLM resolve фильтров с учётом `variable.type` (в т.ч. StringList):
   вопрос → `dialog_results` → default; `{Name}` → SQL-литерал по типу
8. `queries.sql_filled` page sources → MCP `execute_sql` → `sql_results`

Нужны Docker + `DATABASE_URL` или `POSTGRES_*` (см. `.env.example`).

Ответ: … `dialog_results`, `resolved`, `queries`, `sql_results`, `catalog`, `route`, …

`ask_trace.txt`: … → DIALOG SQL → RESOLVE → QUERIES → SQL (MCP) → RESULT
