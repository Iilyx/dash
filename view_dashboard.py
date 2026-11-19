#!/usr/bin/env python3
"""Stimulsoft Dashboards.PYTHON 2024.2.2 — по официальным sample Flask.

Docs / samples:
  Editing a Dashboard Template in the Designer
  Showing a Dashboard in the Viewer
  How to Activate the Product
  Using Parameters in SQL Query (onBeginProcessData)

Запуск:
  .venv/bin/pip install -r requirements.txt
  MRT_PATH=твой.mrt .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import stimulsoft_dashboards  # noqa: F401 — подтягивает dashboards.js
from flask import Flask, Response, jsonify, request, session
from stimulsoft_data_adapters.enums.StiFrameworkType import StiFrameworkType
from stimulsoft_reports.classes.StiResult import StiResult
from stimulsoft_reports.designer import StiDesigner
from stimulsoft_reports.events import StiDataEventArgs, StiReportEventArgs, StiVariablesEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer

ROOT = Path(__file__).resolve().parent
_SAFE_NAME = re.compile(r"[^\w.\-]+", re.UNICODE)


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
EXPORT_DIR = path_of(os.environ.get("EXPORT_DIR", "exports").strip() or "exports")

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
app.secret_key = (
    os.environ.get("FLASK_SECRET", "").strip()
    or os.environ.get("SECRET_KEY", "").strip()
    or "dash-render-filters-dev"
)


def begin_process_data(args: StiDataEventArgs) -> None:
    args.connectionString = PG_CONN


def _parse_filters_payload(raw) -> dict[str, object]:
    """Нормализовать тело/квери в {VariableName: value}."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            raw = json.loads(s)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    # {"filters": {...}} или сразу {Name: value}
    inner = raw.get("filters") if isinstance(raw.get("filters"), dict) else raw
    if not isinstance(inner, dict):
        return {}
    out: dict[str, object] = {}
    for key, value in inner.items():
        if key in {"mrt", "run_queries", "sql", "kind", "count"}:
            continue
        name = str(key).strip()
        if not name:
            continue
        out[name] = value
    return out


