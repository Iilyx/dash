# dash

1. `.mrt` → `catalog_pages/` (разбивка по pages)
2. FastAPI `/ask` → LLM API выбирает page → `{"page":"..."}`

Reasoning low: короткий index pages в промпт.

## Запуск

```bash
cp .env.example .env
# задай LLM_MODEL (и LLM_BASE_URL если не :8080)

.venv/bin/pip install -r requirements.txt
.venv/bin/python host.py
```

```bash
curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько единиц на складе"}'
# {"page":"Products"}
```

1 page в отчёте — LLM не зовётся. Несколько — `POST {LLM_BASE_URL}/chat/completions`.
