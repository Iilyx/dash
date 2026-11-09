#!/usr/bin/env python3
"""Stimulsoft dashboard viewer: .mrt + Postgres.

Фильтры: дефолты из схемы .mrt (Variable.Value, иначе DataFilter Value2)
→ подстановка в SqlCommand (@Param и {Name}) → sql_trace.json.

Запуск:
  MRT_PATH=report.mrt .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import webbrowser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import stimulsoft_dashboards  # noqa: F401 — тянет stimulsoft.dashboards.js
from flask import Flask, Response, jsonify, request, url_for
from stimulsoft_reports.events import StiDataEventArgs, StiVariablesEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer
from stimulsoft_reports.viewer.enums import StiParametersPanelPosition

from extract_mrt import catalog_from_report, flag_true, load_mrt, numbered

ROOT = Path(__file__).resolve().parent
SQL_LOG: deque[dict] = deque(maxlen=80)
# дефолты фильтров из .mrt + выбранные на панели
FILTER_VALUES: dict[str, object] = {}
FILTER_TYPES: dict[str, str] = {}
SOURCE_SQL: dict[str, str] = {}

BRACE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
QUOTED_BRACE = re.compile(r"(?P<q>['\"])\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}(?P=q)")
AT_PARAM = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?\d+[.,]\d+$")


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
    return (
        f"Host={parsed.hostname}; Port={parsed.port or 5432}; "
        f"Database={parsed.path.lstrip('/').split('?', 1)[0]}; "
        f"User Id={parsed.username or ''}; Password={parsed.password or ''};"
    )


load_env()

MRT_PATH = path_of(
    sys.argv[1]
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
    else (os.environ.get("MRT_PATH", "").strip() or "PatientHealthKPI.mrt")
)
if not MRT_PATH.is_file():
    raise SystemExit(f"Нет .mrt: {MRT_PATH} — задай MRT_PATH в .env")

VIEWER_HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "5050") or "5050")
OPEN_BROWSER = os.environ.get("VIEWER_OPEN", "1").lower() in {"1", "true", "yes"}
PG_CONN = stimulsoft_pg_connection_string(database_url()) if database_url() else ""
SQL_LOG_PATH = path_of(os.environ.get("SQL_LOG_PATH", "sql_trace.json").strip() or "sql_trace.json")

app = Flask(__name__)


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() in {"", "{}", "[]", "null", "NULL", "None"}:
        return True
    if isinstance(value, (list, tuple, dict, set)) and len(value) == 0:
        return True
    return False


def _type_kind(typ: str | None) -> str:
    t = (typ or "").lower()
    if "bool" in t:
        return "bool"
    if any(x in t for x in ("int", "long", "short", "byte")):
        return "int"
    if any(x in t for x in ("float", "double", "decimal", "numeric", "money", "single")):
        return "float"
    if "date" in t or "time" in t:
        return "datetime"
    return "string"


def _sql_literal(value: object, typ: str | None = None) -> str:
    if _is_empty(value):
        return "NULL"
    kind = _type_kind(typ)
    if isinstance(value, bool) or kind == "bool":
        text = str(value).strip().lower()
        return "TRUE" if text in {"1", "true", "yes", "y"} else "FALSE"
    if isinstance(value, (list, tuple)):
        parts = [_sql_literal(v, typ) for v in value if not _is_empty(v)]
        return ", ".join(parts) if parts else "NULL"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value).strip()
    if kind in {"int", "float"} or INT_RE.fullmatch(text) or FLOAT_RE.fullmatch(text):
        if FLOAT_RE.fullmatch(text):
            return text.replace(",", ".")
        if INT_RE.fullmatch(text):
            return text
        try:
            float(text.replace(",", "."))
            return text.replace(",", ".")
        except ValueError:
            return "NULL"
    return "'" + text.replace("'", "''") + "'"


def _fill_sql(sql: str, values: dict[str, object], types: dict[str, str]) -> str:
    """SqlCommand: @Param и {Name} → литералы из values."""
    if not sql:
        return ""
    by_key = {str(k).lower(): v for k, v in values.items()}
    types_l = {str(k).lower(): v for k, v in types.items()}

    def repl_at(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if key not in by_key:
            return match.group(0)
        return _sql_literal(by_key[key], types_l.get(key))

    def repl_quoted(match: re.Match[str]) -> str:
        key = match.group("name").lower()
        if key not in by_key:
            return match.group(0)
        lit = _sql_literal(by_key[key], types_l.get(key))
        if lit == "NULL":
            return "NULL"
        # уже в кавычках в шаблоне — только внутренность
        if lit.startswith("'") and lit.endswith("'"):
            return f"{match.group('q')}{lit[1:-1]}{match.group('q')}"
        return lit

    def repl_brace(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if key not in by_key:
            return match.group(0)
        return _sql_literal(by_key[key], types_l.get(key))

    out = AT_PARAM.sub(repl_at, sql)
    out = QUOTED_BRACE.sub(repl_quoted, out)
    return BRACE.sub(repl_brace, out)


def _collect_value2_defaults(report: dict) -> dict[str, object]:
    """DataFilters Value='{Name}' + Value2 → дефолт Name, если Variable.Value пуст."""
    out: dict[str, object] = {}
    pages = report.get("Pages") if isinstance(report.get("Pages"), dict) else {}
    for page in numbered(pages):
        if not isinstance(page, dict):
            continue
        for comp in numbered(page.get("Components")):
            if not isinstance(comp, dict):
                continue
            stack = [comp]
            while stack:
                node = stack.pop()
                if not isinstance(node, dict):
                    continue
                for child in numbered(node.get("Components")):
                    stack.append(child)
                filters = node.get("DataFilters")
                for rule in numbered(filters if isinstance(filters, dict) else {}):
                    if not isinstance(rule, dict):
                        continue
                    val = str(rule.get("Value") or "").strip()
                    v2 = rule.get("Value2")
                    m = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", val)
                    if not m or _is_empty(v2):
                        continue
                    name = m.group(1)
                    out.setdefault(name, v2)
    return out


def load_schema_defaults(report: dict | None = None) -> tuple[dict[str, object], dict[str, str], dict[str, str]]:
    """Variable.Value (+ Value2) и SqlCommand из схемы .mrt."""
    global FILTER_VALUES, FILTER_TYPES, SOURCE_SQL
    if report is None:
        report = load_mrt(MRT_PATH)
    catalog = catalog_from_report(report)
    types: dict[str, str] = {}
    values: dict[str, object] = {}
    for var in catalog.get("variables") or []:
        if not isinstance(var, dict) or not var.get("name"):
            continue
        name = str(var["name"])
        types[name] = str(var.get("type") or "")
        if not _is_empty(var.get("value")):
            values[name] = var.get("value")
    for name, v2 in _collect_value2_defaults(report).items():
        values.setdefault(name, v2)
        types.setdefault(name, "System.String")
    templates: dict[str, str] = {}
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict):
            continue
        name = (source.get("name") or "").strip()
        sql = str(source.get("sql") or "").strip()
        if name and sql:
            templates[name] = sql
    FILTER_VALUES = dict(values)
    FILTER_TYPES = dict(types)
    SOURCE_SQL = templates
    print(
        f"[viewer] schema defaults={values} sql_sources={list(templates)}",
        flush=True,
    )
    return values, types, templates


def patch_mrt(raw: str) -> str:
    """Минимальный патч: панель параметров + ConnectOnStart для SQL."""
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    report["RequestParameters"] = False
    report["ParametersDisplayMode"] = "Panel"
    dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}
    for source in numbered(dictionary.get("DataSources")):
        if not isinstance(source, dict):
            continue
        if source.get("SqlCommand") or source.get("Query") or source.get("SqlQuery"):
            source["ConnectOnStart"] = True
    for variable in numbered(dictionary.get("Variables")):
        if not isinstance(variable, dict):
            continue
        name = variable.get("Name")
        if not name:
            continue
        if name in FILTER_VALUES and _is_empty(variable.get("Value")):
            variable["Value"] = str(FILTER_VALUES[name])
        if flag_true(variable.get("RequestFromUser")):
            variable["ShowOnParametersPanel"] = True
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def _lookup_sql(data_source: str | None) -> str:
    if not data_source:
        return ""
    if data_source in SOURCE_SQL:
        return SOURCE_SQL[data_source]
    lower = data_source.lower()
    for name, sql in SOURCE_SQL.items():
        if name.lower() == lower:
            return sql
    return ""


def _log(entry: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    SQL_LOG.appendleft(entry)
    phase = entry.get("phase")
    if phase == "parameters":
        print(f"[viewer] params {entry.get('params')}", flush=True)
    elif phase == "sql":
        print(
            f"[viewer] SQL {entry.get('source')} "
            f"{(entry.get('sql_substituted') or '')[:120]!r}",
            flush=True,
        )
    elif phase == "result":
        if entry.get("error"):
            print(f"[viewer] result {entry.get('source')} ERROR {entry.get('error')}", flush=True)
        else:
            print(f"[viewer] result {entry.get('source')} rows={entry.get('rows')}", flush=True)
    if phase not in {"parameters", "sql", "result"}:
        return
    try:
        SQL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SQL_LOG_PATH.open("a", encoding="utf-8") as fh:
            meta = {k: v for k, v in entry.items() if not str(k).startswith("sql_")}
            fh.write(json.dumps(meta, ensure_ascii=False, default=str, indent=2))
            fh.write("\n")
            for key in ("sql_template", "sql_from_client", "sql_substituted"):
                val = entry.get(key)
                if not val:
                    continue
                fh.write(f"--- {key} ---\n{val}\n--- end {key} ---\n")
            fh.write("\n")
    except Exception as exc:
        print(f"[viewer] sql log write failed: {exc}", flush=True)


def prepare_variables(args: StiVariablesEventArgs) -> None:
    """Пустые переменные → дефолт из схемы; выбранные значения запомнить."""
    global FILTER_VALUES
    selected = dict(FILTER_VALUES)
    for name, var in (args.variables or {}).items():
        key = str(name)
        if not _is_empty(var.value):
            selected[key] = var.value
            continue
        if key in FILTER_VALUES and not _is_empty(FILTER_VALUES[key]):
            var.value = FILTER_VALUES[key]
            selected[key] = var.value
    FILTER_VALUES = selected
    _log({"phase": "parameters", "params": selected})


def begin_process_data(args: StiDataEventArgs) -> None:
    """SqlCommand из .mrt + фильтры (выбранные / дефолты схемы)."""
    src = args.dataSource or "?"
    if PG_CONN:
        args.connectionString = PG_CONN

    template = _lookup_sql(src)
    from_client = args.queryString or ""
    base = template if template else from_client

    # параметры от Stimulsoft поверх дефолтов
    values = dict(FILTER_VALUES)
    for name, param in (args.parameters or {}).items():
        if not _is_empty(getattr(param, "value", None)):
            values[str(name)] = param.value

    filled = _fill_sql(base, values, FILTER_TYPES) if base else ""
    args.queryString = filled
    args.parameters = {}

    cmd = getattr(args.command, "value", None) or str(args.command or "")
    if cmd not in {"ExecuteQuery", "Execute", "GetData"} and not from_client and not template:
        return
    _log(
        {
            "phase": "sql",
            "source": src,
            "command": cmd,
            "params": values,
            "sql_template": template,
            "sql_from_client": from_client,
            "sql_substituted": filled,
        }
    )


def end_process_data(args: StiDataEventArgs) -> None:
    src = args.dataSource or "?"
    result = args.result
    entry: dict = {"phase": "result", "source": src}
    if result is None:
        entry["rows"] = None
    elif getattr(result, "success", True) is False:
        entry["error"] = getattr(result, "notice", None) or str(result)
    else:
        rows = getattr(result, "rows", None)
        count = getattr(result, "count", None)
        entry["rows"] = count if count is not None else (len(rows) if rows is not None else 0)
    _log(entry)


@app.get("/mrt")
def serve_mrt():
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    report = load_mrt(MRT_PATH)
    load_schema_defaults(report)
    patched = patch_mrt(raw)
    return Response(patched, mimetype="application/json", headers={"Cache-Control": "no-store"})


@app.get("/debug")
def debug():
    return jsonify(
        {
            "mrt": str(MRT_PATH.resolve()),
            "defaults": FILTER_VALUES,
            "types": FILTER_TYPES,
            "sql_sources": list(SOURCE_SQL),
            "sql_log_path": str(SQL_LOG_PATH.resolve()),
            "sql_log": list(SQL_LOG)[:20],
            "pg": bool(PG_CONN),
        }
    )


_OPENED_JS = """
(function () {
  var r = args.report;
  if (!r) return;
  r.requestParameters = false;
  try { r.parametersDisplayMode = Stimulsoft.Report.StiParametersDisplayMode.Panel; } catch (e) {}
  try { r.dashboardViewerSettings = 671; } catch (e2) {}
})();
""".strip()


@app.route("/", methods=["GET", "POST"])
@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    sti = StiViewer()
    sti.id = "viewer"
    sti.options.appearance.fullScreenMode = True
    sti.options.appearance.parametersPanelPosition = StiParametersPanelPosition.TOP
    sti.options.appearance.parametersPanelColumnsCount = 3
    sti.options.toolbar.visible = True
    sti.options.toolbar.showParametersButton = True
    sti.options.toolbar.showRefreshButton = True
    sti.onPrepareVariables += prepare_variables
    sti.onBeginProcessData += begin_process_data
    sti.onEndProcessData += end_process_data
    sti.onOpenedReport += _OPENED_JS

    if sti.processRequest(request):
        return sti.getFrameworkResponse()

    report = StiReport()
    report.onBeforeRender += _OPENED_JS
    report.onBeginProcessData += begin_process_data
    report.onEndProcessData += end_process_data
    report.onPrepareVariables += prepare_variables
    report.loadFile(url_for("serve_mrt", _external=True))
    sti.report = report
    return sti.getFrameworkResponse()


def main() -> None:
    load_schema_defaults()
    SQL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SQL_LOG_PATH.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "phase": "session",
                    "mrt": str(MRT_PATH.resolve()),
                    "defaults": FILTER_VALUES,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        fh.write("\n")
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    print(f"MRT:  {MRT_PATH.resolve()}")
    print(f"log:  {SQL_LOG_PATH.resolve()}")
    print(f"Open: {url}")
    if OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=VIEWER_HOST, port=VIEWER_PORT, debug=False)


if __name__ == "__main__":
    main()
