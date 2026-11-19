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
import secrets
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import stimulsoft_dashboards  # noqa: F401 — подтягивает dashboards.js
from flask import Flask, Response, jsonify, request, session
from stimulsoft_data_adapters.enums.StiFrameworkType import StiFrameworkType
from stimulsoft_reports.classes.StiResult import StiResult
from stimulsoft_reports.designer import StiDesigner
from stimulsoft_reports.events import StiDataEventArgs, StiReportEventArgs, StiVariablesEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer

ROOT = Path(__file__).resolve().parent
_SAFE_NAME = re.compile(r"[^\w.\-]+", re.UNICODE)

# token → {VariableName: value}; не только Flask session (Swagger/curl часто без cookie)
RENDER_FILTER_STORE: dict[str, dict[str, object]] = {}
_RENDER_QUERY_SKIP = {
    "run_queries",
    "sql",
    "filters",
    "sti_event",
    "event",
    "rf",
    "no_redirect",
    "redirect",
    "open",
}


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
app.secret_key = (
    os.environ.get("FLASK_SECRET", "").strip()
    or os.environ.get("SECRET_KEY", "").strip()
    or "dash-render-filters-dev"
)


def begin_process_data(args: StiDataEventArgs) -> None:
    args.connectionString = PG_CONN


def _parse_filters_payload(raw) -> dict[str, object]:
    """Нормализовать тело/квери в {VariableName: value}."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            raw = json.loads(s)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    # {"filters": {...}} или сразу {Name: value}
    inner = raw.get("filters") if isinstance(raw.get("filters"), dict) else raw
    if not isinstance(inner, dict):
        return {}
    out: dict[str, object] = {}
    for key, value in inner.items():
        if key in {"mrt", "run_queries", "sql", "kind", "count"}:
            continue
        name = str(key).strip()
        if not name:
            continue
        out[name] = value
    return out


def _coerce_variable_value(type_name: str | None, value: object) -> object:
    """Подогнать value под Stimulsoft typeName (String, Int32, DateTime, StringList, …)."""
    if value is None:
        return None
    t = (type_name or "").lower()
    if t.endswith("list"):
        if isinstance(value, list):
            return list(value)
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return parsed
                except json.JSONDecodeError:
                    pass
            if "," in s:
                return [p.strip() for p in s.split(",") if p.strip()]
            return [s] if s else []
        return [value]
    if t.endswith("range"):
        if isinstance(value, dict):
            return {"from": value.get("from"), "to": value.get("to")}
        return value
    if "datetime" in t or t == "date":
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value.strip():
            s = value.strip().replace("Z", "").replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s[:19] if " " in s else s[:10], fmt)
                except ValueError:
                    continue
            try:
                return datetime.fromisoformat(value.strip().replace("Z", ""))
            except ValueError:
                return value
        return value
    if t in {"int", "int32", "int64", "uint", "uint32", "uint64", "byte", "sbyte", "short", "ushort"} or t.endswith("int32") or t.endswith("int64"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if t in {"decimal", "double", "float", "single"} or "decimal" in t or "double" in t:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if t in {"bool", "boolean"}:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return value


def _store_render_filters(filters: dict[str, object]) -> str:
    token = secrets.token_urlsafe(12)
    RENDER_FILTER_STORE[token] = dict(filters)
    # не раздувать память
    while len(RENDER_FILTER_STORE) > 64:
        RENDER_FILTER_STORE.pop(next(iter(RENDER_FILTER_STORE)), None)
    return token


def _load_render_filters(token: str | None) -> dict[str, object] | None:
    if not token:
        return None
    got = RENDER_FILTER_STORE.get(token)
    return dict(got) if isinstance(got, dict) else None


def make_prepare_variables(filters: dict[str, object]):
    """onPrepareVariables: подставить переданные фильтры перед рендером."""

    def prepare_variables(args: StiVariablesEventArgs) -> None:
        if not filters:
            print("prepareVariables skip: no filters", flush=True)
            return
        variables = args.variables
        if not variables:
            print(
                f"prepareVariables skip: empty variables from client; wanted={list(filters.keys())}",
                flush=True,
            )
            return
        by_lower = {str(name).lower(): name for name in variables.keys()}
        ordered = list(variables.items())
        applied = []
        missed = []
        for name, value in filters.items():
            key = by_lower.get(str(name).lower())
            # запасной путь: числовой индекс среди variables (обрезанный .mrt без Name)
            if key is None and str(name).isdigit():
                idx = int(str(name))
                if 0 <= idx < len(ordered):
                    key = ordered[idx][0]
            if key is None:
                missed.append(str(name))
                continue
            var = variables[key]
            var.value = _coerce_variable_value(getattr(var, "typeName", None), value)
            applied.append(f"{key}={var.value!r}")
        print(
            f"prepareVariables applied={applied} missed={missed} "
            f"available={list(variables.keys())}",
            flush=True,
        )

    return prepare_variables


def _mrt_variable_list(data: dict) -> list[tuple[str, dict]]:
    """[(key, variable_dict), ...] из Dictionary.Variables."""
    dictionary = data.get("Dictionary") if isinstance(data, dict) else None
    raw = dictionary.get("Variables") if isinstance(dictionary, dict) else None
    out: list[tuple[str, dict]] = []
    if isinstance(raw, dict):
        for key in sorted(raw.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)):
            var = raw[key]
            if isinstance(var, dict):
                out.append((str(key), var))
    elif isinstance(raw, list):
        for i, var in enumerate(raw):
            if isinstance(var, dict):
                out.append((str(i), var))
    return out


def patch_mrt_for_auto_render(raw: str, filters: dict[str, object] | None) -> str:
    """Только для /render: выключить RequestParameters и проставить Value фильтров.

    Обычный /viewer (/mrt) не трогаем — там остаётся панель + Submit.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        return raw
    data["RequestParameters"] = False
    if "ParametersDisplayMode" in data:
        data["ParametersDisplayMode"] = "Once"
    if not filters:
        return json.dumps(data, ensure_ascii=False)
    vars_list = _mrt_variable_list(data)
    by_name = {
        str(v.get("Name") or "").strip().lower(): v
        for _, v in vars_list
        if str(v.get("Name") or "").strip()
    }
    by_key = {k: v for k, v in vars_list}

    def _set_value(target: dict, value: object) -> None:
        if isinstance(value, bool):
            target["Value"] = "True" if value else "False"
        elif isinstance(value, (list, dict)):
            target["Value"] = json.dumps(value, ensure_ascii=False)
        else:
            target["Value"] = "" if value is None else str(value)

    applied = []
    missed: list[str] = []
    for name, value in filters.items():
        target = by_name.get(str(name).lower())
        if target is None and str(name) in by_key:
            target = by_key[str(name)]
        if target is None and str(name).isdigit():
            idx = int(str(name))
            if 0 <= idx < len(vars_list):
                target = vars_list[idx][1]
        if target is None:
            missed.append(str(name))
            continue
        _set_value(target, value)
        applied.append(str(target.get("Name") or name))
    print(
        f"mrt-render patched values={applied} missed={missed} RequestParameters=False",
        flush=True,
    )
    return json.dumps(data, ensure_ascii=False)


