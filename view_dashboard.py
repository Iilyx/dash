#!/usr/bin/env python3
"""Отдельный viewer Stimulsoft Dashboards.PYTHON для .mrt.

По интеграции: https://www.stimulsoft.com/en/products/dashboards-python#integration

Запуск:
  .venv/bin/python view_dashboard.py
  .venv/bin/python view_dashboard.py /path/to/full.mrt
  # http://127.0.0.1:8040/
  # http://127.0.0.1:8040/debug  — variables / RequestFromUser / filter elements

Фильтры на дашборде бывают двух видов:
  1) элементы на холсте: ComboBox / ListBox / TreeView / DatePicker
  2) панель параметров: variables с RequestFromUser=true (кнопка Parameters в toolbar)

MRT_PATH / VIEWER_* / DATABASE_URL|POSTGRES_* — из .env
"""

from __future__ import annotations

import json
import os
import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from flask import Flask, jsonify, request, send_from_directory, url_for
from stimulsoft_reports.events import StiDataEventArgs, StiVariablesEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer
from stimulsoft_reports.viewer.enums import StiParametersPanelPosition

from extract_mrt import catalog_from_report, load_mrt

ROOT = Path(__file__).resolve().parent

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

FILTER_ELEMENT_TYPES = {
    "StiComboBoxElement",
    "StiListBoxElement",
    "StiTreeViewElement",
    "StiTreeViewBoxElement",
    "StiDatePickerElement",
    "StiNumberBoxElement",
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


def resolve_mrt_path() -> Path:
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        return path_of(sys.argv[1])
    return path_of(os.environ.get("MRT_PATH", "test.mrt"))


load_env()

MRT_PATH = resolve_mrt_path()
if not MRT_PATH.is_file():
    raise SystemExit(f"Нет .mrt: {MRT_PATH} (MRT_PATH в .env или путь аргументом)")

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
    """Пустой @Param → no-op, не '' (PG: invalid input syntax for type integer \"\")."""
    if not query:
        return query
    pat = re.escape(name)
    query = re.sub(
        rf"[\w.\"`]+?\s+IN\s*\(\s*@{pat}\s*\)",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        rf"[\w.\"`]+?\s*=\s*@{pat}\b",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    return re.sub(rf"@{pat}\b", "NULL", query, flags=re.IGNORECASE)


def mrt_looks_anonymized(path: Path) -> bool:
    """Обезличка: все строковые значения пустые."""
    raw = path.read_text(encoding="utf-8-sig")
    nonempty = [v for v in re.findall(r':\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw) if v]
    return len(nonempty) == 0


def dashboard_debug(path: Path) -> dict:
    """Что Stimulsoft должен показать как фильтры."""
    try:
        catalog = catalog_from_report(load_mrt(path))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}

    variables = []
    for var in catalog.get("variables") or []:
        if not isinstance(var, dict):
            continue
        variables.append(
            {
                "name": var.get("name"),
                "alias": var.get("alias"),
                "type": var.get("type"),
                "category": var.get("category"),
                "value": var.get("value"),
                "request_from_user": bool(var.get("request_from_user")),
                "sql_parameter": bool(var.get("sql_parameter")),
                "has_dialog": isinstance(var.get("dialog"), dict),
                "dialog": var.get("dialog"),
            }
        )

    filter_elements = []
    for page in catalog.get("pages") or []:
        if not isinstance(page, dict):
            continue
        page_name = page.get("alias") or page.get("name")
        for el in page.get("elements") or []:
            if not isinstance(el, dict):
                continue
            typ = el.get("type")
            role = el.get("role")
            if typ in FILTER_ELEMENT_TYPES or role == "filter":
                filter_elements.append(
                    {
                        "page": page_name,
                        "name": el.get("name"),
                        "type": typ,
                        "title": el.get("title") or el.get("text"),
                        "data_source": el.get("data_source"),
                    }
                )

    request_vars = [v for v in variables if v.get("request_from_user")]
    return {
        "ok": True,
        "path": str(path.resolve()),
        "anonymized": mrt_looks_anonymized(path),
        "report": catalog.get("report"),
        "request_from_user_variables": request_vars,
        "request_from_user_count": len(request_vars),
        "all_variables_count": len(variables),
        "variables": variables,
        "filter_elements_on_canvas": filter_elements,
        "filter_elements_count": len(filter_elements),
        "hint": (
            "Панель Parameters (toolbar) ← request_from_user_variables. "
            "Виджеты ComboBox/TreeView на холсте ← filter_elements_on_canvas."
        ),
    }


def prepare_variables(args: StiVariablesEventArgs) -> None:
    """Только лог: какие RequestFromUser пришли. Values не трогаем (иначе сбивается dialog)."""
    variables = args.variables or {}
    names = list(variables.keys())
    print(f"[viewer] onPrepareVariables count={len(names)} names={names}", flush=True)
    for name, var in variables.items():
        vtype = var.type.value if getattr(var, "type", None) is not None else "?"
        empty = _is_empty(var.value)
        print(
            f"[viewer]   var {name} type={vtype} empty={empty} value={var.value!r}",
            flush=True,
        )


def begin_process_data(args: StiDataEventArgs) -> None:
    """Connection string из .env; пустые @Param не слать как ''."""
    if PG_CONN and args.connectionString is not None:
        args.connectionString = PG_CONN

    src = args.dataSource or "?"
    qpreview = (args.queryString or "")[:180].replace("\n", " ")
    print(f"[viewer] SQL source={src} q={qpreview!r}", flush=True)

    params = args.parameters or {}
    if not params:
        return

    query = args.queryString or ""
    for name, param in params.items():
        print(
            f"[viewer]   @{name} typeGroup={param.typeGroup!r} "
            f"typeName={param.typeName!r} value={param.value!r}",
            flush=True,
        )
        if not _is_empty(param.value):
            continue
        kind = "number" if _is_number_param(param) else (param.typeGroup or "value")
        query = _soften_empty_param(query, name)
        param.value = None
        print(f"[viewer] empty @{name} ({kind}) on {src} → no-op", flush=True)

    if query != (args.queryString or ""):
        args.queryString = query


@app.get("/mrt")
def serve_mrt():
    resp = send_from_directory(
        MRT_PATH.parent,
        MRT_PATH.name,
        mimetype="application/json",
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/debug")
def debug():
    """JSON: RequestFromUser + filter elements из текущего .mrt."""
    return jsonify(dashboard_debug(MRT_PATH))


@app.route("/", methods=["GET", "POST"])
@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    sti = StiViewer()
    sti.options.appearance.fullScreenMode = True
    sti.options.appearance.parametersPanelPosition = StiParametersPanelPosition.TOP
    sti.options.toolbar.visible = True
    sti.options.toolbar.showParametersButton = True
    sti.onPrepareVariables += prepare_variables
    sti.onBeginProcessData += begin_process_data

    if sti.processRequest(request):
        return sti.getFrameworkResponse()

    report = StiReport()
    # абсолютный URL надёжнее для клиентского loadFile
    report.loadFile(url_for("serve_mrt", _external=True))
    sti.report = report
    return sti.getFrameworkResponse()


def main() -> None:
    info = dashboard_debug(MRT_PATH)
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    print(f"MRT:     {MRT_PATH.resolve()}")
    print(f"anon:    {info.get('anonymized')}")
    print(f"RFU:     {info.get('request_from_user_count')} variables")
    print(f"filters: {info.get('filter_elements_count')} canvas elements")
    if info.get("request_from_user_variables"):
        print(
            "RFU names:",
            [v.get("name") for v in info["request_from_user_variables"]],
        )
    if info.get("filter_elements_on_canvas"):
        print(
            "canvas:",
            [
                f"{e.get('type')}:{e.get('name')}"
                for e in info["filter_elements_on_canvas"]
            ],
        )
    print(f"DB:      {'yes' if PG_CONN else 'no'}")
    print(f"Open:    {url}")
    print(f"Debug:   {url}debug")
    if info.get("anonymized"):
        print(
            "WARNING: .mrt обезличен (пустые строки) — фильтров не будет. "
            "Запусти: python view_dashboard.py /path/to/full.mrt",
            flush=True,
        )
    if OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=VIEWER_HOST, port=VIEWER_PORT, debug=False)


if __name__ == "__main__":
    main()