def _coerce_variable_value(type_name: str | None, value: object) -> object:
    """Подогнать value под Stimulsoft typeName (String, Int32, DateTime, StringList, …)."""
    if value is None:
        return None
    t = (type_name or "").lower()
    if t.endswith("list"):
        if isinstance(value, list):
            return list(value)
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    pass
            if "," in s:
                return [p.strip() for p in s.split(",") if p.strip()]
            return [s] if s else []
        return [value]
    if t.endswith("range"):
        if isinstance(value, dict):
            return {"from": value.get("from"), "to": value.get("to")}
        return value
    if "datetime" in t or t == "date":
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value.strip():
            s = value.strip().replace("Z", "").replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s[:19] if " " in s else s[:10], fmt)
                except ValueError:
                    continue
            try:
                return datetime.fromisoformat(value.strip().replace("Z", ""))
            except ValueError:
                return value
        return value
    if t in {"int", "int32", "int64", "uint", "uint32", "uint64", "byte", "sbyte", "short", "ushort"} or t.endswith("int32") or t.endswith("int64"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if t in {"decimal", "double", "float", "single"} or "decimal" in t or "double" in t:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if t in {"bool", "boolean"}:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return value


def make_prepare_variables(filters: dict[str, object]):
    """onPrepareVariables: подставить переданные фильтры перед рендером."""

    def prepare_variables(args: StiVariablesEventArgs) -> None:
        if not filters or not args.variables:
            return
        by_lower = {str(name).lower(): name for name in args.variables.keys()}
        applied = []
        for name, value in filters.items():
            key = by_lower.get(str(name).lower())
            if not key:
                continue
            var = args.variables[key]
            var.value = _coerce_variable_value(getattr(var, "typeName", None), value)
            applied.append(key)
        print(f"prepareVariables applied={applied}", flush=True)

    return prepare_variables


def build_viewer(*, filters: dict[str, object] | None = None, inject_screenshot: bool = True):
    """Собрать StiViewer (+ опционально фильтры через onPrepareVariables)."""
    viewer = StiViewer()
    apply_license(viewer)
    viewer.options.appearance.fullScreenMode = True
    viewer.onBeginProcessData += begin_process_data
    if filters:
        viewer.onPrepareVariables += make_prepare_variables(filters)

    # handler/GetResource — без inject: иначе JS ломается (в файле есть «StiViewer»)
    if sti_process(viewer):
        return sti_response(viewer, inject_screenshot=False)

    report = StiReport()
    report.loadFile("/mrt")
    viewer.report = report
    return sti_response(viewer, inject_screenshot=inject_screenshot)


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
    return handled


def inject_screenshot_ui(html: str) -> str:
    """Кнопка «Скрин» на странице viewer + window.__dashReport для bake карт."""
    needle = "viewer.report = report;\nviewer.renderHtml('viewerContent');"
    repl = (
        "viewer.report = report;\n"
        "window.__dashViewer = viewer;\n"
        "window.__dashReport = report;\n"
        "viewer.renderHtml('viewerContent');"
    )
    if needle in html:
        html = html.replace(needle, repl, 1)
    try:
        ver = int((ROOT / "screenshot_dashboard.js").stat().st_mtime)
    except OSError:
        ver = 1
    tag = f'<script src="/screenshot-dashboard.js?v={ver}"></script>\n'
    if "screenshot-dashboard.js" not in html:
        if "</body>" in html:
            html = html.replace("</body>", tag + "</body>", 1)
        else:
            html = html + tag
    return html


def sti_response(component, inject_screenshot: bool = False):
    component.handler.framework = StiFrameworkType.FLASK
    if not component.handler.origin:
        component.handler.origin = request.origin or "*"
    resp = component.getFrameworkResponse()
    if inject_screenshot and hasattr(resp, "get_data") and hasattr(resp, "set_data"):
        raw = resp.get_data(as_text=True)
        if isinstance(raw, str) and ("viewerContent" in raw or "StiViewer" in raw):
            resp.set_data(inject_screenshot_ui(raw))
            resp.mimetype = "text/html"
    return resp


def _safe_name(name: str, fallback: str = "dashboard.png", ext: str | None = None) -> str:
    raw = (name or "").strip() or fallback
    base = Path(raw).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or fallback
    allowed = {".png", ".csv", ".json", ".txt", ".html", ".pdf"}
    suffix = Path(cleaned).suffix.lower()
    want = ("." + ext.lstrip(".")).lower() if ext else ""
    if want in allowed and not cleaned.lower().endswith(want):
        cleaned = Path(cleaned).stem + want
    elif suffix not in allowed:
        cleaned += want if want in allowed else ".png"
    return cleaned[:160]


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/screenshot-dashboard.js")
def screenshot_dashboard_js():
    path = ROOT / "screenshot_dashboard.js"
    resp = Response(path.read_text(encoding="utf-8"), mimetype="application/javascript; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/filters")
def filters():
    """Variables с Category «Фильтр(ы)»: items или SQL dialog.source (SQL считается по умолчанию)."""
    try:
        from filters_mrt import extract_filters

        # по умолчанию SQL выполняется; ?run_queries=0 — только метаданные
        rq = (request.args.get("run_queries") or request.args.get("sql") or "1").strip().lower()
        run_queries = rq not in {"0", "false", "no", "off"}
        plan = extract_filters(
            MRT_PATH,
            run_queries=run_queries,
            database_url=_db or None,
        )
    except Exception as exc:
        return jsonify(
            {
                "mrt": MRT_PATH.name,
                "kind": "category_filters",
                "error": str(exc),
                "count": 0,
                "filters": [],
            }
        ), 500
    resp = jsonify(plan)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/screenshot")
def api_screenshot():
    """Сохраняет PNG/CSV (и др.) из viewer в EXPORT_DIR."""
    body = request.get_json(silent=True) or {}
    b64 = body.get("data_b64")
    if not isinstance(b64, str) or not b64.strip():
        return jsonify({"ok": False, "error": "data_b64 required"}), 400
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"bad base64: {exc}"}), 400
    if not data:
        return jsonify({"ok": False, "error": "empty payload"}), 400
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = _safe_name(
        str(body.get("name") or ""),
        f"{MRT_PATH.stem}_{stamp}.png",
        str(body.get("ext") or "") or None,
    )
    out = EXPORT_DIR / name
    try:
        out.write_bytes(data)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    print(f"screenshot saved: {out.resolve()}", flush=True)
    return jsonify({"ok": True, "name": out.name, "path": str(out.resolve())})


