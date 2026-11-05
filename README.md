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
6. фильтры ← вопрос / default → `queries.sql_filled`
7. SQL этих dialog sources → MCP `execute_sql` → `dialog_results`

Нужны Docker + `DATABASE_URL` или `POSTGRES_*` (см. `.env.example`).

Ответ: `page_index`, `page_name`, `source_names`, `sources`, `braces`, `variables`,
`filters`, `dialog_source_names`, `dialog_results`, `resolved`, `queries`, `catalog`,
`route`, `sources_route`, `resolve_route`, `trace`.

`ask_trace.txt`: … → DIALOG SOURCES → RESOLVE → QUERIES → DIALOG SQL (MCP) → RESULT
