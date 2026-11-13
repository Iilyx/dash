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

Пайплайн `/ask`:
1. LLM → page
2. LLM → `source_names` по ссылкам в pages
3. SQL этих sources → `{Name}` → `variables` по `name`
4. `filters` = все `variables` с `category` Фильтр (не только из `{}`)
5. `variable.dialog.source` → ещё sources по name
6. SQL dialog sources → MCP `execute_sql` → `dialog_results`
7. LLM resolve фильтров с учётом `variable.type` (в т.ч. StringList):
   вопрос → `dialog_results` → default; `{Name}` → SQL-литерал по типу
8. `queries.sql_filled` page sources → MCP `execute_sql` → `sql_results`
9. LLM: все `page.elements` + SQL → образная доска (`view`) + `dashboard.layout`

Нужны Docker + `DATABASE_URL` или `POSTGRES_*` (см. `.env.example`).

Ответ: `answer`, `view` (markdown-сцена), `dashboard.layout`, `sql_results`, …

`ask_trace.txt`: … → SQL (MCP) → DASHBOARD → RESULT

## Stimulsoft viewer (Python 2024.2.2)

```bash
cp .env.example .env   # MRT_PATH, POSTGRES_*, license
.venv/bin/pip install -r requirements.txt
.venv/bin/python view_dashboard.py
```


Viewer: http://127.0.0.1:8040/viewer · Designer: `/designer`  
(порт из `.env`: `VIEWER_PORT`)

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

### Готовая страница «как на экране»

После рендера (таблицы, кнопки, графики) в viewer:

- **Ctrl+Shift+S** — скачает PNG + JSON (`datasets` + `params`) и положит копию в `data/filled/`
- или в консоли: `await dashDownloadFilled()`

```bash
curl -s http://127.0.0.1:8040/api/filled      # метаданные последнего снимка
curl -s http://127.0.0.1:8040/api/filled.png  -o filled.png
```

`Format(x, "N2")` → JS-вид (`Format("{0:N2}", x)`):

```bash
.venv/bin/python convert_mrt.py test.mrt
MRT_PATH=_converted.mrt .venv/bin/python view_dashboard.py
```