@app.route("/mrt", methods=["GET", "POST"])
def serve_mrt():
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    resp = Response(raw, mimetype="application/json; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/", methods=["GET", "POST"])
@app.route("/designer", methods=["GET", "POST"])
def designer():
    designer = StiDesigner()
    apply_license(designer)
    designer.options.appearance.fullScreenMode = True
    designer.onBeginProcessData += begin_process_data
    designer.onSaveReport += save_report
    designer.onSaveAsReport += save_report

    if sti_process(designer):
        return sti_response(designer)

    report = StiReport()
    report.loadFile("/mrt")
    designer.report = report
    return sti_response(designer)


@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    return build_viewer(filters=None, inject_screenshot=True)


def _is_stimulsoft_handler_request() -> bool:
    """POST от JS viewer (PrepareVariables / BeginProcessData / …), не наш JSON фильтров."""
    if request.args.get("sti_event") or request.args.get("event"):
        return True
    raw = request.get_data(cache=True, as_text=False) or b""
    if not raw:
        return False
    try:
        text = raw.decode("utf-8", errors="ignore").lstrip()
    except Exception:
        return False
    if not text.startswith("{") and not text.startswith("["):
        return False
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    # типичные поля handler'а Stimulsoft
    if obj.get("event") or obj.get("command") or obj.get("viewerId") or obj.get("data"):
        return True
    return False


@app.route("/render", methods=["GET", "POST"])
def render_with_filters():
    """Принять значения фильтров (Category «Фильтры») и отрендерить viewer.

    POST JSON:
      {"filters": {"Region": "Брестская", "Year": 2024}}
      или {"Region": "Брестская", "Year": 2024}

    GET:
      /render?Region=Брестская&Year=2024
      /render?filters={"Region":"Брестская"}

    Значения кладутся в session — последующие POST viewer'а снова их подставят.
    """
    incoming: dict[str, object] = {}
    if request.method == "POST" and not _is_stimulsoft_handler_request():
        body = request.get_json(silent=True)
        if body is None and request.data:
            try:
                body = json.loads(request.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = None
        incoming = _parse_filters_payload(body)
        if not incoming and request.form:
            incoming = {k: v for k, v in request.form.items() if k != "filters"}
            if request.form.get("filters"):
                incoming.update(_parse_filters_payload(request.form.get("filters")))
    elif request.method == "GET":
        if request.args.get("filters"):
            incoming = _parse_filters_payload(request.args.get("filters"))
        if not incoming:
            incoming = {
                k: v
                for k, v in request.args.items()
                if k not in {"run_queries", "sql", "filters", "sti_event", "event"}
            }

    if incoming:
        session["render_filters"] = incoming

    filters = session.get("render_filters") or incoming or None
    if isinstance(filters, dict) and not filters:
        filters = None
    return build_viewer(filters=filters, inject_screenshot=True)


def main() -> None:
    print("Stimulsoft Dashboards.PYTHON 2024.2.2 — official Flask wiring", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    print(f"Designer: http://{HOST}:{PORT}/designer", flush=True)
    print(f"Viewer:   http://{HOST}:{PORT}/viewer", flush=True)
    print(f"Filters:  http://{HOST}:{PORT}/filters", flush=True)
    print(f"Render:   http://{HOST}:{PORT}/render  (POST filters → viewer)", flush=True)
    print(f"Screens:  {EXPORT_DIR.resolve()}", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/viewer")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
