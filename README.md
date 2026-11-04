# dash

1. LLM выбирает **page** → `catalog_pages/{N}.json`
2. LLM по **полному catalog** + вопросу:
   - в `pages` найти все `{Name}`
   - найти в `variables`
   - `category: "Фильтры"` → значение из вопроса или default
   - `dialog` → путь к `sources` (например `keys_column` / `dialog.source`)
   - какие `sources.sql` выполнять

```bash
cp .env.example .env   # LLM_MODEL=...
.venv/bin/pip install -r requirements.txt
.venv/bin/python host.py

curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько единиц на складе"}'
```

Ответ: `page_index`, `page_name`, `catalog`, `plan`, `route`, `trace`.

`ask_trace.txt`: QUESTION → ROUTE → CATALOG → PLAN → RESULT.