def inject_filter_values_js(
    html: str,
    filters: dict[str, object] | None,
    *,
    auto_render: bool = False,
) -> str:
    """Клиентский fallback: выставить variables; для /render — без панели Submit."""
    if not filters or not isinstance(html, str):
        return html
    payload = json.dumps(filters, ensure_ascii=False)
    auto_js = "true" if auto_render else "false"
    script = (
        "<script>(function(){\n"
        f"  window.__dashFilters = {payload};\n"
        f"  window.__dashAutoRender = {auto_js};\n"
        "  function dashApplyFilters(args){\n"
        "    var F = window.__dashFilters || {};\n"
        "    var vars = args && args.variables;\n"
        "    if (!vars) return;\n"
        "    var list = Array.isArray(vars) ? vars : null;\n"
        "    if (list) {\n"
        "      list.forEach(function(v, i){\n"
        "        var n = (v && v.name != null) ? String(v.name) : '';\n"
        "        var hit = null;\n"
        "        Object.keys(F).forEach(function(k){\n"
        "          if (hit != null) return;\n"
        "          if (String(k).toLowerCase() === n.toLowerCase()) hit = F[k];\n"
        "          else if (String(k) === String(i)) hit = F[k];\n"
        "        });\n"
        "        if (hit != null) v.value = hit;\n"
        "      });\n"
        "      return;\n"
        "    }\n"
        "    Object.keys(F).forEach(function(k){\n"
        "      if (vars[k] != null) { vars[k].value = F[k]; return; }\n"
        "      var lk = String(k).toLowerCase();\n"
        "      Object.keys(vars).forEach(function(name){\n"
        "        if (String(name).toLowerCase() === lk) vars[name].value = F[k];\n"
        "      });\n"
        "    });\n"
        "  }\n"
        "  window.dashApplyFilters = dashApplyFilters;\n"
        "})();</script>\n"
    )
    if "window.__dashFilters" in html:
        return html
    if "</head>" in html:
        return html.replace("</head>", script + "</head>", 1)
    if "</body>" in html:
        return html.replace("</body>", script + "</body>", 1)
    return html + script


