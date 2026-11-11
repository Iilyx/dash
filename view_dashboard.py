#!/usr/bin/env python3
"""Редактор дашбордов Stimulsoft Dashboards.PYTHON 2024.3.3.

По доке: StiLicense + StiDesigner + loadFile + getFrameworkResponse.

  .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import stimulsoft_dashboards  # noqa: F401
from flask import Flask, Response, request
from stimulsoft_reports import StiLicense, StiResult
from stimulsoft_reports.designer import StiDesigner
from stimulsoft_reports.events import StiDataEventArgs, StiReportEventArgs
from stimulsoft_reports.report import StiReport

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

# Лицензии (как в How to Activate). В JS один Key — Dashboards, иначе Reports.
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

# Postgres connection string для onBeginProcessData
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


def main() -> None:
    url = f"http://{HOST}:{PORT}/"
    print(f"Stimulsoft Dashboards.PYTHON 2024.3.3 designer", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    print(f"URL: {url}", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
