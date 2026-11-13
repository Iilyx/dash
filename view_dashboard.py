#!/usr/bin/env python3
"""Stimulsoft Dashboards.PYTHON 2024.2.2 — по официальным sample Flask.

Docs / samples:
  Editing a Dashboard Template in the Designer
  Showing a Dashboard in the Viewer
  How to Activate the Product
  Using Parameters in SQL Query (onBeginProcessData)
  Setting Dashboard Variables on the Server-Side
  passQueryParametersToReport / onPrepareVariables / onInteraction

Параметры:
  POST /api/params  {"Region":"West","Visits":5}
  GET  /api/params
  GET  /viewer?Region=West   (query → переменные mrt)
  клики/фильтры на дашборде → POST /api/params (onInteraction)

Запуск:
  .venv/bin/pip install -r requirements.txt
  MRT_PATH=твой.mrt .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import webbrowser
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import psycopg
import stimulsoft_dashboards  # noqa: F401 — подтягивает dashboards.js
from extract_mrt import catalog_from_report, load_mrt
from flask import Flask, Response, jsonify, request
from stimulsoft_data_adapters.enums.StiFrameworkType import StiFrameworkType
from stimulsoft_reports.classes.StiResult import StiResult
from stimulsoft_reports.designer import StiDesigner
from stimulsoft_reports.events import (
    StiDataEventArgs,
    StiReportEventArgs,
    StiVariablesEventArgs,
)
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.report.dictionary.StiVariable import StiVariable
from stimulsoft_reports.report.enums.StiVariableType import StiVariableType
from stimulsoft_reports.viewer import StiViewer

BRACE_NAME = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
QUOTED_BRACE = re.compile(
    r"(?P<q>['\"])\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}(?P=q)"
)
AT_PARAM = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")

ROOT = Path(__file__).resolve().parent


def load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip("'\"")


def path_of(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def apply_license(component) -> None:
    """В 2024.2.x лицензия вешается на instance: component.license.setKey/setFile."""
    key = (
        os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_KEY", "").strip()
        or os.environ.get("STIMULSOFT_REPORTS_LICENSE_KEY", "").strip()
        or os.environ.get("STIMULSOFT_LICENSE_KEY", "").strip()
    )
    file_raw = (
        os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_FILE", "").strip()
        or os.environ.get("STIMULSOFT_REPORTS_LICENSE_FILE", "").strip()
        or os.environ.get("STIMULSOFT_LICENSE_FILE", "").strip()
    )
    if key:
        component.license.setKey(key)
    elif file_raw:
        component.license.setFile(str(path_of(file_raw).resolve()))


load_env()

_mrt = os.environ.get("MRT_PATH", "").strip()
if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
    MRT_PATH = path_of(sys.argv[1])
elif _mrt:
    MRT_PATH = path_of(_mrt)
else:
    raise SystemExit("Задай MRT_PATH в .env")
if not MRT_PATH.is_file():
    raise SystemExit(f"Нет .mrt: {MRT_PATH}")

HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
PORT = int(os.environ.get("VIEWER_PORT", "8040") or "8040")
OPEN_BROWSER = os.environ.get("VIEWER_OPEN", "1").lower() in {"1", "true", "yes"}

_db = os.environ.get("DATABASE_URL", "").strip()
if not _db:
    user = os.environ.get("POSTGRES_USER", "").strip()
    password = os.environ.get("POSTGRES_PASSWORD", "").strip()
    db = os.environ.get("POSTGRES_DB", "").strip()
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("POSTGRES_PORT", "5432").strip() or "5432"
    if user and password and db:
        _db = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"

PG_CONN = ""
if _db:
    u = urlparse(_db)
    if u.hostname and u.path.lstrip("/"):
        PG_CONN = (
            f"Host={u.hostname}; Port={u.port or 5432}; "
            f"Database={u.path.lstrip('/').split('?', 1)[0]}; "
            f"User Id={u.username or ''}; Password={u.password or ''};"
        )
if not PG_CONN:
    raise SystemExit("Нет DATABASE_URL / POSTGRES_USER+PASSWORD+DB в .env")

app = Flask(__name__)

# Последние параметры: POST /api/params, query /viewer?X=, клики onInteraction.
_params_lock = threading.Lock()
_PARAMS: dict[str, Any] = {}
_LAST_INTERACTION: dict[str, Any] | None = None
_INTERACTIONS: list[dict[str, Any]] = []
_INTERACTIONS_MAX = 50

_STI_QUERY_SKIP = {"sti_event", "sti_data", "sti_handler"}
_DEBUG_JS = (ROOT / "dash_debug.js").read_text(encoding="utf-8")

# Последний ответ POST/GET /api/filled (только JSON, без скринов).
_LAST_FILLED: dict[str, Any] | None = None


def _pg_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    user = os.environ.get("POSTGRES_USER", "").strip()
    password = os.environ.get("POSTGRES_PASSWORD", "").strip()
    db = os.environ.get("POSTGRES_DB", "").strip()
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("POSTGRES_PORT", "5432").strip() or "5432"
    if user and password and db:
        return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    raise RuntimeError("Нет DATABASE_URL / POSTGRES_*")


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return "'" + value.isoformat().replace("'", "''") + "'"
    if isinstance(value, (list, tuple)):
        parts = ", ".join(_sql_literal(v) for v in value)
        return f"({parts})" if parts else "(NULL)"
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def _fill_sql(sql: str, params: dict[str, Any]) -> str:
    by_key = {str(k).lower(): v for k, v in params.items()}

    def repl_quoted(match: re.Match[str]) -> str:
        key = match.group("name").lower()
        if key not in by_key:
            return match.group(0)
        value = by_key[key]
        if value is None:
            return "NULL"
        quote = match.group("q")
        inner = str(value).replace("'", "''").replace('"', '""')
        return f"{quote}{inner}{quote}"

    def repl_bare(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if key not in by_key:
            return match.group(0)
        return _sql_literal(by_key[key])

    def repl_at(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if key not in by_key:
            return match.group(0)
        return _sql_literal(by_key[key])

    out = QUOTED_BRACE.sub(repl_quoted, sql)
    out = BRACE_NAME.sub(repl_bare, out)
    return AT_PARAM.sub(repl_at, out)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return str(value)


def _run_sql(sql: str, row_limit: int) -> dict[str, Any]:
    with psycopg.connect(_pg_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return {
                    "columns": [],
                    "rowcount": cur.rowcount,
                    "rows": [],
                }
            columns = [d.name for d in cur.description]
            rows_raw = cur.fetchmany(row_limit)
            rows = [
                {columns[i]: _jsonable(row[i]) for i in range(len(columns))}
                for row in rows_raw
            ]
            return {
                "columns": columns,
                "rowcount": len(rows),
                "rows": rows,
                "truncated": len(rows) >= row_limit,
            }


def _default_params_from_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for var in catalog.get("variables") or []:
        if not isinstance(var, dict) or not var.get("name"):
            continue
        if "value" in var and var["value"] is not None:
            out[str(var["name"])] = var["value"]
    return out


def build_filled(
    params: dict[str, Any],
    *,
    row_limit: int = 500,
    source_names: list[str] | None = None,
    include_lazy: bool = False,
) -> dict[str, Any]:
    """params → SQL из .mrt → datasets (без браузера / скринов)."""
    report = load_mrt(MRT_PATH)
    catalog = catalog_from_report(report)
    resolved = _default_params_from_catalog(catalog)
    resolved.update(params)

    want = None
    if source_names:
        want = {n.lower() for n in source_names if isinstance(n, str)}

    datasets: dict[str, Any] = {}
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict):
            continue
        name = source.get("name")
        sql = source.get("sql")
        if not isinstance(name, str) or not isinstance(sql, str) or not sql.strip():
            continue
        if want is not None and name.lower() not in want:
            continue
        if not include_lazy and source.get("connect_on_start") is False:
            continue
        filled = _fill_sql(sql, resolved)
        try:
            result = _run_sql(filled, row_limit)
            datasets[name] = {
                "ok": True,
                "sql": sql,
                "sql_filled": filled,
                **result,
            }
        except Exception as exc:  # noqa: BLE001
            datasets[name] = {
                "ok": False,
                "sql": sql,
                "sql_filled": filled,
                "error": str(exc),
            }

    return {
        "ok": True,
        "params": resolved,
        "sources_total": len(catalog.get("sources") or []),
        "datasets": datasets,
        "pages_count": len(catalog.get("pages") or []),
    }


def _public_query_params() -> dict[str, str]:
    return {
        k: v
        for k, v in request.args.items()
        if k not in _STI_QUERY_SKIP and not k.startswith("sti_")
    }


def _body_params() -> dict[str, Any]:
    out: dict[str, Any] = {}
    if request.form:
        out.update({k: v for k, v in request.form.items()})
    if request.is_json:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            nested = body.get("params")
            if isinstance(nested, dict):
                out.update(nested)
            else:
                out.update(
                    {
                        k: v
                        for k, v in body.items()
                        if k not in {"params", "interaction"}
                    }
                )
    return out


def params_snapshot() -> dict[str, Any]:
    with _params_lock:
        return dict(_PARAMS)


def params_update(values: dict[str, Any]) -> dict[str, Any]:
    with _params_lock:
        for key, value in values.items():
            if value is None:
                _PARAMS.pop(key, None)
            else:
                _PARAMS[key] = value
        return dict(_PARAMS)


def coerce_value(type_name: str, value: Any) -> Any:
    if value is None or type_name is None:
        return value
    if isinstance(value, (dict, list)):
        return value
    text = value if isinstance(value, str) else str(value)
    try:
        if type_name in {
            StiVariableType.INT,
            StiVariableType.INT16,
            StiVariableType.INT32,
            StiVariableType.INT64,
            StiVariableType.SHORT,
            StiVariableType.LONG,
            StiVariableType.BYTE,
            StiVariableType.SBYTE,
            StiVariableType.UINT,
            StiVariableType.UINT16,
            StiVariableType.UINT32,
            StiVariableType.UINT64,
            StiVariableType.USHORT,
            StiVariableType.ULONG,
        }:
            return int(float(text)) if text != "" else value
        if type_name in {
            StiVariableType.DOUBLE,
            StiVariableType.DECIMAL,
            StiVariableType.SINGLE,
            "Float",
        }:
            return float(text) if text != "" else value
        if type_name == StiVariableType.BOOLEAN:
            if isinstance(value, bool):
                return value
            return text.strip().lower() in {"1", "true", "yes", "y", "on"}
        if type_name == StiVariableType.DATETIME:
            if isinstance(value, datetime):
                return value
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
    except (TypeError, ValueError):
        return value
    return value


def apply_params_to_variables(args: StiVariablesEventArgs) -> None:
    incoming = params_snapshot()
    incoming.update(_public_query_params())
    if not incoming:
        return
    variables = args.variables
    if variables is None:
        args.variables = {}
        variables = args.variables
    for name, raw in incoming.items():
        if name in variables:
            var = variables[name]
            var.value = coerce_value(var.typeName, raw)
        else:
            variables[name] = StiVariable(name, StiVariableType.STRING, str(raw))


def begin_process_data(args: StiDataEventArgs) -> None:
    args.connectionString = PG_CONN
    incoming = params_snapshot()
    if not incoming or not args.parameters:
        return
    for name, param in args.parameters.items():
        if name in incoming:
            param.value = incoming[name]


def prepare_variables(args: StiVariablesEventArgs) -> None:
    apply_params_to_variables(args)


def save_report(args: StiReportEventArgs):
    if args.report is None:
        return StiResult.getError("empty report")
    try:
        MRT_PATH.write_text(
            json.dumps(args.report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return StiResult.getError(str(exc))
    return None


def sti_process(component) -> bool:
    """2024.2.2: ImportError django в __processFrameworkRequest ломает Flask-детект.

    Не передаём flask.request — кормим query/body вручную и форсим framework=Flask.
    """
    handled = component.processRequest(
        query=request.args.to_dict(),
        body=request.get_data(False),
    )
    component.handler.framework = StiFrameworkType.FLASK
    component.handler.origin = request.origin or "*"
    component.handler.error = None
    component.handler.passQueryParametersToReport = True
    return handled


def sti_response(component, inject_debug: bool = False):
    component.handler.framework = StiFrameworkType.FLASK
    component.handler.passQueryParametersToReport = True
    if not component.handler.origin:
        component.handler.origin = request.origin or "*"
    resp = component.getFrameworkResponse()
    if inject_debug and hasattr(resp, "get_data") and hasattr(resp, "set_data"):
        html = resp.get_data(as_text=True)
        try:
            ver = int((ROOT / "dash_debug.js").stat().st_mtime)
        except OSError:
            ver = 1
        tag = f'<script src="/dash-debug.js?v={ver}"></script>\n'
        if "dash-debug.js" not in html:
            if "</head>" in html:
                html = html.replace("</head>", tag + "</head>", 1)
            else:
                html = tag + html
            resp.set_data(html)
    return resp


def remember_interaction(dump: Any) -> None:
    global _LAST_INTERACTION
    if not isinstance(dump, dict):
        dump = {"raw": dump}
    with _params_lock:
        _LAST_INTERACTION = dump
        _INTERACTIONS.append(dump)
        overflow = len(_INTERACTIONS) - _INTERACTIONS_MAX
        if overflow > 0:
            del _INTERACTIONS[:overflow]
    action = dump.get("action")
    keys = dump.get("__keys")
    print(f"[interaction] action={action!r} keys={keys}", flush=True)


def wire_viewer(viewer: StiViewer) -> None:
    apply_license(viewer)
    viewer.options.appearance.fullScreenMode = True
    viewer.handler.passQueryParametersToReport = True
    viewer.onBeginProcessData += begin_process_data
    viewer.onPrepareVariables += prepare_variables
    # alnum → вызов window.dashCaptureInteraction(args) из dash_debug.js
    viewer.onInteraction += "dashCaptureInteraction"


def wire_designer(designer: StiDesigner) -> None:
    apply_license(designer)
    designer.options.appearance.fullScreenMode = True
    designer.handler.passQueryParametersToReport = True
    designer.onBeginProcessData += begin_process_data
    designer.onPrepareVariables += prepare_variables
    designer.onSaveReport += save_report
    designer.onSaveAsReport += save_report


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/dash-debug.js")
def dash_debug_js():
    resp = Response(_DEBUG_JS, mimetype="application/javascript; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/params")
def get_params():
    with _params_lock:
        return jsonify(
            {
                "params": dict(_PARAMS),
                "interaction": _LAST_INTERACTION,
                "interactions_count": len(_INTERACTIONS),
            }
        )


@app.get("/api/interactions")
def get_interactions():
    with _params_lock:
        return jsonify({"interactions": list(_INTERACTIONS)})


@app.post("/api/params")
def post_params():
    incoming = _body_params()
    incoming.update(_public_query_params())
    body = request.get_json(silent=True) if request.is_json else None
    if isinstance(body, dict) and body.get("interaction") is not None:
        remember_interaction(body.get("interaction"))
    updated = params_update(incoming)
    return jsonify({"ok": True, "params": updated})


@app.delete("/api/params")
def clear_params():
    global _LAST_INTERACTION
    with _params_lock:
        _PARAMS.clear()
        _LAST_INTERACTION = None
        _INTERACTIONS.clear()
    return jsonify({"ok": True, "params": {}})


@app.post("/api/filled")
def post_filled():
    """Только API: params → SQL из .mrt → datasets (строки из Postgres). Без скринов."""
    global _LAST_FILLED
    body = request.get_json(silent=True) if request.is_json else None
    if not isinstance(body, dict):
        body = {}
    incoming = _body_params()
    incoming.update(_public_query_params())
    nested = body.get("params") if isinstance(body.get("params"), dict) else None
    if nested:
        incoming.update(nested)
    # плоский JSON тоже принимаем как params
    for k, v in body.items():
        if k in {"params", "row_limit", "source_names", "include_lazy", "run_sql"}:
            continue
        incoming[k] = v
    if incoming:
        params_update(incoming)

    with _params_lock:
        merged = dict(_PARAMS)
    merged.update(incoming)

    row_limit = body.get("row_limit", 500)
    try:
        row_limit = max(1, min(int(row_limit), 5000))
    except (TypeError, ValueError):
        row_limit = 500
    names = body.get("source_names")
    if not isinstance(names, list):
        names = None
    include_lazy = bool(body.get("include_lazy"))

    try:
        result = build_filled(
            merged,
            row_limit=row_limit,
            source_names=names,
            include_lazy=include_lazy,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500

    result["ts"] = datetime.now().isoformat(timespec="seconds")
    with _params_lock:
        _LAST_FILLED = result
    return jsonify(result)


@app.get("/api/filled")
def get_filled():
    """Повторить filled с текущими /api/params (+ query). Или вернуть последний кэш ?cache=1."""
    global _LAST_FILLED
    if request.args.get("cache", "").lower() in {"1", "true", "yes"}:
        with _params_lock:
            if _LAST_FILLED is None:
                return jsonify(
                    {
                        "ok": False,
                        "error": "no cache — сначала POST /api/filled",
                    }
                ), 404
            return jsonify(_LAST_FILLED)

    incoming = _public_query_params()
    if incoming:
        params_update(incoming)
    with _params_lock:
        merged = dict(_PARAMS)
    merged.update(incoming)
    try:
        row_limit = max(1, min(int(request.args.get("row_limit", 500)), 5000))
    except (TypeError, ValueError):
        row_limit = 500
    try:
        result = build_filled(merged, row_limit=row_limit)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    result["ts"] = datetime.now().isoformat(timespec="seconds")
    with _params_lock:
        _LAST_FILLED = result
    return jsonify(result)


@app.route("/mrt", methods=["GET", "POST"])
def serve_mrt():
    """Отдаём .mrt; пустой DashboardViewerSettings=0 прячет Export на dashboard."""
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # StiDashboardViewerSettings.All = 1023 (Toolbar+Refresh+Open+FS+Menu+Edit+Snapshots+Exports+…)
        dvs = data.get("DashboardViewerSettings")
        if dvs in ("", None, 0, "0"):
            data["DashboardViewerSettings"] = 1023
        raw = json.dumps(data, ensure_ascii=False)
    resp = Response(raw, mimetype="application/json; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/", methods=["GET", "POST"])
@app.route("/designer", methods=["GET", "POST"])
def designer():
    designer = StiDesigner()
    wire_designer(designer)

    if sti_process(designer):
        return sti_response(designer)

    # query на первом открытии тоже кладём в store
    q = _public_query_params()
    if q:
        params_update(q)

    report = StiReport()
    report.loadFile("/mrt")
    designer.report = report
    return sti_response(designer)


@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    viewer = StiViewer()
    wire_viewer(viewer)

    if sti_process(viewer):
        # handler/ajax — без debug overlay
        return sti_response(viewer)

    q = _public_query_params()
    if q:
        params_update(q)

    report = StiReport()
    report.loadFile("/mrt")
    viewer.report = report
    return sti_response(viewer, inject_debug=True)


def main() -> None:
    print("Stimulsoft Dashboards.PYTHON 2024.2.2 — official Flask wiring", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    print(f"Designer: http://{HOST}:{PORT}/designer", flush=True)
    print(f"Viewer:   http://{HOST}:{PORT}/viewer", flush=True)
    print(f"Params:   POST/GET http://{HOST}:{PORT}/api/params", flush=True)
    print(f"Clicks:   GET http://{HOST}:{PORT}/api/interactions", flush=True)
    print(f"Filled:   POST/GET http://{HOST}:{PORT}/api/filled", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/viewer")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