def build_viewer(
    *,
    filters: dict[str, object] | None = None,
    filter_token: str | None = None,
    inject_screenshot: bool = True,
    auto_render: bool = False,
):
    """Собрать StiViewer.

    auto_render=True — только /render: без панели Submit, сразу дашборд с фильтрами.
    /viewer вызывает с auto_render=False.
    """
    viewer = StiViewer()
    apply_license(viewer)
    viewer.options.appearance.fullScreenMode = True
    viewer.onBeginProcessData += begin_process_data
    if auto_render:
        # не показывать Parameters / Submit — значения уже переданы
        viewer.options.toolbar.showParametersButton = False
    if filters:
        viewer.onPrepareVariables += make_prepare_variables(filters)
        # клиентский хук как дополнительный канал
        viewer.onPrepareVariables += "dashApplyFilters"
    # token в URL handler'а — PrepareVariables/BeginProcessData без cookie
    # StiHandler.url — property без setter, только __init__; пишем private field
    handler_path = "/open" if auto_render else "/viewer"
    if filter_token:
        handler_url = f"{handler_path}?rf={filter_token}"
        try:
            viewer.handler._StiBaseHandler__url = handler_url  # noqa: SLF001
        except Exception:
            pass

    # handler/GetResource — без inject: иначе JS ломается (в файле есть «StiViewer»)
    if sti_process(viewer):
        return sti_response(
            viewer,
            inject_screenshot=False,
            filter_token=filter_token,
            auto_render=auto_render,
            handler_path=handler_path,
        )

    report = StiReport()
    # /open → пропатченный .mrt без RequestParameters; /viewer → обычный /mrt
    if auto_render and filter_token:
        report.loadFile(f"/mrt-render?rf={filter_token}")
    else:
        report.loadFile("/mrt")
    viewer.report = report
    return sti_response(
        viewer,
        inject_screenshot=inject_screenshot,
        filters=filters,
        filter_token=filter_token,
        auto_render=auto_render,
        handler_path=handler_path,
    )


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


def inject_screenshot_ui(html: str, *, auto_render: bool = False) -> str:
    """Кнопка «Скрин» на странице viewer + window.__dashReport для bake карт."""
    needle = "viewer.report = report;\nviewer.renderHtml('viewerContent');"
    auto_block = ""
    if auto_render:
        # только /render: без диалога параметров, значения уже в .mrt / __dashFilters
        auto_block = (
            "try { report.requestParameters = false; } catch (e) {}\n"
            "try {\n"
            "  if (report.dictionary && report.dictionary.variables) {\n"
            "    var F = window.__dashFilters || {};\n"
            "    var vs = report.dictionary.variables;\n"
            "    var n = vs.count || 0;\n"
            "    for (var i = 0; i < n; i++) {\n"
            "      var v = vs.getByIndex ? vs.getByIndex(i) : (vs.list && vs.list[i]);\n"
            "      if (!v) continue;\n"
            "      var name = (v.name || '') + '';\n"
            "      if (F[name] != null) { v.valueObject = F[name]; continue; }\n"
            "      Object.keys(F).forEach(function(k){\n"
            "        if (String(k).toLowerCase() === name.toLowerCase()) v.valueObject = F[k];\n"
            "      });\n"
            "    }\n"
            "  }\n"
            "} catch (e) {}\n"
        )
    repl = (
        auto_block
        + "viewer.report = report;\n"
        "window.__dashViewer = viewer;\n"
        "window.__dashReport = report;\n"
        "viewer.renderHtml('viewerContent');"
    )
    if needle in html:
        html = html.replace(needle, repl, 1)
    try:
        ver = int((ROOT / "screenshot_dashboard.js").stat().st_mtime)
    except OSError:
        ver = 1
    tag = f'<script src="/screenshot-dashboard.js?v={ver}"></script>\n'
    if "screenshot-dashboard.js" not in html:
        if "</body>" in html:
            html = html.replace("</body>", tag + "</body>", 1)
        else:
            html = html + tag
    return html


