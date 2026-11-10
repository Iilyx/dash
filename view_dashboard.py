#!/usr/bin/env python3
"""Минимальный viewer: .mrt + Postgres connection.

Как в Samples-Dashboards.Python:
  StiViewer + loadFile(.mrt) + onBeginProcessData(connectionString).

Запуск:
  MRT_PATH=full.mrt .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

# Нужен пакет dashboards — иначе в HTML не подтянется stimulsoft.dashboards.js
import stimulsoft_dashboards  # noqa: F401
from flask import Flask, Response, request, url_for
from stimulsoft_reports.events import StiDataEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer
from stimulsoft_reports.viewer.enums import StiParametersPanelPosition

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
    env_path = os.environ.get("MRT_PATH", "").strip()
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        return path_of(sys.argv[1])
    if env_path:
        return path_of(env_path)
    return path_of("test.mrt")


load_env()

MRT_PATH = resolve_mrt_path()
if not MRT_PATH.is_file():
    raise SystemExit(f"Нет .mrt: {MRT_PATH} — задай MRT_PATH в .env")

VIEWER_HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "8040") or "8040")
OPEN_BROWSER = os.environ.get("VIEWER_OPEN", "1").lower() in {"1", "true", "yes"}
# панель фильтров: иначе дефолт Stimulsoft (min=5) кладёт поля построчно
PARAMETERS_PANEL_COLUMNS = int(os.environ.get("PARAMETERS_PANEL_COLUMNS", "3") or "3")
PARAMETERS_PANEL_MAX_HEIGHT = int(
    os.environ.get("PARAMETERS_PANEL_MAX_HEIGHT", "220") or "220"
)

PG_CONN = stimulsoft_pg_connection_string(database_url()) if database_url() else ""
if not PG_CONN:
    raise SystemExit(
        "Нет DATABASE_URL / POSTGRES_USER+PASSWORD+DB в .env — "
        "viewer не подставит connection string."
    )

app = Flask(__name__)


def begin_process_data(args: StiDataEventArgs) -> None:
    """Подставить Postgres connection; SQL/параметры — как в .mrt."""
    if PG_CONN:
        args.connectionString = PG_CONN


@app.get("/mrt")
def serve_mrt():
    """Отдать .mrt как есть."""
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    resp = Response(raw, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/", methods=["GET", "POST"])
@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    sti = StiViewer()
    sti.id = "viewer"
    sti.options.appearance.fullScreenMode = True
    sti.options.appearance.parametersPanelPosition = StiParametersPanelPosition.TOP
    sti.options.appearance.parametersPanelColumnsCount = PARAMETERS_PANEL_COLUMNS
    sti.options.appearance.minParametersCountForMultiColumns = 1
    sti.options.appearance.parametersPanelMaxHeight = PARAMETERS_PANEL_MAX_HEIGHT
    sti.onBeginProcessData += begin_process_data

    if sti.processRequest(request):
        return sti.getFrameworkResponse()

    report = StiReport()
    report.onBeginProcessData += begin_process_data
    report.loadFile(url_for("serve_mrt", _external=True))
    sti.report = report
    return sti.getFrameworkResponse()


def main() -> None:
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    print(f"MRT:  {MRT_PATH.resolve()}")
    print(f"DB:   Host=…; Database=… (from .env)")
    print(f"Open: {url}")
    if OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=VIEWER_HOST, port=VIEWER_PORT, debug=False)


if __name__ == "__main__":
    main()
