# dash

Stimulsoft Dashboards.PYTHON **2024.2.2** — viewer + designer для `.mrt`.

```bash
cp .env.example .env   # MRT_PATH, POSTGRES_*, license
.venv/bin/pip install -r requirements.txt
.venv/bin/python view_dashboard.py
```

Viewer: http://127.0.0.1:8040/viewer · Designer: `/designer`  
(порт из `.env`: `VIEWER_PORT`)

Нужны Docker + `DATABASE_URL` или `POSTGRES_*` (см. `.env.example` / `docker-compose.yml`).

Параметры дашборда (имена = variables / SQL `@param` в `.mrt`):

```bash
# задать
curl -s -X POST http://127.0.0.1:8040/api/params \
  -H 'Content-Type: application/json' \
  -d '{"Region":"West","Visits":5}'

# или query
open 'http://127.0.0.1:8040/viewer?Region=West'

# текущие (+ последний interaction с кликов)
curl -s http://127.0.0.1:8040/api/params
```

Клики на дашборде: на `/viewer` справа внизу debug-панель с полным `args`.  
То же в `GET /api/interactions` и в `interaction` у `GET /api/params`.

`Format(x, "N2")` → JS-вид (`Format("{0:N2}", x)`):

```bash
.venv/bin/python convert_mrt.py test.mrt
MRT_PATH=_converted.mrt .venv/bin/python view_dashboard.py
```