def inject_handler_url(
    html: str,
    filter_token: str | None,
    *,
    handler_path: str = "/open",
) -> str:
    """Прописать Stimulsoft.handler.url с rf=token (на случай если private set не сработал)."""
    if not filter_token or not isinstance(html, str):
        return html
    url = f"{handler_path}?rf={filter_token}"
    script = (
        "<script>(function(){\n"
        f"  var u = {json.dumps(url)};\n"
        "  function apply(){\n"
        "    try {\n"
        "      if (window.Stimulsoft && Stimulsoft.handler) Stimulsoft.handler.url = u;\n"
        "    } catch (e) {}\n"
        "  }\n"
        "  apply();\n"
        "  document.addEventListener('DOMContentLoaded', apply);\n"
        "})();</script>\n"
    )
    if "window.__dashHandlerUrl" in html:
        return html
    script = script.replace(
        "var u = ",
        "window.__dashHandlerUrl = true; var u = ",
        1,
    )
    if "</head>" in html:
        return html.replace("</head>", script + "</head>", 1)
    if "</body>" in html:
        return html.replace("</body>", script + "</body>", 1)
    return html + script


def sti_response(
    component,
    inject_screenshot: bool = False,
    filters: dict[str, object] | None = None,
    filter_token: str | None = None,
    auto_render: bool = False,
    handler_path: str = "/open",
):
    component.handler.framework = StiFrameworkType.FLASK
    if not component.handler.origin:
        component.handler.origin = request.origin or "*"
    resp = component.getFrameworkResponse()
    if inject_screenshot and hasattr(resp, "get_data") and hasattr(resp, "set_data"):
        raw = resp.get_data(as_text=True)
        if isinstance(raw, str) and ("viewerContent" in raw or "StiViewer" in raw):
            # сначала __dashFilters, потом патч renderHtml (auto_render)
            html = inject_filter_values_js(raw, filters, auto_render=auto_render)
            html = inject_screenshot_ui(html, auto_render=auto_render)
            html = inject_handler_url(html, filter_token, handler_path=handler_path)
            resp.set_data(html)
            resp.mimetype = "text/html"
    return resp


def _safe_name(name: str, fallback: str = "dashboard.png", ext: str | None = None) -> str:
    raw = (name or "").strip() or fallback
    base = Path(raw).name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or fallback
    allowed = {".png", ".csv", ".json", ".txt", ".html", ".pdf"}
    suffix = Path(cleaned).suffix.lower()
    want = ("." + ext.lstrip(".")).lower() if ext else ""
    if want in allowed and not cleaned.lower().endswith(want):
        cleaned = Path(cleaned).stem + want
    elif suffix not in allowed:
        cleaned += want if want in allowed else ".png"
    return cleaned[:160]


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


