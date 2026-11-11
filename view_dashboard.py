#!/usr/bin/env python3
"""Stimulsoft Dashboards.PYTHON 2024.3.3 — designer как в Samples-Dashboards.Python.

Официальный паттерн:
  StiDesigner + processRequest + StiReport.loadFile + getFrameworkResponse
  onBeginProcessData (Postgres) + onSaveReport

Запуск:
  # .env: MRT_PATH=... STIMULSOFT_LICENSE_KEY=... POSTGRES_*
  .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

# подтягивает stimulsoft.dashboards.js в HTML (без этого только Reports)
import stimulsoft_dashboards  # noqa: F401
from flask import Flask, Response, request
from stimulsoft_reports import StiLicense, StiResult
from stimulsoft_reports.designer import StiDesigner
from stimulsoft_reports.events import StiDataEventArgs, StiReportEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer
from stimulsoft_reports.viewer.enums import (
    StiParametersPanelPosition,
    StiToolbarDisplayMode,
)

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


def check_postgres(url: str) -> str:
    try:
        import psycopg
    except ImportError:
        return "psycopg not installed"
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"FAIL: {exc}"


def resolve_mrt_path() -> Path:
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        return path_of(sys.argv[1])
    env_path = os.environ.get("MRT_PATH", "").strip()
    if not env_path:
        raise SystemExit("Задай MRT_PATH в .env — путь к .mrt")
    return path_of(env_path)


def apply_license() -> str:
    """Как в How_to_Activate_the_Product: StiLicense.setKey / setFile.

    Приоритет: Ultimate → Dashboards → Reports (в JS один Base.StiLicense.Key).
    """
    candidates = [
        ("ultimate", "STIMULSOFT_LICENSE_KEY", "STIMULSOFT_LICENSE_FILE"),
        (
            "dashboards",
            "STIMULSOFT_DASHBOARDS_LICENSE_KEY",
            "STIMULSOFT_DASHBOARDS_LICENSE_FILE",
        ),
        (
            "reports",
            "STIMULSOFT_REPORTS_LICENSE_KEY",
            "STIMULSOFT_REPORTS_LICENSE_FILE",
        ),
    ]
    found: list[str] = []
    applied: str | None = None
    for kind, key_env, file_env in candidates:
        key = os.environ.get(key_env, "").strip()
        key_file = os.environ.get(file_env, "").strip()
        if key:
            found.append(f"{kind}:key({len(key)})")
            if applied is None:
                StiLicense.setKey(key)
                applied = f"{kind} key ({len(key)} chars)"
            continue
        if key_file:
            path = path_of(key_file)
            if path.is_file():
                found.append(f"{kind}:file({path.name})")
                if applied is None:
                    StiLicense.setFile(str(path.resolve()))
                    applied = f"{kind} file ({path.name})"
            else:
                found.append(f"{kind}:file missing ({path})")
    if applied is None:
        return "not set (trial)"
    if len(found) > 1:
        return f"{applied}; also {', '.join(found[1:])}"
    return applied


load_env()
LICENSE_STATUS = apply_license()

MRT_PATH = resolve_mrt_path()
if not MRT_PATH.is_file():
    raise SystemExit(f"Нет .mrt: {MRT_PATH} — задай MRT_PATH в .env")

VIEWER_HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "8040") or "8040")
OPEN_BROWSER = os.environ.get("VIEWER_OPEN", "1").lower() in {"1", "true", "yes"}
PARAMETERS_PANEL_COLUMNS = int(os.environ.get("PARAMETERS_PANEL_COLUMNS", "3") or "3")
PARAMETERS_PANEL_MAX_HEIGHT = int(
    os.environ.get("PARAMETERS_PANEL_MAX_HEIGHT", "220") or "220"
)

_DB_URL = database_url()
PG_CONN = stimulsoft_pg_connection_string(_DB_URL) if _DB_URL else ""
if not PG_CONN:
    raise SystemExit(
        "Нет DATABASE_URL / POSTGRES_USER+PASSWORD+DB в .env — "
        "не подставится connection string."
    )
PG_STATUS = check_postgres(_DB_URL)

app = Flask(__name__)


def begin_process_data(args: StiDataEventArgs) -> None:
    """Как в Using_Parameters_in_SQL_Query — подмена connection string."""
    if PG_CONN:
        args.connectionString = PG_CONN
    db = getattr(args, "database", None)
    src = getattr(args, "dataSource", None) or "?"
    sql = (getattr(args, "queryString", None) or "").replace("\n", " ").strip()
    if len(sql) > 180:
        sql = sql[:180] + "…"
    print(f"[data] BEGIN db={db} source={src} sql={sql or '<empty>'}", flush=True)


def end_process_data(args: StiDataEventArgs) -> None:
    result = getattr(args, "result", None)
    src = getattr(args, "dataSource", None) or "?"
    if result is None:
        print(f"[data] END source={src} result=<none>", flush=True)
        return
    ok = getattr(result, "success", None)
    notice = getattr(result, "notice", None) or ""
    rows = getattr(result, "count", None)
    if rows is None and getattr(result, "rows", None) is not None:
        rows = len(result.rows)
    print(f"[data] END source={src} ok={ok} rows={rows} notice={notice!r}", flush=True)


def save_report(args: StiReportEventArgs):
    """Как в Saving_a_Dashboard_Template_on_the_Server_Side."""
    if args.report is None:
        return StiResult.getError("пустой report — нечего сохранять")
    try:
        text = json.dumps(args.report, ensure_ascii=False, indent=2)
        MRT_PATH.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        return StiResult.getError(str(exc))
    return None


def make_report() -> StiReport:
    report = StiReport()
    # абсолютный /mrt — иначе relative loadFile с /designer даёт 404
    report.loadFile("/mrt")
    return report


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/mrt", methods=["GET", "POST"])
def serve_mrt():
    if not MRT_PATH.is_file():
        return Response('{"error":"mrt missing"}', status=404, mimetype="application/json")
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    resp = Response(raw, mimetype="application/json; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(404)
def not_found(_err):
    print(f"[404] {request.method} {request.path} args={dict(request.args)}", flush=True)
    if request.args.get("sti_event") or request.path.startswith("/mrt"):
        return Response("null", status=404, mimetype="application/json")
    return Response("not found", status=404)


@app.route("/", methods=["GET", "POST"])
@app.route("/designer", methods=["GET", "POST"])
def designer():
    """Editing_a_Dashboard_Template_in_the_Designer — дефолтный редактор."""
    designer = StiDesigner()
    designer.options.appearance.fullScreenMode = True

    designer.onBeginProcessData += begin_process_data
    designer.onEndProcessData += end_process_data
    designer.onSaveReport += save_report
    designer.onSaveAsReport += save_report

    if designer.processRequest(request):
        return designer.getFrameworkResponse()

    designer.report = make_report()
    return designer.getFrameworkResponse()


@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    """Showing_a_Dashboard_in_the_Viewer + Design → /designer."""
    viewer = StiViewer()
    viewer.options.appearance.fullScreenMode = True
    viewer.options.appearance.parametersPanelPosition = StiParametersPanelPosition.TOP
    viewer.options.appearance.parametersPanelColumnsCount = PARAMETERS_PANEL_COLUMNS
    if hasattr(viewer.options.appearance, "minParametersCountForMultiColumns"):
        viewer.options.appearance.minParametersCountForMultiColumns = 1
    viewer.options.appearance.parametersPanelMaxHeight = PARAMETERS_PANEL_MAX_HEIGHT
    viewer.options.toolbar.displayMode = StiToolbarDisplayMode.SEPARATED
    viewer.options.toolbar.showDesignButton = True

    viewer.onBeginProcessData += begin_process_data
    viewer.onEndProcessData += end_process_data
    viewer.onDesignReport += "window.location = '/designer';"

    if viewer.processRequest(request):
        return viewer.getFrameworkResponse()

    viewer.report = make_report()
    return viewer.getFrameworkResponse()


def main() -> None:
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    parsed = urlparse(_DB_URL)
    print(f"MRT:      {MRT_PATH.resolve()}", flush=True)
    print(f"License:  {LICENSE_STATUS}", flush=True)
    print(
        f"Postgres: {parsed.hostname}:{parsed.port or 5432}/"
        f"{(parsed.path or '/').lstrip('/')} → {PG_STATUS}",
        flush=True,
    )
    if PG_STATUS != "ok":
        print(
            "WARNING: БД недоступна — данные не подтянутся. "
            "docker compose up -d postgres",
            flush=True,
        )
    if LICENSE_STATUS.startswith("not set"):
        print(
            "WARNING: нет STIMULSOFT_LICENSE_KEY — trial может резать дашборд.",
            flush=True,
        )
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
