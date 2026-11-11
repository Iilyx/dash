#!/usr/bin/env python3
"""Stimulsoft Dashboards.PYTHON 2024.3.3 — официальные Flask samples.

Версия пакетов = ReportVersion / лицензия шаблона (2024.3.3), не latest.

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
from stimulsoft_reports import StiLicense, StiResult
from stimulsoft_reports.designer import StiDesigner
from stimulsoft_reports.events import StiDataEventArgs, StiReportEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer

# RequestParameters / RequestFromUser: без Submit холст белый.
# После loadFile отключаем ожидание и жмём Submit на parameters panel.
_VIEWER_LOAD_PATCH = "report.requestParameters = false;\n"
_VIEWER_AUTOSUBMIT_JS = """
window.viewer = viewer;
(function () {
  function clickSubmit() {
    try {
      var panel = viewer.jsObject && viewer.jsObject.controls
        && viewer.jsObject.controls.parametersPanel;
      var btn = panel && panel.mainButtons && panel.mainButtons.submit;
      if (!btn) return false;
      if (typeof btn.action === 'function') btn.action();
      else if (typeof btn.onclick === 'function') btn.onclick();
      else btn.click();
      return true;
    } catch (e) { return false; }
  }
  var n = 0;
  var t = setInterval(function () {
    if (clickSubmit() || ++n > 40) clearInterval(t);
  }, 250);
})();
""".replace("\n", "")

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


load_env()

# How to Activate the Product
_dash = os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_KEY", "").strip()
_reports = os.environ.get("STIMULSOFT_REPORTS_LICENSE_KEY", "").strip()
if _dash:
    StiLicense.setKey(_dash)
elif _reports:
    StiLicense.setKey(_reports)
elif os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_FILE", "").strip():
    StiLicense.setFile(str(path_of(os.environ["STIMULSOFT_DASHBOARDS_LICENSE_FILE"]).resolve()))
elif os.environ.get("STIMULSOFT_REPORTS_LICENSE_FILE", "").strip():
    StiLicense.setFile(str(path_of(os.environ["STIMULSOFT_REPORTS_LICENSE_FILE"]).resolve()))

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
    # Docs: change connectionString / parameters on the server side
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


def _patch_viewer_render(resp: Response) -> Response:
    """Сразу отрисовать дашборд: не ждать ручной Submit по RequestParameters."""
    html = resp.get_data(as_text=True)
    if "report.requestParameters" not in html:
        html = html.replace(
            "report.loadFile('/mrt');",
            "report.loadFile('/mrt');\n" + _VIEWER_LOAD_PATCH,
            1,
        )
        if "report.requestParameters" not in html:
            html = html.replace(
                'report.loadFile("/mrt");',
                'report.loadFile("/mrt");\n' + _VIEWER_LOAD_PATCH,
                1,
            )
    if "parametersPanel.mainButtons.submit" not in html:
        html = html.replace(
            "viewer.renderHtml('viewerContent');",
            "viewer.renderHtml('viewerContent');\n" + _VIEWER_AUTOSUBMIT_JS,
            1,
        )
        if "parametersPanel.mainButtons.submit" not in html:
            html = html.replace(
                'viewer.renderHtml("viewerContent");',
                'viewer.renderHtml("viewerContent");\n' + _VIEWER_AUTOSUBMIT_JS,
                1,
            )
    resp.set_data(html)
    return resp


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
    # Official: Editing a Dashboard Template in the Designer
    designer = StiDesigner()
    designer.options.appearance.fullScreenMode = True
    designer.onBeginProcessData += begin_process_data
    designer.onSaveReport += save_report
    designer.onSaveAsReport += save_report

    if designer.processRequest(request):
        return designer.getFrameworkResponse()

    report = StiReport()
    report.loadFile("/mrt")
    designer.report = report
    return designer.getFrameworkResponse()


@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    # Official: Showing a Dashboard in the Viewer
    # + patch: RequestParameters иначе белый холст до Submit
    viewer = StiViewer()
    viewer.options.appearance.fullScreenMode = True
    viewer.onBeginProcessData += begin_process_data

    if viewer.processRequest(request):
        return viewer.getFrameworkResponse()

    report = StiReport()
    report.loadFile("/mrt")
    viewer.report = report
    return _patch_viewer_render(viewer.getFrameworkResponse())


def main() -> None:
    print("Stimulsoft Dashboards.PYTHON 2024.3.3 — official Flask wiring", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    print(f"Designer: http://{HOST}:{PORT}/designer", flush=True)
    print(f"Viewer:   http://{HOST}:{PORT}/viewer", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/viewer")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