def openapi_spec() -> dict:
    """OpenAPI 3 для Flask viewer — /docs."""
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Stimulsoft Viewer API",
            "version": "1.0.0",
            "description": (
                "`/viewer` — preview (панель + Submit). "
                "`/open` — передал фильтры → сразу готовый дашборд. "
                f"MRT: `{MRT_PATH.name}`"
            ),
        },
        "paths": {
            "/filters": {
                "get": {
                    "tags": ["filters"],
                    "summary": "Список фильтров из .mrt (+ SQL choices)",
                    "description": (
                        "Variables с Category «Фильтр(ы)». "
                        "`choices_from=items` — Keys/Values; "
                        "`choices_from=query` — SqlCommand DataSource (по умолчанию выполняется)."
                    ),
                    "parameters": [
                        {
                            "name": "run_queries",
                            "in": "query",
                            "schema": {
                                "type": "string",
                                "enum": ["0", "1", "true", "false"],
                                "default": "1",
                            },
                            "description": "1 — выполнить SQL dialog.source; 0 — только метаданные",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Фильтры",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/FiltersResponse"}
                                }
                            },
                        }
                    },
                }
            },
            "/open": {
                "get": {
                    "tags": ["open"],
                    "summary": "Открыть дашборд уже с фильтрами",
                    "description": "Сразу построенный дашборд, без Submit. /viewer не меняется.",
                    "parameters": [
                        {
                            "name": "filters",
                            "in": "query",
                            "schema": {"type": "string"},
                            "description": 'JSON: {"trendtype":"nocumulative","typenum":1000}',
                        },
                        {
                            "name": "trendtype",
                            "in": "query",
                            "schema": {"type": "string"},
                            "description": "Имена variables из GET /filters",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "HTML дашборда с фильтрами",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                },
                "post": {
                    "tags": ["open"],
                    "summary": "Передать фильтры и открыть дашборд",
                    "description": (
                        "Открывает браузер на /open?rf=TOKEN. "
                        "Accept: application/json → {ok,url,rf}. ?open=0 — без браузера."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RenderRequest"},
                                "examples": {
                                    "wrapped": {
                                        "summary": "через filters",
                                        "value": {
                                            "filters": {
                                                "trendtype": "nocumulative",
                                                "typenum": 1000,
                                            }
                                        },
                                    },
                                    "flat": {
                                        "summary": "плоский объект",
                                        "value": {
                                            "trendtype": "nocumulative",
                                            "typenum": 1000,
                                        },
                                    },
                                },
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "HTML-редирект или JSON {url}",
                            "content": {
                                "text/html": {"schema": {"type": "string"}},
                                "application/json": {"schema": {"type": "object"}},
                            },
                        }
                    },
                },
            },
            "/render": {
                "get": {
                    "tags": ["open"],
                    "summary": "Алиас /open",
                    "responses": {
                        "200": {
                            "description": "То же что /open",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                },
                "post": {
                    "tags": ["open"],
                    "summary": "Алиас /open",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RenderRequest"}
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "То же что /open",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                },
            },
            "/viewer": {
                "get": {
                    "tags": ["viewer"],
                    "summary": "Preview: панель + ручной Submit",
                    "responses": {
                        "200": {
                            "description": "HTML Stimulsoft Viewer без авто-фильтров",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/designer": {
                "get": {
                    "tags": ["viewer"],
                    "summary": "Stimulsoft Designer",
                    "responses": {
                        "200": {
                            "description": "HTML Designer",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/mrt": {
                "get": {
                    "tags": ["viewer"],
                    "summary": "Сырой .mrt (JSON)",
                    "responses": {
                        "200": {
                            "description": "MRT JSON",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
            "/api/screenshot": {
                "post": {
                    "tags": ["export"],
                    "summary": "Сохранить PNG/PDF/CSV в exports/",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ScreenshotRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Сохранено",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ScreenshotResponse"}
                                }
                            },
                        }
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "FiltersResponse": {
                    "type": "object",
                    "properties": {
                        "mrt": {"type": "string"},
                        "kind": {"type": "string", "example": "category_filters"},
                        "count": {"type": "integer"},
                        "run_queries": {"type": "boolean"},
                        "database": {"type": "boolean"},
                        "filters": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/FilterItem"},
                        },
                    },
                },
                "FilterItem": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "name": {"type": "string"},
                        "alias": {"type": "string"},
                        "category": {"type": "string", "example": "Фильтры"},
                        "type": {"type": "string"},
                        "choices_from": {
                            "type": "string",
                            "enum": ["items", "query", "none"],
                        },
                        "choices": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {},
                                    "value": {},
                                },
                            },
                        },
                        "sql": {"type": "string"},
                        "query_ok": {"type": "boolean"},
                        "query_error": {"type": "string"},
                    },
                },
                "RenderRequest": {
                    "type": "object",
                    "description": "Либо {filters:{...}}, либо плоский {Name: value}",
                    "additionalProperties": True,
                    "properties": {
                        "filters": {
                            "type": "object",
                            "additionalProperties": True,
                            "example": {"Region": "Брестская", "Year": 2024},
                        }
                    },
                },
                "ScreenshotRequest": {
                    "type": "object",
                    "required": ["data_b64"],
                    "properties": {
                        "name": {"type": "string"},
                        "ext": {"type": "string", "example": "png"},
                        "data_b64": {"type": "string", "format": "byte"},
                    },
                },
                "ScreenshotResponse": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "name": {"type": "string"},
                        "path": {"type": "string"},
                        "error": {"type": "string"},
                    },
                },
            }
        },
    }


