# dash

Stimulsoft Dashboards viewer (Python 2024.2.2): фильтры, `/open`, скрин через `GET /shot`.

```bash
cp .env.example .env   # MRT_PATH, POSTGRES_*, license
.venv/bin/pip install -r requirements.txt
docker compose up -d   # Postgres, опционально
.venv/bin/python view_dashboard.py
```

- Viewer: http://127.0.0.1:8040/viewer  
- Open: `/open` (фильтры → готовый дашборд)  
- Shot: вкладка `/open` + `GET /shot` → PNG + PDF таблиц в `exports/`  
- Filters: `/filters`  
- Docs: `/docs`  

Порт: `VIEWER_PORT` в `.env`.
