#!/usr/bin/env python3
"""Минимальный viewer: .mrt + Postgres connection.

Как в Samples-Dashboards.Python:
  StiViewer + loadFile(.mrt) + onBeginProcessData(connectionString).

Запуск:
  MRT_PATH=full.mrt .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import json
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


def numbered(node) -> list:
    if node is None:
        return []
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        if all(str(k).isdigit() for k in node):
            return [node[k] for k in sorted(node, key=lambda x: int(x))]
        return [node]
    return []


def mrt_summary(path: Path) -> dict:
    """Короткий диагноз шаблона (полный .mrt тоже ок)."""
    raw = path.read_text(encoding="utf-8-sig")
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"json: {exc}", "bytes": len(raw)}
    dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}
    sources = []
    for src in numbered(dictionary.get("DataSources")):
        if not isinstance(src, dict):
            continue
        sql = src.get("SqlCommand") or ""
        cols = src.get("Columns")
        col_count = len(cols) if isinstance(cols, (dict, list)) else 0
        sources.append(
            {
                "name": src.get("Name") or "",
                "ident": src.get("Ident") or "",
                "sql_len": len(sql) if isinstance(sql, str) else 0,
                "columns": col_count,
                "connect_on_start": src.get("ConnectOnStart"),
            }
        )
    databases = []
    for db in numbered(dictionary.get("Databases")):
        if isinstance(db, dict):
            databases.append(
                {
                    "name": db.get("Name") or "",
                    "ident": db.get("Ident") or "",
                    "has_conn": bool(db.get("ConnectionString") or db.get("ConnectionStringEncrypted")),
                }
            )
    rfu = 0
    for var in numbered(dictionary.get("Variables")):
        if isinstance(var, dict) and str(var.get("RequestFromUser")).lower() in {
            "true",
            "1",
        }:
            rfu += 1
    return {
        "ok": True,
        "bytes": len(raw),
        "request_parameters": report.get("RequestParameters"),
        "parameters_display_mode": report.get("ParametersDisplayMode"),
        "databases": databases,
        "sources": sources,
        "request_from_user_count": rfu,
    }


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
    """Подставить Postgres connection; SQL/параметры — как в .mrt.

    Sum/Switch/Round в ValueMeter.Expression считает JS после ответа адаптера.
    """
    src = getattr(args, "dataSource", None) or "?"
    cmd = getattr(args, "command", None) or "?"
    db = getattr(args, "database", None) or "?"
    q = getattr(args, "queryString", None) or ""
    print(
        f"[viewer] beginProcessData cmd={cmd} db={db} source={src} sql_len={len(q)}",
        flush=True,
    )
    if not q.strip() and str(cmd).lower() in {"executequery", "executesql", "select"}:
        print("[viewer] пустой SqlCommand — метры/Expression не на чем считать", flush=True)
    if PG_CONN:
        args.connectionString = PG_CONN


@app.get("/mrt")
def serve_mrt():
    """Отдать .mrt как есть (relative URL, как в сэмпле Stimulsoft)."""
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    # .mrt JSON; text/plain тоже ок, json удобнее в DevTools
    resp = Response(raw, mimetype="application/json; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/debug")
def debug():
    """Что в текущем MRT_PATH: sources/RequestParameters."""
    info = mrt_summary(MRT_PATH)
    info["mrt_path"] = str(MRT_PATH.resolve())
    info["pg_conn_set"] = bool(PG_CONN)
    if info.get("request_parameters") in (True, "true", "True", 1, "1"):
        info["hint"] = (
            "RequestParameters=true → dashboard SQL/метры ждут Submit на панели. "
            "До Submit часто виден только Label (N1), без числа."
        )
    return info


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
    # как в Samples: handler только на viewer
    sti.onBeginProcessData += begin_process_data

    if sti.processRequest(request):
        return sti.getFrameworkResponse()

    report = StiReport()
    # relative /mrt — без _external (localhost vs 127.0.0.1 ломал loadFile)
    report.loadFile(url_for("serve_mrt"))
    sti.report = report
    return sti.getFrameworkResponse()


def main() -> None:
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    summary = mrt_summary(MRT_PATH)
    print(f"MRT:  {MRT_PATH.resolve()} ({summary.get('bytes')} bytes)", flush=True)
    print(f"DB:   connection string from .env", flush=True)
    if summary.get("ok"):
        print(
            f"MRT:  RequestParameters={summary.get('request_parameters')} "
            f"RFU={summary.get('request_from_user_count')} "
            f"sources={len(summary.get('sources') or [])}",
            flush=True,
        )
        for src in summary.get("sources") or []:
            print(
                f"  source name={src.get('name')!r} ident={src.get('ident')!r} "
                f"sql_len={src.get('sql_len')} cols={src.get('columns')} "
                f"ConnectOnStart={src.get('connect_on_start')!r}",
                flush=True,
            )
        if summary.get("request_parameters") in (True, "true", "True", 1, "1"):
            print(
                "NOTE: RequestParameters=true — нажми Submit, иначе метры без данных (виден Label N1)",
                flush=True,
            )
        if all((s.get("sql_len") or 0) == 0 for s in (summary.get("sources") or [1])):
            print(
                "WARNING: у всех DataSources sql_len=0 — похоже на scrubbed/не тот MRT_PATH",
                flush=True,
            )
    else:
        print(f"MRT ERROR: {summary.get('error')}", flush=True)
    print(f"Open: {url}", flush=True)
    print(f"Debug: {url}debug", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=VIEWER_HOST, port=VIEWER_PORT, debug=False)


if __name__ == "__main__":
    main()