@app.get("/openapi.json")
def openapi_json():
    resp = jsonify(openapi_spec())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/docs")
def swagger_docs():
    """Swagger UI для viewer API."""
    html = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <title>Stimulsoft Viewer API</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"/>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: "/openapi.json",
      dom_id: "#swagger-ui",
      deepLinking: true,
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
      layout: "BaseLayout"
    });
  </script>
</body>
</html>
"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.get("/screenshot-dashboard.js")
def screenshot_dashboard_js():
    path = ROOT / "screenshot_dashboard.js"
    resp = Response(path.read_text(encoding="utf-8"), mimetype="application/javascript; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/filters")
def filters():
    """Variables с Category «Фильтр(ы)»: items или SQL dialog.source (SQL считается по умолчанию)."""
    try:
        from filters_mrt import extract_filters

        # по умолчанию SQL выполняется; ?run_queries=0 — только метаданные
        rq = (request.args.get("run_queries") or request.args.get("sql") or "1").strip().lower()
        run_queries = rq not in {"0", "false", "no", "off"}
        plan = extract_filters(
            MRT_PATH,
            run_queries=run_queries,
            database_url=_db or None,
        )
    except Exception as exc:
        return jsonify(
            {
                "mrt": MRT_PATH.name,
                "kind": "category_filters",
                "error": str(exc),
                "count": 0,
                "filters": [],
            }
        ), 500
    resp = jsonify(plan)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/screenshot")
def api_screenshot():
    """Сохраняет PNG/CSV (и др.) из viewer в EXPORT_DIR."""
    body = request.get_json(silent=True) or {}
    b64 = body.get("data_b64")
    if not isinstance(b64, str) or not b64.strip():
        return jsonify({"ok": False, "error": "data_b64 required"}), 400
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"bad base64: {exc}"}), 400
    if not data:
        return jsonify({"ok": False, "error": "empty payload"}), 400
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = _safe_name(
        str(body.get("name") or ""),
        f"{MRT_PATH.stem}_{stamp}.png",
        str(body.get("ext") or "") or None,
    )
    out = EXPORT_DIR / name
    try:
        out.write_bytes(data)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    print(f"screenshot saved: {out.resolve()}", flush=True)
    return jsonify({"ok": True, "name": out.name, "path": str(out.resolve())})


