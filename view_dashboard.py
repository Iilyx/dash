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

import json
import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import stimulsoft_dashboards  # noqa: F401 — подтягивает dashboards.js
from flask import Flask, Response, request
from stimulsoft_data_adapters.enums.StiFrameworkType import StiFrameworkType
from stimulsoft_reports.classes.StiResult import StiResult
from stimulsoft_reports.designer import StiDesigner
from stimulsoft_reports.events import StiDataEventArgs, StiReportEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer

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


# Кнопки поверх viewer: весь дашборд → PDF / Excel (exportDocumentAsync).
# Stimulsoft fullscreen держит z-index до 1000000 — наши кнопки выше.
_EXPORT_UI = """
<script>
(function () {
  function mount() {
    if (document.getElementById("sti-export-bar")) return;
    var style = document.createElement("style");
    style.textContent =
      "#sti-export-bar{position:fixed!important;z-index:2000001!important;top:12px!important;" +
      "right:12px!important;display:flex!important;gap:6px!important;" +
      "font:13px/1.2 system-ui,sans-serif!important;pointer-events:auto!important}" +
      "#sti-export-bar button{cursor:pointer;padding:8px 14px;border:1px solid #222;" +
      "background:#111;color:#fff;border-radius:4px;font:inherit}" +
      "#sti-export-bar button:hover{background:#333}" +
      "#sti-export-bar button:disabled{opacity:.5;cursor:wait}";
    document.head.appendChild(style);

    var bar = document.createElement("div");
    bar.id = "sti-export-bar";
    bar.innerHTML =
      '<button type="button" id="sti-export-pdf">PDF</button>' +
      '<button type="button" id="sti-export-xlsx">Excel</button>';
    document.body.appendChild(bar);

    function report() {
      if (window.viewer && window.viewer.report) return window.viewer.report;
      return null;
    }
    function exportDoc(fmt, ext, mime) {
      var r = report();
      if (!r) { alert("Дашборд ещё не загружен"); return; }
      var btns = bar.querySelectorAll("button");
      btns.forEach(function (b) { b.disabled = true; });
      r.exportDocumentAsync(function (data) {
        var name = (r.reportAlias || r.reportName || "dashboard") + ext;
        Stimulsoft.System.StiObject.saveAs(data, name, mime);
        btns.forEach(function (b) { b.disabled = false; });
      }, fmt);
    }
    document.getElementById("sti-export-pdf").onclick = function () {
      exportDoc(Stimulsoft.Report.StiExportFormat.Pdf, ".pdf", "application/pdf");
    };
    document.getElementById("sti-export-xlsx").onclick = function () {
      exportDoc(
        Stimulsoft.Report.StiExportFormat.Excel2007,
        ".xlsx",
        "application/vnd.ms-excel"
      );
    };
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { setTimeout(mount, 0); });
  } else {
    setTimeout(mount, 0);
  }
  window.addEventListener("load", function () { setTimeout(mount, 50); });
})();
</script>
"""


# Включает Save / View Data / Full Screen на всех dashboard-элементах
# и полный набор пунктов в меню Save (PDF, Excel, Image, Data, Document…).
_ELEMENT_SAVE_JS = """
<script>
window.stiEnableElementSaves = function (report) {
  if (!report) return;
  function enable(c) {
    var di = c && c.dashboardInteraction;
    if (!di) return;
    di.showSaveButton = true;
    if ("showViewDataButton" in di) di.showViewDataButton = true;
    if ("showFullScreenButton" in di) di.showFullScreenButton = true;
  }
  if (typeof report.getComponents === "function") {
    var comps = report.getComponents();
    for (var i = 0; i < comps.count; i++) enable(comps.getByIndex(i));
    return;
  }
  if (!report.pages) return;
  for (var p = 0; p < report.pages.count; p++) {
    var page = report.pages.getByIndex(p);
    if (!page || !page.components) continue;
    for (var c = 0; c < page.components.count; c++) enable(page.components.getByIndex(c));
  }
};
</script>
"""


def inject_export_ui(html: str) -> str:
    """window.viewer + кнопки PDF/Excel + Save на всех элементах до assign в viewer."""
    html = html.replace(
        "let viewer = new Stimulsoft.Viewer.StiViewer",
        "var viewer = window.viewer = new Stimulsoft.Viewer.StiViewer",
        1,
    )
    # Docs: после load, до assign в viewer. loadFile может догрузить async — ещё раз через timeout.
    html = html.replace(
        "viewer.report = report;",
        (
            "stiEnableElementSaves(report); viewer.report = report; "
            "setTimeout(function(){ stiEnableElementSaves(viewer.report||report); }, 0); "
            "setTimeout(function(){ stiEnableElementSaves(viewer.report||report); }, 800);"
        ),
        1,
    )
    inject = _ELEMENT_SAVE_JS + _EXPORT_UI
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx == -1:
        return html + inject
    return html[:idx] + inject + html[idx:]


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


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


def enable_full_exports(viewer: StiViewer) -> None:
    """Полное меню Save: диалог настроек + все форматы (элемент и весь дашборд)."""
    ex = viewer.options.exports
    viewer.options.toolbar.showSaveButton = True
    ex.showExportDialog = True
    ex.showExportToDocument = True
    ex.showExportToPdf = True
    ex.showExportToExcel = True
    ex.showExportToImageSvg = True
    ex.showExportToCsv = True
    ex.showExportToJson = True
    ex.showExportToHtml = True
    ex.showExportToHtml5 = True
    ex.showExportToWord = True
    ex.showExportToText = True
    ex.showExportToPowerPoint = True
    ex.showExportToOpenDocumentCalc = True
    ex.showExportToOpenDocumentWriter = True
    ex.showExportToXps = True


@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    viewer = StiViewer()
    apply_license(viewer)
    viewer.options.appearance.fullScreenMode = True
    enable_full_exports(viewer)
    viewer.onBeginProcessData += begin_process_data

    if sti_process(viewer):
        return sti_response(viewer)

    report = StiReport()
    report.loadFile("/mrt")
    viewer.report = report
    resp = sti_response(viewer)
    html = resp.get_data(as_text=True)
    if isinstance(html, str) and ("</body>" in html.lower() or "StiViewer" in html):
        resp.set_data(inject_export_ui(html))
    return resp


def main() -> None:
    print("Stimulsoft Dashboards.PYTHON 2024.2.2 — official Flask wiring", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    print(f"Designer: http://{HOST}:{PORT}/designer", flush=True)
    print(f"Viewer:   http://{HOST}:{PORT}/viewer", flush=True)
    lic = (
        os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_KEY", "").strip()
        or os.environ.get("STIMULSOFT_REPORTS_LICENSE_KEY", "").strip()
        or os.environ.get("STIMULSOFT_LICENSE_KEY", "").strip()
        or os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_FILE", "").strip()
        or os.environ.get("STIMULSOFT_REPORTS_LICENSE_FILE", "").strip()
        or os.environ.get("STIMULSOFT_LICENSE_FILE", "").strip()
    )
    if not lic:
        print(
            "WARN: нет Stimulsoft license — серый trial/DEMO watermark на элементах. "
            "Задай STIMULSOFT_DASHBOARDS_LICENSE_KEY в .env",
            flush=True,
        )
    if OPEN_BROWSER:
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/viewer")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
