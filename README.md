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

Ответ: `page_index`, `page_name`, `page`, `route`, `trace`.

`ask_trace.txt` — по шагам логики:
1. QUESTION → 2. PAGES/PROMPT → 3. LLM ROUTE → 4. PAGE SELECTED → 5. PAGE JSON → 6. CATALOG JSON → 7. RESULT
