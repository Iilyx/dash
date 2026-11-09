#!/usr/bin/env python3
"""Отдельный viewer Stimulsoft Dashboards.PYTHON для .mrt.

По интеграции: https://www.stimulsoft.com/en/products/dashboards-python#integration
Sample: Showing_a_Dashboard_in_the_Viewer (Flask).

Запуск:
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python view_dashboard.py
  # открыть http://127.0.0.1:8040/

MRT_PATH / VIEWER_HOST / VIEWER_PORT / DATABASE_URL|POSTGRES_* — из .env
"""

from __future__ import annotations

import os
import re
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from flask import Flask, request, send_from_directory, url_for
from stimulsoft_reports.events import StiDataEventArgs, StiVariablesEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.report.enums.StiVariableType import StiVariableType
from stimulsoft_reports.viewer import StiViewer

ROOT = Path(__file__).resolve().parent

# typeGroup / typeName → числовой параметр (иначе '' уходит в SQL как строка)
_NUM_TYPE_HINTS = (
    "number",
    "int",
    "long",
    "short",
    "byte",
    "decimal",
    "double",
    "float",
    "single",
    "money",
    "numeric",
)

# Типы переменных-фильтров, где пусто = «не выбрано», а не ''
_NUMERIC_VAR_TYPES = {
    StiVariableType.BYTE,
    StiVariableType.DECIMAL,
    StiVariableType.DOUBLE,
    StiVariableType.INT,
    StiVariableType.INT16,
    StiVariableType.SHORT,
    StiVariableType.INT32,
    StiVariableType.INT64,
    StiVariableType.LONG,
    StiVariableType.SBYTE,
    StiVariableType.SINGLE,
    StiVariableType.UINT16,
    StiVariableType.USHORT,
    StiVariableType.UINT,
    StiVariableType.UINT32,
    StiVariableType.UINT64,
    StiVariableType.ULONG,
}


def load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def path_of(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def database_url() -> str:
    configured = os.environ.get("DATABASE_URL", "").strip()
    if configured:
        return configured
    user = os.environ.get("POSTGRES_USER", "").strip()
    password = os.environ.get("POSTGRES_PASSWORD", "").strip()
    db = os.environ.get("POSTGRES_DB", "").strip()
    port = os.environ.get("POSTGRES_PORT", "5432").strip() or "5432"
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if user and password and db:
        return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    return ""


def stimulsoft_pg_connection_string(url: str) -> str:
    """Stimulsoft PostgreSQL: Host=…; Port=…; Database=…; User Id=…; Password=…"""
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.path.lstrip("/"):
        return ""
    user = parsed.username or ""
    password = parsed.password or ""
    host = parsed.hostname
    port = parsed.port or 5432
    database = parsed.path.lstrip("/").split("?", 1)[0]
    return (
        f"Host={host}; Port={port}; Database={database}; "
        f"User Id={user}; Password={password};"
    )


load_env()

MRT_PATH = path_of(os.environ.get("MRT_PATH", "test.mrt"))
if not MRT_PATH.is_file():
    raise SystemExit(f"Нет .mrt: {MRT_PATH} (задайте MRT_PATH в .env)")

VIEWER_HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "8040") or "8040")
OPEN_BROWSER = os.environ.get("VIEWER_OPEN", "1").lower() in {"1", "true", "yes"}

PG_CONN = stimulsoft_pg_connection_string(database_url()) if database_url() else ""

app = Flask(__name__)


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return True
    return False


def _is_number_param(param) -> bool:
    group = str(getattr(param, "typeGroup", "") or "").lower()
    name = str(getattr(param, "typeName", "") or "").lower()
    if group == "number":
        return True
    return any(h in group or h in name for h in _NUM_TYPE_HINTS)


def _soften_empty_param(query: str, name: str) -> str:
    """Фильтр не выбран → убрать условие, а не подставлять '' / NULL.

    CategoryID = @Category  →  TRUE
    CategoryID IN (@Category) → TRUE
    оставшиеся @Category → NULL
    """
    if not query:
        return query
    pat = re.escape(name)
    # col IN (@Param) / col in (@Param)
    query = re.sub(
        rf"[\w.\"`]+?\s+IN\s*\(\s*@{pat}\s*\)",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    # col = @Param / col=@Param
    query = re.sub(
        rf"[\w.\"`]+?\s*=\s*@{pat}\b",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    # всё остальное
    return re.sub(rf"@{pat}\b", "NULL", query, flags=re.IGNORECASE)


def prepare_variables(args: StiVariablesEventArgs) -> None:
    """Пустые переменные фильтров: не оставлять '' для Int/List."""
    variables = args.variables or {}
    for name, var in variables.items():
        if not _is_empty(var.value):
            continue
        vtype = var.type
        if vtype in _NUMERIC_VAR_TYPES:
            # None → дальше SQL-параметр тоже увидит пустоту и soften'ится
            var.value = None
            print(f"[viewer] filter var {name} ({vtype.value}) empty → None", flush=True)
        elif vtype is not None and str(vtype.value).endswith("List"):
            var.value = []
            print(f"[viewer] filter var {name} ({vtype.value}) empty → []", flush=True)


def begin_process_data(args: StiDataEventArgs) -> None:
    """Connection string из .env; пустые фильтры/@Param не слать как ''."""
    if PG_CONN and args.connectionString is not None:
        args.connectionString = PG_CONN

    params = args.parameters or {}
    if not params:
        return

    # Дашборд с фильтрами: пока ничего не выбрано, Stimulsoft часто шлёт "".
    # PG: invalid input syntax for type integer ""
    query = args.queryString or ""
    for name, param in params.items():
        if not _is_empty(param.value):
            continue
        kind = "number" if _is_number_param(param) else (param.typeGroup or "value")
        query = _soften_empty_param(query, name)
        param.value = None
        src = args.dataSource or "?"
        print(f"[viewer] empty filter @{name} ({kind}) on {src} → no-op", flush=True)

    if query != (args.queryString or ""):
        args.queryString = query


@app.get("/mrt")
def serve_mrt():
    """Отдать .mrt клиентскому JS-движку Stimulsoft."""
    return send_from_directory(
        MRT_PATH.parent,
        MRT_PATH.name,
        mimetype="application/json",
    )


@app.route("/", methods=["GET", "POST"])
@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    sti = StiViewer()
    sti.options.appearance.fullScreenMode = True
    # фильтры/variables → defaults; SQL @Param → не ''
    sti.onPrepareVariables += prepare_variables
    sti.onBeginProcessData += begin_process_data

    if sti.processRequest(request):
        return sti.getFrameworkResponse()

    report = StiReport()
    # URL: клиент скачает .mrt и построит дашборд в браузере
    report.loadFile(url_for("serve_mrt"))
    sti.report = report
    return sti.getFrameworkResponse()


def main() -> None:
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    print(f"MRT:  {MRT_PATH}")
    print(f"DB:   {'yes (override connectionString)' if PG_CONN else 'no (.mrt as-is)'}")
    print(f"Open: {url}")
    if OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=VIEWER_HOST, port=VIEWER_PORT, debug=False)


if __name__ == "__main__":
    main()
