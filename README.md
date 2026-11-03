# dash

Сейчас хост делает один шаг: вопрос → LLM выбирает pages → пишет `page.txt`.

## Запуск

```bash
cp .env.example .env
# задай LLM_MODEL

.venv/bin/pip install -r requirements.txt
.venv/bin/python host.py
```

Нужны: `LLM_BASE_URL` (по умолчанию `http://127.0.0.1:8080/v1`) и `LLM_MODEL`.  
Таймаут LLM: `LLM_TIMEOUT` (секунды, по умолчанию 300).

```bash
curl -s http://127.0.0.1:8090/health | jq .

curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько единиц на складе"}'
# {"pages":[0]}
```

Выбранные pages пишутся в `page.txt` (на диск), в HTTP только `{"pages":[...]}`.

## Catalog

При старте host сам:

```text
MRT → catalog.json + catalog_pages/
```

Вручную:

```bash
python extract_mrt.py SalesOfProducts.mrt
```

## Postgres

`docker-compose.yml` — Postgres на потом (SQL/MCP пока отключены в host).
