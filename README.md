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
- Filters: `POST /filters` — без body: фильтры `MRT_PATH`; `{"question":"…"}` → LLM выбирает `.mrt`  
- Boards: `/dashboards`  
- Docs: `/docs`  

Порт: `VIEWER_PORT` в `.env`.

### dashboards/

- `instruction.md` — инструкция, как выбирать дашборд  
- `*.mrt` — сами шаблоны  

`POST /filters {"question":"…"}` → LLM читает инструкцию, выбирает `.mrt`, дальше filters по этому файлу.