@app.route("/mrt", methods=["GET", "POST"])
def serve_mrt():
    """Обычный шаблон для /viewer и /designer — панель параметров как в .mrt."""
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    resp = Response(raw, mimetype="application/json; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/mrt-render", methods=["GET", "POST"])
def serve_mrt_render():
    """Шаблон только для /open: RequestParameters=false + Value из фильтров."""
    token = (request.args.get("rf") or "").strip() or None
    filters = _load_render_filters(token) or session.get("render_filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    try:
        patched = patch_mrt_for_auto_render(raw, filters)
    except Exception as exc:
        print(f"mrt-render patch failed: {exc}", flush=True)
        patched = raw
    resp = Response(patched, mimetype="application/json; charset=utf-8")
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
    return build_viewer(filters=None, inject_screenshot=True)


def _is_stimulsoft_handler_request() -> bool:
    """POST от JS viewer (PrepareVariables / BeginProcessData / …), не наш JSON фильтров."""
    if request.args.get("sti_event") or request.args.get("event"):
        return True
    raw = request.get_data(cache=True, as_text=False) or b""
    if not raw:
        return False
    try:
        text = raw.decode("utf-8", errors="ignore").lstrip()
    except Exception:
        return False
    if not text.startswith("{") and not text.startswith("["):
        return False
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    # типичные поля handler'а Stimulsoft
    if obj.get("event") or obj.get("command") or obj.get("viewerId") or obj.get("data"):
        return True
    return False


def _collect_incoming_filters() -> dict[str, object]:
    """Собрать {name: value} из POST JSON / form или GET query."""
    incoming: dict[str, object] = {}
    is_handler = _is_stimulsoft_handler_request()
    if request.method == "POST" and not is_handler:
        body = request.get_json(silent=True)
        if body is None and request.data:
            try:
                body = json.loads(request.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = None
        incoming = _parse_filters_payload(body)
        if not incoming and request.form:
            incoming = {
                k: v
                for k, v in request.form.items()
                if k not in _RENDER_QUERY_SKIP
            }
            if request.form.get("filters"):
                incoming.update(_parse_filters_payload(request.form.get("filters")))
    elif request.method == "GET":
        if request.args.get("filters"):
            incoming = _parse_filters_payload(request.args.get("filters"))
        if not incoming:
            incoming = {
                k: v
                for k, v in request.args.items()
                if k not in _RENDER_QUERY_SKIP
            }
    return incoming


def open_dashboard_with_filters():
    """Подставить фильтры и открыть уже построенный дашборд (без Submit).

    Отдельно от /viewer (preview с ручным Submit).

    GET  /open?trendtype=nocumulative&typenum=1000
    GET  /open?filters={"trendtype":"nocumulative","typenum":1000}
    POST /open  JSON {"filters":{"trendtype":"nocumulative","typenum":1000}}
         → открывает браузер на /open?rf=TOKEN (или ?open=0 без браузера)
    """
    token = (request.args.get("rf") or "").strip() or None
    incoming = _collect_incoming_filters()
    is_handler = _is_stimulsoft_handler_request()

    if incoming:
        session["render_filters"] = incoming
        token = _store_render_filters(incoming)
        if request.method == "POST" and not is_handler:
            view_path = f"/open?rf={token}"
            view_url = f"http://{HOST}:{PORT}{view_path}"
            open_flag = (request.args.get("open") or "1").strip().lower()
            if open_flag not in {"0", "false", "no", "off"}:
                try:
                    webbrowser.open(view_url)
                    print(f"opened browser: {view_url}", flush=True)
                except Exception as exc:
                    print(f"browser open failed: {exc}", flush=True)
            # JSON удобен для Postman; HTML — для браузера
            wants_json = "application/json" in (request.headers.get("Accept") or "")
            if wants_json:
                return jsonify(
                    {
                        "ok": True,
                        "url": view_url,
                        "path": view_path,
                        "rf": token,
                        "filters": incoming,
                    }
                )
            html = (
                "<!doctype html><meta charset=utf-8>"
                f"<meta http-equiv=refresh content='0;url={view_path}'>"
                "<title>Opening dashboard…</title>"
                "<p>Открываю дашборд с фильтрами… "
                f"<a href='{view_path}'>{view_url}</a></p>"
            )
            return Response(html, mimetype="text/html; charset=utf-8")

    filters = _load_render_filters(token) or session.get("render_filters") or incoming or None
    if isinstance(filters, dict) and not filters:
        filters = None
    if filters and not token:
        token = _store_render_filters(filters)
        session["render_filters"] = filters

    print(f"open filters={filters} rf={token}", flush=True)
    return build_viewer(
        filters=filters,
        filter_token=token,
        inject_screenshot=True,
        auto_render=True,
    )


@app.route("/open", methods=["GET", "POST"])
def open_with_filters():
    """Новый endpoint: передал фильтры → открылся дашборд уже с ними (без Submit)."""
    return open_dashboard_with_filters()


@app.route("/render", methods=["GET", "POST"])
def render_with_filters():
    """Алиас /open (совместимость)."""
    return open_dashboard_with_filters()


def main() -> None:
    print("Stimulsoft Dashboards.PYTHON 2024.2.2 — official Flask wiring", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    print(f"Designer: http://{HOST}:{PORT}/designer", flush=True)
    print(f"Viewer:   http://{HOST}:{PORT}/viewer   (preview + Submit)", flush=True)
    print(f"Filters:  http://{HOST}:{PORT}/filters", flush=True)
    print(f"Open:     http://{HOST}:{PORT}/open     (filters → ready dashboard)", flush=True)
    print(f"Swagger:  http://{HOST}:{PORT}/docs", flush=True)
    print(f"Screens:  {EXPORT_DIR.resolve()}", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/viewer")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
