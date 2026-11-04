# dash

1. `.mrt` → `catalog_pages/N.json` (по одной page)
2. `/ask` — LLM проходит **каждую** page, смотрит русский/видимый текст (`ru_text`), говорит подходит или нет

## Запуск

```bash
cp .env.example .env
# LLM_MODEL=...

.venv/bin/pip install -r requirements.txt
.venv/bin/python host.py
```

Thinking (Qwen 3.5) — только через `.env`:

| переменная | пример | смысл |
|---|---|---|
| `LLM_ENABLE_THINKING` | `true` / `false` | вкл/выкл thinking |
| `LLM_REASONING_EFFORT` | `low` / `medium` / `high` / `xhigh` / `none` | глубина |
| `LLM_THINKING_BUDGET` | `512` | лимит токенов thinking (вместо effort) |

```bash
curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько единиц на складе"}'
```

В консоли host — стрим (`[thinking]` / ответ). В JSON: `{"pages_checked": N, "answer": "..."}`. Текущие настройки — `GET /health` → `thinking`.
