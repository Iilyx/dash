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
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import stimulsoft_dashboards  # noqa: F401 — подтягивает dashboards.js
from flask import Flask, Response, jsonify, request
from stimulsoft_data_adapters.enums.StiFrameworkType import StiFrameworkType
from stimulsoft_reports.classes.StiResult import StiResult
from stimulsoft_reports.designer import StiDesigner
from stimulsoft_reports.events import StiDataEventArgs, StiReportEventArgs
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


def begin_process_data(args: StiDataEventArgs) -> None:
    args.connectionString = PG_CONN


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


def sti_response(component):
    component.handler.framework = StiFrameworkType.FLASK
    if not component.handler.origin:
        component.handler.origin = request.origin or "*"
    return component.getFrameworkResponse()


def enable_viewer_exports(viewer: StiViewer) -> None:
    viewer.options.toolbar.showSaveButton = True
    ex = viewer.options.exports
    ex.showExportDialog = True
    ex.showExportToPdf = True
    ex.showExportToExcel = True


def inject_element_exports(html: str) -> str:
    """window.viewer + кнопка скачивания элементов с Save."""
    if "element_exports.js" in html:
        return html
    hook = (
        "viewer.renderHtml('viewerContent');\n"
        "window.viewer = viewer;\n"
    )
    if "viewer.renderHtml('viewerContent');" in html:
        html = html.replace("viewer.renderHtml('viewerContent');", hook, 1)
    tag = '<script src="/element_exports.js"></script>\n</body>'
    if "</body>" in html:
        return html.replace("</body>", tag, 1)
    return html + "\n" + tag


def write_export_bytes(filename: str, data: bytes) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    raw_stem = _SAFE_NAME.sub("_", Path(filename).stem).strip("._")[:160] or "export"
    ext = Path(filename).suffix.lstrip(".") or "bin"
    out = EXPORT_DIR / f"{raw_stem}.{ext}"
    out.write_bytes(data)
    return out


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/mrt", methods=["GET", "POST"])
def serve_mrt():
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    resp = Response(raw, mimetype="application/json; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/element_exports.js")
def serve_element_exports_js():
    path = ROOT / "element_exports.js"
    resp = Response(path.read_text(encoding="utf-8"), mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/export")
def api_export():
    """JS element download copy → exports/. Body: {filename, data: base64}."""
    body = request.get_json(silent=True) or {}
    filename = str(body.get("filename") or "").strip()
    raw_b64 = body.get("data")
    if not filename or not isinstance(raw_b64, str) or not raw_b64:
        return jsonify({"ok": False, "error": "filename + data(base64) required"}), 400
    try:
        data = base64.b64decode(raw_b64, validate=False)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"bad base64: {exc}"}), 400
    if not data:
        return jsonify({"ok": False, "error": "empty data"}), 400
    try:
        out = write_export_bytes(filename, data)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    print(f"element export saved: {out.resolve()} ({len(data)} bytes)", flush=True)
    return jsonify({"ok": True, "path": str(out.resolve()), "bytes": len(data)})


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
    viewer = StiViewer()
    apply_license(viewer)
    viewer.options.appearance.fullScreenMode = True
    enable_viewer_exports(viewer)
    viewer.onBeginProcessData += begin_process_data

    if sti_process(viewer):
        return sti_response(viewer)

    report = StiReport()
    report.loadFile("/mrt")
    viewer.report = report
    resp = sti_response(viewer)
    html = resp.get_data(as_text=True)
    resp.set_data(inject_element_exports(html))
    return resp


def main() -> None:
    print("Stimulsoft Dashboards.PYTHON 2024.2.2 — official Flask wiring", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    print(f"Exports: {EXPORT_DIR.resolve()}", flush=True)
    print(f"Designer: http://{HOST}:{PORT}/designer", flush=True)
    print(f"Viewer:   http://{HOST}:{PORT}/viewer", flush=True)
    print("Viewer: Скачать Save → PDF+Excel по элементам", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/viewer")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
