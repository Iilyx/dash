#!/usr/bin/env python3
"""Stimulsoft Dashboards.PYTHON 2024.3.3 — как в документации.

  StiLicense.setKey(...)          # активация
  StiDesigner / StiViewer         # редактор и просмотр
  onBeginProcessData              # Postgres connection string
  onSaveReport                    # сохранение .mrt

Ключи в .env:
  STIMULSOFT_DASHBOARDS_LICENSE_KEY=...
  STIMULSOFT_REPORTS_LICENSE_KEY=...

Запуск:
  .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import stimulsoft_dashboards  # noqa: F401 — подключает dashboards.js
from flask import Flask, Response, request
from stimulsoft_reports import StiLicense, StiResult
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


def apply_license() -> str:
    """По доке How to Activate: StiLicense.setKey / setFile.

    В JS один Stimulsoft.Base.StiLicense.Key — ставим Dashboards, иначе Reports.
    Оба ключа можно держать в .env.
    """
    dash = os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_KEY", "").strip()
    reports = os.environ.get("STIMULSOFT_REPORTS_LICENSE_KEY", "").strip()
    dash_file = os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_FILE", "").strip()
    reports_file = os.environ.get("STIMULSOFT_REPORTS_LICENSE_FILE", "").strip()

    parts: list[str] = []
    if dash:
        parts.append(f"dashboards key ({len(dash)} chars)")
    elif dash_file:
        p = path_of(dash_file)
        parts.append(f"dashboards file ({p.name})" if p.is_file() else f"dashboards file missing: {p}")

    if reports:
        parts.append(f"reports key ({len(reports)} chars)")
    elif reports_file:
        p = path_of(reports_file)
        parts.append(f"reports file ({p.name})" if p.is_file() else f"reports file missing: {p}")

    # Dashboards.PYTHON нужен ключ Dashboards (или Ultimate в этом поле)
    if dash:
        StiLicense.setKey(dash)
    elif dash_file and path_of(dash_file).is_file():
        StiLicense.setFile(str(path_of(dash_file).resolve()))
    elif reports:
        StiLicense.setKey(reports)
    elif reports_file and path_of(reports_file).is_file():
        StiLicense.setFile(str(path_of(reports_file).resolve()))
    else:
        return "not set (trial)"

    return "; ".join(parts) if parts else "ok"


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
    return (
        f"Host={parsed.hostname}; Port={parsed.port or 5432}; "
        f"Database={parsed.path.lstrip('/').split('?', 1)[0]}; "
        f"User Id={parsed.username or ''}; Password={parsed.password or ''};"
    )


load_env()
LICENSE_STATUS = apply_license()

mrt_env = os.environ.get("MRT_PATH", "").strip()
if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
    MRT_PATH = path_of(sys.argv[1])
elif mrt_env:
    MRT_PATH = path_of(mrt_env)
else:
    raise SystemExit("Задай MRT_PATH в .env")
if not MRT_PATH.is_file():
    raise SystemExit(f"Нет .mrt: {MRT_PATH}")

VIEWER_HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "8040") or "8040")
OPEN_BROWSER = os.environ.get("VIEWER_OPEN", "1").lower() in {"1", "true", "yes"}

_DB_URL = database_url()
PG_CONN = stimulsoft_pg_connection_string(_DB_URL) if _DB_URL else ""
if not PG_CONN:
    raise SystemExit("Нет DATABASE_URL / POSTGRES_USER+PASSWORD+DB в .env")

app = Flask(__name__)


def begin_process_data(args: StiDataEventArgs) -> None:
    if PG_CONN:
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
    # Editing a Dashboard Template in the Designer
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
    # Showing a Dashboard in the Viewer
    viewer = StiViewer()
    viewer.options.appearance.fullScreenMode = True
    viewer.options.toolbar.showDesignButton = True
    viewer.onBeginProcessData += begin_process_data
    viewer.onDesignReport += "window.location = '/designer';"

    if viewer.processRequest(request):
        return viewer.getFrameworkResponse()

    report = StiReport()
    report.loadFile("/mrt")
    viewer.report = report
    return viewer.getFrameworkResponse()


def main() -> None:
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    print(f"MRT:      {MRT_PATH.resolve()}", flush=True)
    print(f"License:  {LICENSE_STATUS}", flush=True)
    print(f"Designer: {url}", flush=True)
    print(f"Viewer:   {url}viewer", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=VIEWER_HOST, port=VIEWER_PORT, debug=False)


if __name__ == "__main__":
    main()
