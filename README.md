# dash

Stimulsoft `.mrt` + Postgres: LLM `/ask` (`host.py`) и viewer фильтров (`view_dashboard.py`).

## Viewer

Фильтры берутся из схемы `.mrt` (`Variable.Value`, иначе DataFilter `Value2`) и подставляются в `SqlCommand` (`@Param` / `{Name}`). Лог: `sql_trace.json`.

```bash
cp .env.example .env
.venv/bin/pip install -r requirements.txt
MRT_PATH=report.mrt .venv/bin/python view_dashboard.py
```

## /ask (LLM)

```bash
.venv/bin/python host.py

curl -s http://127.0.0.1:8090/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"сколько единиц на складе"}'
```

Нужны Docker + `DATABASE_URL` или `POSTGRES_*` (см. `.env.example`).
