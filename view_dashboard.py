#!/usr/bin/env python3
"""Stimulsoft Dashboards.PYTHON 2024.2.2 — viewer/designer + snapshot + .mdc.

После фильтров на экране:
  data  → StiElementDataCache → POST/GET /api/snapshot
  mdc   → saveDocumentToJsonString → скачать .mdc + POST/GET /api/document
  open  → /document открывает последний .mdc без повторного SQL
  клики → onInteraction → POST/GET /api/interaction

Запуск:
  .venv/bin/pip install -r requirements.txt
  MRT_PATH=твой.mrt .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any
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

_lock = threading.Lock()
_SNAPSHOT: dict[str, Any] | None = None
_LAST_INTERACTION: dict[str, Any] | None = None
_DOCUMENT_JSON: str | None = None
_DOCUMENT_NAME = "dashboard.mdc"
SNAPSHOTS_DIR = ROOT / "snapshots"


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


def license_key() -> str:
    return (
        os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_KEY", "").strip()
        or os.environ.get("STIMULSOFT_REPORTS_LICENSE_KEY", "").strip()
        or os.environ.get("STIMULSOFT_LICENSE_KEY", "").strip()
    )


def license_file() -> str:
    raw = (
        os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_FILE", "").strip()
        or os.environ.get("STIMULSOFT_REPORTS_LICENSE_FILE", "").strip()
        or os.environ.get("STIMULSOFT_LICENSE_FILE", "").strip()
    )
    return str(path_of(raw).resolve()) if raw else ""


def apply_license(component) -> None:
    """В 2024.2.x лицензия вешается на instance: component.license.setKey/setFile."""
    key = license_key()
    file_path = license_file()
    if key:
        component.license.setKey(key)
    elif file_path:
        component.license.setFile(file_path)


def license_js_snippet() -> str:
    """JS для кастомных HTML-страниц (например /document)."""
    key = license_key()
    if key:
        return f"Stimulsoft.Base.StiLicense.Key = {json.dumps(key)};\n"
    file_path = license_file()
    if file_path:
        try:
            raw = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if raw:
            return f"Stimulsoft.Base.StiLicense.Key = {json.dumps(raw)};\n"
    return "console.warn('[dash] Stimulsoft license not set in .env');\n"


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


def sti_response(component, inject_snapshot: bool = False):
    component.handler.framework = StiFrameworkType.FLASK
    if not component.handler.origin:
        component.handler.origin = request.origin or "*"
    resp = component.getFrameworkResponse()
    if inject_snapshot and hasattr(resp, "get_data") and hasattr(resp, "set_data"):
        html = resp.get_data(as_text=True)
        if "</head>" in html or "<html" in html.lower():
            if "dash-snapshot.js" not in html:
                tag = '<script src="/dash-snapshot.js"></script>\n'
                if "</head>" in html:
                    html = html.replace("</head>", tag + "</head>", 1)
                else:
                    html = tag + html
            # Stimulsoft генерит `let viewer = ...` внутри start() — снаружи не видно.
            # Пробрасываем на window, чтобы snapshot читал уже отрендеренный report.
            html = html.replace(
                "let viewer = new Stimulsoft.Viewer.StiViewer",
                "let viewer = window.viewer = new Stimulsoft.Viewer.StiViewer",
                1,
            )
            if "viewer.renderHtml(" in html and "dashSnapshotHook" not in html:
                html = html.replace(
                    "viewer.renderHtml('viewerContent');",
                    "viewer.renderHtml('viewerContent');\n"
                    "window.viewer = viewer;\n"
                    "setTimeout(function(){ "
                    "if (window.dashCaptureSnapshot) "
                    "console.log('[dash] viewer ready, press snapshot'); "
                    "}, 1500);",
                    1,
                )
            resp.set_data(html)
    return resp


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/dash-snapshot.js")
def dash_snapshot_js():
    try:
        js = (ROOT / "dash_snapshot.js").read_text(encoding="utf-8")
    except OSError as exc:
        return Response(f"/* missing dash_snapshot.js: {exc} */", status=404)
    resp = Response(js, mimetype="application/javascript; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/snapshot")
def get_snapshot():
    with _lock:
        if _SNAPSHOT is None:
            return jsonify({"ok": False, "snapshot": None, "error": "no snapshot yet"})
        return jsonify({"ok": True, "snapshot": _SNAPSHOT})


@app.post("/api/snapshot")
def post_snapshot():
    global _SNAPSHOT
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    with _lock:
        _SNAPSHOT = body
    n = len(body.get("elements") or []) if isinstance(body.get("elements"), list) else 0
    skipped = body.get("skipped_count")
    print(f"[snapshot] elements={n} skipped={skipped} ok={body.get('ok')!r}", flush=True)
    return jsonify({"ok": True, "elements": n, "skipped": skipped})


@app.get("/api/interaction")
def get_interaction():
    with _lock:
        if _LAST_INTERACTION is None:
            return jsonify({"ok": False, "interaction": None, "error": "no interaction yet"})
        return jsonify({"ok": True, "interaction": _LAST_INTERACTION})


@app.post("/api/interaction")
def post_interaction():
    global _LAST_INTERACTION
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    with _lock:
        _LAST_INTERACTION = body
    print(f"[interaction] action={body.get('action')!r}", flush=True)
    return jsonify({"ok": True})


def _store_document(name: str, document: str) -> Path:
    global _DOCUMENT_JSON, _DOCUMENT_NAME
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (name or "dashboard.mdc"))
    lower = safe.lower()
    if not (lower.endswith(".mdc") or lower.endswith(".mrt") or lower.endswith(".mdz")):
        safe += ".mdc"
    path = SNAPSHOTS_DIR / safe
    path.write_text(document, encoding="utf-8")
    # всегда дублируем как last.mdc — /document.mdc читает его
    latest = SNAPSHOTS_DIR / "last.mdc"
    if path.resolve() != latest.resolve():
        latest.write_text(document, encoding="utf-8")
    with _lock:
        _DOCUMENT_JSON = document
        _DOCUMENT_NAME = safe
    return path


def _read_stored_document() -> tuple[str | None, str]:
    with _lock:
        doc = _DOCUMENT_JSON
        name = _DOCUMENT_NAME
    if doc:
        return doc, name
    latest = SNAPSHOTS_DIR / "last.mdc"
    if latest.is_file():
        return latest.read_text(encoding="utf-8"), "last.mdc"
    return None, name


@app.get("/api/document")
def get_document():
    doc, name = _read_stored_document()
    if not doc:
        return jsonify({"ok": False, "error": "no document yet — нажми mdc во viewer"}), 404
    # по умолчанию без тела (оно большое); ?include=1 — целиком в JSON
    if request.args.get("include") == "1" or request.args.get("download") == "1":
        if request.args.get("download") == "1":
            return Response(
                doc,
                mimetype="application/json; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{name}"'},
            )
        return jsonify({"ok": True, "filename": name, "chars": len(doc), "document": doc})
    return jsonify({"ok": True, "filename": name, "chars": len(doc), "url": "/document.mdc"})


@app.get("/document.mdc")
def document_mdc_file():
    """Сырой .mdc для Stimulsoft loadDocumentFile / скачивания."""
    doc, name = _read_stored_document()
    if not doc:
        return Response("no document", status=404, mimetype="text/plain")
    return Response(
        doc,
        mimetype="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/document")
def post_document():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    document = body.get("document")
    if not isinstance(document, str) or len(document) < 2:
        return jsonify({"ok": False, "error": "document string required"}), 400
    name = str(body.get("filename") or "dashboard.mdc")
    path = _store_document(name, document)
    print(
        f"[document] saved {path.name} method={body.get('method')!r} chars={len(document)}",
        flush=True,
    )
    return jsonify({"ok": True, "filename": path.name, "chars": len(document), "path": str(path)})


@app.get("/document")
def open_document_page():
    """Открыть последний .mdc во viewer без повторного SQL."""
    lic = license_js_snippet()
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Open document (.mdc)</title>
  <script src="/viewer?sti_event=GetResource&sti_data=stimulsoft.reports.js"></script>
  <script src="/viewer?sti_event=GetResource&sti_data=stimulsoft.dashboards.js"></script>
  <script src="/viewer?sti_event=GetResource&sti_data=stimulsoft.viewer.js"></script>
  <script src="/viewer?sti_event=GetResource&sti_data=stimulsoft.handler.js"></script>
  <style>
    #status {{
      position: fixed; left: 12px; top: 12px; z-index: 99999;
      background: #0f172a; color: #e2e8f0; border: 1px solid #334155;
      border-radius: 8px; padding: 8px 12px; font: 13px/1.4 ui-sans-serif, system-ui, sans-serif;
      max-width: min(520px, 90vw); white-space: pre-wrap;
    }}
  </style>
</head>
<body>
  <div id="status">загрузка…</div>
  <div id="viewerContent"></div>
  <script>
  {lic}
  function setStatus(t) {{
    var el = document.getElementById('status');
    if (el) el.textContent = t;
  }}
  async function start() {{
    try {{
      if (typeof Stimulsoft === 'undefined') {{
        setStatus('Stimulsoft JS не загрузился (проверь /viewer?sti_event=GetResource…)');
        return;
      }}
      setStatus('проверяю /api/document…');
      const metaResp = await fetch('/api/document');
      const meta = await metaResp.json();
      if (!metaResp.ok) {{
        setStatus('Нет .mdc.\\nОткрой /viewer → кнопка mdc.\\nПотом снова /document');
        return;
      }}
      setStatus('качаю /document.mdc (' + (meta.chars || '?') + ' chars)…');
      const rawResp = await fetch('/document.mdc', {{ cache: 'no-store' }});
      if (!rawResp.ok) {{
        setStatus('Не удалось скачать /document.mdc: HTTP ' + rawResp.status);
        return;
      }}
      const text = await rawResp.text();
      if (!text || text.length < 2) {{
        setStatus('Файл .mdc пустой. Пересохрани кнопкой mdc во viewer.');
        return;
      }}
      setStatus('открываю в viewer…');
      const options = new Stimulsoft.Viewer.StiViewerOptions();
      options.appearance.fullScreenMode = true;
      options.toolbar.showFullScreenButton = false;
      const viewer = window.viewer = new Stimulsoft.Viewer.StiViewer(options, 'viewerDoc', false);
      const report = new Stimulsoft.Report.StiReport();

      // loadDocument принимает string JSON / XML / object
      try {{
        report.loadDocument(text);
      }} catch (e1) {{
        try {{
          report.loadDocument(JSON.parse(text));
        }} catch (e2) {{
          // иногда saveDocument сохраняет template-like JSON — пробуем как обычный report
          try {{
            report.load(text);
          }} catch (e3) {{
            setStatus('Не открылось как document/report:\\n' + e1 + '\\n' + e2 + '\\n' + e3);
            console.error(e1, e2, e3);
            return;
          }}
        }}
      }}

      viewer.report = report;
      viewer.renderHtml('viewerContent');
      setStatus('открыто: ' + (meta.filename || 'document') + ' (' + text.length + ' chars). Можно закрыть это сообщение.');
      console.log('[document] opened', meta.filename, text.length, {{
        isDocument: report.isDocument,
        isRendered: report.isRendered,
        isDashboard: !!(report.isDashboard || (report.pages && report.pages.count))
      }});
    }} catch (e) {{
      setStatus('Ошибка: ' + e);
      console.error(e);
    }}
  }}
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
  </script>
</body>
</html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


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
    viewer = StiViewer()
    apply_license(viewer)
    viewer.options.appearance.fullScreenMode = True
    viewer.onBeginProcessData += begin_process_data
    # JS: window.dashCaptureInteraction(args) из dash_snapshot.js
    viewer.onInteraction += "dashCaptureInteraction"

    if sti_process(viewer):
        return sti_response(viewer)

    report = StiReport()
    report.loadFile("/mrt")
    viewer.report = report
    return sti_response(viewer, inject_snapshot=True)


def main() -> None:
    print("Stimulsoft Dashboards.PYTHON 2024.2.2 — viewer + snapshot", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    if license_key() or license_file():
        print("License: set", flush=True)
    else:
        print(
            "License: MISSING — задай STIMULSOFT_DASHBOARDS_LICENSE_KEY в .env",
            flush=True,
        )
    print(f"Designer:  http://{HOST}:{PORT}/designer", flush=True)
    print(f"Viewer:    http://{HOST}:{PORT}/viewer", flush=True)
    print(f"Data JSON: POST/GET http://{HOST}:{PORT}/api/snapshot", flush=True)
    print(f"Document:  POST/GET http://{HOST}:{PORT}/api/document  (кнопка mdc)", flush=True)
    print(f"Open .mdc: http://{HOST}:{PORT}/document", flush=True)
    print(f"Clicks:    POST/GET http://{HOST}:{PORT}/api/interaction", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/viewer")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
