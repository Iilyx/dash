# dash

Простой пайплайн:

1. `.mrt` → `catalog.json` + `catalog_pages/N.json`
2. `/ask` берёт **первый** page (`catalog_pages/0.json`), шлёт в LLM, возвращает ответ модели

## Запуск

```bash
cp .env.example .env
# LLM_MODEL=

.venv/bin/pip install -r requirements.txt
.venv/bin/python host.py
```

```bash
curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"что на этом дашборде?"}'
```

```json
{
  "page": "Products",
  "catalog": "catalog_pages/0.json",
  "answer": "..."
}
```
