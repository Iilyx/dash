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
import threading
import time
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
from stimulsoft_reports.viewer.enums.StiParametersPanelPosition import (
    StiParametersPanelPosition,
)
from stimulsoft_reports.viewer.options.StiAppearanceOptions import StiAppearanceOptions

# JS default=5: при t<=5 колонок всегда 1 → фильтры столбиком. Патчим класс,
# чтобы getHtml() сериализовал override.
if not hasattr(StiAppearanceOptions, "minParametersCountForMultiColumns"):
    StiAppearanceOptions.minParametersCountForMultiColumns = 5

ROOT = Path(__file__).resolve().parent
_SAFE_NAME = re.compile(r"[^\w.\-]+", re.UNICODE)

# token → {VariableName: value}; legacy ?rf= (можно не использовать)
RENDER_FILTER_STORE: dict[str, dict[str, object]] = {}
# последние фильтры /open — без rf в URL (curl POST → браузер на /open)
LATEST_RENDER_FILTERS: dict[str, object] = {}
# GET /shot → сигнал уже открытому /open (без перезагрузки дашборда)
_SHOT_LOCK = threading.Lock()
_SHOT_RESULTS: dict[str, dict[str, object]] = {}
_RENDER_QUERY_SKIP = {
    "run_queries",
    "sql",
    "filters",
    "sti_event",
    "sti_data",
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


def apply_filter_panel_ui(viewer_options) -> None:
    """Фильтры в строку сверху; Submit/Reset → Применить/Сбросить (ru)."""
    viewer_options.localization = "ru"
    appearance = viewer_options.appearance
    appearance.parametersPanelPosition = StiParametersPanelPosition.TOP
    # иначе mobile JS форсит Left + 1 колонку
    appearance.allowMobileMode = False
    # Stimulsoft: columns = 1 если count <= minParametersCountForMultiColumns (дефолт 5)
    appearance.minParametersCountForMultiColumns = 1
    appearance.parametersPanelColumnsCount = 8


def filter_param_width() -> int:
    """Ширина контролов панели фильтров, px. FILTER_PARAM_WIDTH в .env."""
    raw = (os.environ.get("FILTER_PARAM_WIDTH") or "180").strip() or "180"
    try:
        width = int(raw)
    except ValueError:
        width = 180
    return max(80, min(width, 600))


def inject_filter_panel_layout(html: str) -> str:
    """Шире контролы; кнопки Сброс/Применить в той же строке, что фильтры."""
    if "dashFilterPanelLayout" in html:
        return html
    w = filter_param_width()
    block = (
        "<style id=\"dashFilterPanelLayout\">\n"
        ".stiJsViewerParametersPanel .stiJsViewerTextBoxDefault input,\n"
        ".stiJsViewerParametersPanel input.stiJsViewerTextBox,\n"
        ".stiJsViewerParametersPanel .stiJsViewerTextBox {\n"
        f"  min-width: {w}px !important;\n"
        f"  width: {w}px !important;\n"
        "}\n"
        ".stiJsViewerParametersPanel [id$=\"MainButtons\"] {\n"
        "  margin-top: 0 !important;\n"
        "  white-space: nowrap;\n"
        "}\n"
        "</style>\n"
        "<script>(function(){\n"
        f"  var PARAM_W = {w};\n"
        "  function widen(js){\n"
        "    try { js.options.currentParameterWidth = PARAM_W; } catch (e) {}\n"
        "    try {\n"
        "      var r = (window.__dashReport || (window.__dashViewer && window.__dashViewer.report)\n"
        "               || (window.viewer && window.viewer.report));\n"
        "      if (r) r.parameterWidth = PARAM_W;\n"
        "    } catch (e2) {}\n"
        "    try {\n"
        "      var panel = js.controls && js.controls.parametersPanel;\n"
        "      if (!panel || !panel.container) return;\n"
        "      var inputs = panel.container.querySelectorAll('input');\n"
        "      for (var i = 0; i < inputs.length; i++) {\n"
        "        inputs[i].style.width = PARAM_W + 'px';\n"
        "        inputs[i].style.minWidth = PARAM_W + 'px';\n"
        "      }\n"
        "    } catch (e3) {}\n"
        "  }\n"
        "  function sameRow(panel){\n"
        "    var btn = panel && panel.mainButtons;\n"
        "    if (!btn) return false;\n"
        "    var table = panel.container && panel.container.querySelector('table');\n"
        "    if (!table || !table.rows || !table.rows.length) return false;\n"
        "    var row0 = table.rows[0];\n"
        "    var target = null;\n"
        "    for (var g = 0; g + 1 < row0.cells.length; g += 2) {\n"
        "      var nameCell = row0.cells[g];\n"
        "      var ctrlCell = row0.cells[g + 1];\n"
        "      var hasName = !!(nameCell && String(nameCell.innerText || '').trim());\n"
        "      var hasCtrl = !!(ctrlCell && ctrlCell.querySelector(\n"
        "        '.stiJsViewerTextBoxDefault, table, input, select'));\n"
        "      if (!hasName && !hasCtrl) { target = ctrlCell; break; }\n"
        "    }\n"
        "    if (!target) {\n"
        "      var nc = row0.insertCell(-1);\n"
        "      nc.style.padding = '0 10px 0 30px';\n"
        "      target = row0.insertCell(-1);\n"
        "      target.style.padding = '0';\n"
        "    } else {\n"
        "      try { target.previousElementSibling.style.padding = '0 10px 0 30px'; } catch (e0) {}\n"
        "    }\n"
        "    target.style.verticalAlign = 'bottom';\n"
        "    if (btn.parentNode !== target) target.appendChild(btn);\n"
        "    btn.style.marginTop = '0';\n"
        "    for (var r = table.rows.length - 1; r >= 1; r--) {\n"
        "      var row = table.rows[r];\n"
        "      var empty = true;\n"
        "      for (var c = 0; c < row.cells.length; c++) {\n"
        "        if (row.cells[c].childNodes.length) { empty = false; break; }\n"
        "      }\n"
        "      if (empty) table.deleteRow(r);\n"
        "    }\n"
        "    return true;\n"
        "  }\n"
        "  function fix(){\n"
        "    var viewer = window.__dashViewer || window.viewer;\n"
        "    var js = viewer && viewer.jsObject;\n"
        "    if (!js) return false;\n"
        "    widen(js);\n"
        "    var panel = js.controls && js.controls.parametersPanel;\n"
        "    if (!panel) return false;\n"
        "    try {\n"
        "      if (js.collections && js.collections.loc) js.collections.loc.Reset = 'Сбросить';\n"
        "      if (panel.mainButtons && panel.mainButtons.reset) {\n"
        "        var b = panel.mainButtons.reset;\n"
        "        if (b.caption) b.caption.innerText = 'Сбросить';\n"
        "        else if (b.innerText) b.innerText = 'Сбросить';\n"
        "      }\n"
        "    } catch (e4) {}\n"
        "    return sameRow(panel);\n"
        "  }\n"
        "  var n = 0;\n"
        "  var t = setInterval(function(){\n"
        "    n++;\n"
        "    var ok = fix();\n"
        "    if (ok || n > 80) {\n"
        "      clearInterval(t);\n"
        "      if (!ok) return;\n"
        "      try {\n"
        "        var viewer = window.__dashViewer || window.viewer;\n"
        "        var panel = viewer && viewer.jsObject && viewer.jsObject.controls\n"
        "                   && viewer.jsObject.controls.parametersPanel;\n"
        "        var root = panel && panel.container;\n"
        "        if (!root || root.__dashLayoutObs) return;\n"
        "        root.__dashLayoutObs = new MutationObserver(function(){\n"
        "          if (root.__dashFixing) return;\n"
        "          root.__dashFixing = true;\n"
        "          try { fix(); } finally { root.__dashFixing = false; }\n"
        "        });\n"
        "        root.__dashLayoutObs.observe(root, { childList: true });\n"
        "      } catch (e5) {}\n"
        "    }\n"
        "  }, 200);\n"
        "  setTimeout(fix, 0);\n"
        "})();</script>\n"
    )
    if "</head>" in html:
        return html.replace("</head>", block + "</head>", 1)
    if "</body>" in html:
        return html.replace("</body>", block + "</body>", 1)
    return html + block


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
OPEN_BROWSER = os.environ.get("VIEWER_OPEN", "0").lower() in {"1", "true", "yes"}
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


def _remember_filters(filters: dict[str, object]) -> None:
    """Session + last-wins (для /open без ?rf=)."""
    data = dict(filters)
    session["render_filters"] = data
    LATEST_RENDER_FILTERS.clear()
    LATEST_RENDER_FILTERS.update(data)


def _load_render_filters(token: str | None) -> dict[str, object] | None:
    if not token:
        return None
    got = RENDER_FILTER_STORE.get(token)
    return dict(got) if isinstance(got, dict) else None


def _resolve_filters(
    token: str | None = None,
    incoming: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """rf (legacy) → session → LATEST → incoming."""
    for cand in (
        _load_render_filters(token),
        session.get("render_filters"),
        LATEST_RENDER_FILTERS,
        incoming,
    ):
        if isinstance(cand, dict) and cand:
            return dict(cand)
    return None


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
        # name + alias → real variable key
        index = _mrt_var_index()
        by_lower = {str(name).lower(): name for name in variables.keys()}
        ordered = list(variables.items())
        applied = []
        missed = []
        for name, value in filters.items():
            key = by_lower.get(str(name).lower())
            if key is None:
                meta = index.get(str(name).lower())
                if meta and meta.get("name"):
                    key = by_lower.get(str(meta["name"]).lower())
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


def _mrt_var_index(mrt_path: Path | None = None) -> dict[str, dict]:
    """name/alias(lower) → {name, alias, type, raw} из текущего .mrt."""
    path = mrt_path or MRT_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for key, var in _mrt_variable_list(data if isinstance(data, dict) else {}):
        name = str(var.get("Name") or "").strip()
        alias = str(var.get("Alias") or "").strip()
        typ = str(var.get("Type") or "").strip()
        meta = {"name": name or key, "alias": alias, "type": typ, "key": key, "raw": var}
        if name:
            out[name.lower()] = meta
        if alias:
            out[alias.lower()] = meta
        out[str(key).lower()] = meta
    return out


def _normalize_filters_against_mrt(filters: dict[str, object]) -> dict[str, object]:
    """alias→name, value под Type (StringList → list, не JSON-строка)."""
    if not filters:
        return {}
    index = _mrt_var_index()
    out: dict[str, object] = {}
    for key, value in filters.items():
        meta = index.get(str(key).lower())
        name = (meta or {}).get("name") or str(key)
        typ = (meta or {}).get("type")
        out[name] = _coerce_variable_value(typ, value)
    return out


def _set_mrt_variable_value(target: dict, value: object) -> None:
    """Записать Value в переменную .mrt в формате Stimulsoft (List = JSON array)."""
    typ = str(target.get("Type") or "")
    coerced = _coerce_variable_value(typ, value)
    if typ.endswith("List") or (isinstance(coerced, list) and not typ.endswith("Range")):
        # важно: массив в JSON, НЕ строка "[...]" — иначе карта/SQL не видят список
        if isinstance(coerced, list):
            target["Value"] = coerced
        elif coerced is None:
            target["Value"] = []
        else:
            target["Value"] = [coerced]
        return
    if typ.endswith("Range") and isinstance(coerced, dict):
        target["Value"] = coerced
        return
    if isinstance(coerced, bool):
        target["Value"] = "True" if coerced else "False"
        return
    if coerced is None:
        target["Value"] = ""
        return
    if isinstance(coerced, (int, float)):
        target["Value"] = str(coerced)
        return
    target["Value"] = str(coerced)


def patch_mrt_for_auto_render(raw: str, filters: dict[str, object] | None) -> str:
    """Только для /open: проставить Value фильтров, панель параметров оставить.

    RequestParameters=true — иначе Stimulsoft не показывает панель фильтров.
    Submit после загрузки жмём в JS (карта иначе без Variables/SQL).
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        return raw
    data["RequestParameters"] = True
    if not filters:
        return json.dumps(data, ensure_ascii=False)
    vars_list = _mrt_variable_list(data)
    by_name = {
        str(v.get("Name") or "").strip().lower(): v
        for _, v in vars_list
        if str(v.get("Name") or "").strip()
    }
    by_alias = {
        str(v.get("Alias") or "").strip().lower(): v
        for _, v in vars_list
        if str(v.get("Alias") or "").strip()
    }
    by_key = {k: v for k, v in vars_list}

    applied = []
    missed: list[str] = []
    for name, value in filters.items():
        key_l = str(name).lower()
        target = by_name.get(key_l) or by_alias.get(key_l)
        if target is None and str(name) in by_key:
            target = by_key[str(name)]
        if target is None and str(name).isdigit():
            idx = int(str(name))
            if 0 <= idx < len(vars_list):
                target = vars_list[idx][1]
        if target is None:
            missed.append(str(name))
            continue
        _set_mrt_variable_value(target, value)
        applied.append(
            f"{target.get('Name') or name}={target.get('Value')!r}"
        )
    print(
        f"mrt-render patched values={applied} missed={missed} RequestParameters=True",
        flush=True,
    )
    return json.dumps(data, ensure_ascii=False)


def inject_filter_values_js(
    html: str,
    filters: dict[str, object] | None,
    *,
    auto_render: bool = False,
) -> str:
    """Клиентский fallback: выставить variables из __dashFilters."""
    if not filters or not isinstance(html, str):
        return html
    # экранируем < чтобы JSON не закрыл <script> (Unexpected token)
    payload = json.dumps(filters, ensure_ascii=False).replace("<", "\\u003c")
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
        "        if (hit != null) { v.value = hit; try { v.valueObject = hit; } catch (e) {} }\n"
        "      });\n"
        "      return;\n"
        "    }\n"
        "    Object.keys(F).forEach(function(k){\n"
        "      function applyOne(vv){ if (!vv) return; vv.value = F[k]; try { vv.valueObject = F[k]; } catch (e) {} }\n"
        "      if (vars[k] != null) { applyOne(vars[k]); return; }\n"
        "      var lk = String(k).toLowerCase();\n"
        "      Object.keys(vars).forEach(function(name){\n"
        "        if (String(name).toLowerCase() === lk) applyOne(vars[name]);\n"
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

    auto_render=True — /open: сразу дашборд с фильтрами.
    /viewer вызывает с auto_render=False.
    """
    viewer = StiViewer()
    apply_license(viewer)
    viewer.options.appearance.fullScreenMode = True
    apply_filter_panel_ui(viewer.options)
    viewer.onBeginProcessData += begin_process_data
    # /open: панель нужна — Submit шлёт postInteraction(Variables), без этого
    # карта остаётся без данных. Кнопку жмём сами в JS после render.
    if filters:
        viewer.onPrepareVariables += make_prepare_variables(filters)
        # клиентский хук как дополнительный канал
        viewer.onPrepareVariables += "dashApplyFilters"
    # StiJavaScript всегда клеит `?sti_event=` к handler.url —
    # нельзя писать `/open?rf=…` (получится `?rf=…?sti_event=` → HTML вместо JS → Unexpected token).
    # rf остаётся в /mrt-render?rf= и в session; handler = чистый путь.
    handler_path = "/open" if auto_render else "/viewer"
    try:
        viewer.handler._StiBaseHandler__url = handler_path  # noqa: SLF001
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
    # /open → /mrt-render?rf=…; /viewer → сырой шаблон через /mrt-render без rf
    if auto_render and filter_token:
        report.loadFile(f"/mrt-render?rf={filter_token}")
    else:
        report.loadFile("/mrt-render")
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
    """Скрипт скрина (poll GET /shot) и window.__dashReport для bake карт."""
    needle = "viewer.report = report;\nviewer.renderHtml('viewerContent');"
    auto_block = ""
    auto_submit_js = ""
    if auto_render:
        # Панель как в /viewer (requestParameters=true). Один Submit → Variables/SQL.
        # После refresh Variables панель снова открываем и держим.
        auto_block = (
            "try { report.requestParameters = true; } catch (e) {}\n"
            "try {\n"
            "  var F = window.__dashFilters || {};\n"
            "  function dashPick(name, alias){\n"
            "    if (name != null && F[name] != null) return F[name];\n"
            "    if (alias != null && F[alias] != null) return F[alias];\n"
            "    var nl = String(name || '').toLowerCase();\n"
            "    var al = String(alias || '').toLowerCase();\n"
            "    var keys = Object.keys(F);\n"
            "    for (var ki = 0; ki < keys.length; ki++) {\n"
            "      var k = keys[ki];\n"
            "      var lk = String(k).toLowerCase();\n"
            "      if (lk === nl || (al && lk === al)) return F[k];\n"
            "    }\n"
            "    return null;\n"
            "  }\n"
            "  if (report.dictionary && report.dictionary.variables) {\n"
            "    var vs = report.dictionary.variables;\n"
            "    var n = vs.count || 0;\n"
            "    for (var i = 0; i < n; i++) {\n"
            "      var v = vs.getByIndex ? vs.getByIndex(i) : (vs.list && vs.list[i]);\n"
            "      if (!v) continue;\n"
            "      var hit = dashPick(v.name, v.alias);\n"
            "      if (hit == null) continue;\n"
            "      try { v.valueObject = hit; } catch (e1) {}\n"
            "      try { v.value = hit; } catch (e2) {}\n"
            "    }\n"
            "  }\n"
            "} catch (e) {}\n"
        )
        auto_submit_js = (
            "(function(){\n"
            "  var F = window.__dashFilters || {};\n"
            "  var tries = 0;\n"
            "  var submitted = false;\n"
            "  function findParam(params, key){\n"
            "    if (!params) return null;\n"
            "    if (params[key]) return params[key];\n"
            "    var lk = String(key).toLowerCase();\n"
            "    var names = Object.keys(params);\n"
            "    for (var i = 0; i < names.length; i++) {\n"
            "      if (String(names[i]).toLowerCase() === lk) return params[names[i]];\n"
            "    }\n"
            "    return null;\n"
            "  }\n"
            "  function itemLabel(it){\n"
            "    if (!it) return '';\n"
            "    if (it.value != null && String(it.value) !== '') return String(it.value);\n"
            "    return it.key != null ? String(it.key) : '';\n"
            "  }\n"
            "  function matchWant(it, wantSet){\n"
            "    if (!it || !wantSet) return false;\n"
            "    var k = it.key != null ? String(it.key) : '';\n"
            "    var v = it.value != null ? String(it.value) : '';\n"
            "    return !!(wantSet[k] || (v && wantSet[v]));\n"
            "  }\n"
            "  function wantSetOf(raw){\n"
            "    var arr = Array.isArray(raw) ? raw : (raw == null ? [] : [raw]);\n"
            "    var out = {};\n"
            "    for (var i = 0; i < arr.length; i++) out[String(arr[i])] = true;\n"
            "    return out;\n"
            "  }\n"
            "  function refreshListLabels(p){\n"
            "    try {\n"
            "      if (p.menu && typeof p.menu.updateParameterValue === 'function') {\n"
            "        p.menu.updateParameterValue();\n"
            "        return;\n"
            "      }\n"
            "    } catch (e0) {}\n"
            "    try {\n"
            "      var box = p.controls && p.controls.firstTextBox;\n"
            "      var items = p.params && p.params.items;\n"
            "      if (!box || !items) return;\n"
            "      var parts = [];\n"
            "      var keys = Object.keys(items);\n"
            "      for (var i = 0; i < keys.length; i++) {\n"
            "        var it = items[keys[i]];\n"
            "        if (!it || !it.isChecked) continue;\n"
            "        parts.push(itemLabel(it));\n"
            "        if (parts.length >= 30) break;\n"
            "      }\n"
            "      box.value = parts.join('; ');\n"
            "    } catch (e1) {}\n"
            "  }\n"
            "  function applyParamValue(p, raw){\n"
            "    if (!p || !p.params) return;\n"
            "    var basic = String(p.params.basicType || '');\n"
            "    var items = p.params.items;\n"
            "    if (basic === 'List' && items) {\n"
            "      var want = wantSetOf(raw);\n"
            "      var keys = Object.keys(items);\n"
            "      for (var i = 0; i < keys.length; i++) {\n"
            "        var it = items[keys[i]];\n"
            "        var on = matchWant(it, want);\n"
            "        try { it.isChecked = on; } catch (e2) {}\n"
            "        try {\n"
            "          if (p.menu && p.menu.paramsItems && p.menu.paramsItems[i] &&\n"
            "              p.menu.paramsItems[i].checkBox &&\n"
            "              typeof p.menu.paramsItems[i].checkBox.setChecked === 'function') {\n"
            "            p.menu.paramsItems[i].checkBox.setChecked(on);\n"
            "          }\n"
            "        } catch (e3) {}\n"
            "      }\n"
            "      refreshListLabels(p);\n"
            "      return;\n"
            "    }\n"
            "    if (items) {\n"
            "      var one = Array.isArray(raw) ? raw[0] : raw;\n"
            "      var key = one;\n"
            "      var label = one != null ? String(one) : '';\n"
            "      var ikeys = Object.keys(items);\n"
            "      for (var j = 0; j < ikeys.length; j++) {\n"
            "        var it2 = items[ikeys[j]];\n"
            "        if (!it2) continue;\n"
            "        if (String(it2.key) === String(one) || String(it2.value) === String(one)) {\n"
            "          key = it2.key;\n"
            "          label = itemLabel(it2);\n"
            "          break;\n"
            "        }\n"
            "      }\n"
            "      try { p.params.key = key; } catch (e4) {}\n"
            "      try {\n"
            "        if (p.controls && p.controls.firstTextBox) p.controls.firstTextBox.value = label;\n"
            "      } catch (e5) {}\n"
            "      return;\n"
            "    }\n"
            "    try { if (typeof p.setValue === 'function') p.setValue(raw); } catch (e6) {}\n"
            "  }\n"
            "  function keepFiltersOpen(js){\n"
            "    try {\n"
            "      var panel = js && js.controls && js.controls.parametersPanel;\n"
            "      if (!panel) return;\n"
            "      if (typeof panel.changeVisibleState === 'function') panel.changeVisibleState(true);\n"
            "      var btn = js.controls && js.controls.buttons && js.controls.buttons.Parameters;\n"
            "      if (btn) {\n"
            "        try { btn.isSelected = true; } catch (e0) {}\n"
            "        try { if (typeof btn.setSelected === 'function') btn.setSelected(true); } catch (e1) {}\n"
            "      }\n"
            "    } catch (e) {}\n"
            "  }\n"
            "  function tick(){\n"
            "    tries++;\n"
            "    var viewer = window.__dashViewer || window.viewer;\n"
            "    var js = viewer && viewer.jsObject;\n"
            "    if (!js) { if (tries < 100) return; clearInterval(timer); return; }\n"
            "    var panel = js.controls && js.controls.parametersPanel;\n"
            "    keepFiltersOpen(js);\n"
            "    if (!panel) { if (tries < 100) return; clearInterval(timer); return; }\n"
            "    var params = js.options && js.options.parameters;\n"
            "    if (!submitted) {\n"
            "      var ready = params && Object.keys(params).length;\n"
            "      if (!ready && tries < 40) return;\n"
            "      if (params) {\n"
            "        if (F && Object.keys(F).length) {\n"
            "          Object.keys(F).forEach(function(k){\n"
            "            var p = findParam(params, k);\n"
            "            if (!p) return;\n"
            "            applyParamValue(p, F[k]);\n"
            "          });\n"
            "        }\n"
            "        // allowUserValues иначе рисует keys — перерисовать подписи values\n"
            "        Object.keys(params).forEach(function(n){\n"
            "          var p = params[n];\n"
            "          if (p && p.params && p.params.basicType === 'List') refreshListLabels(p);\n"
            "        });\n"
            "      }\n"
            "      submitted = true;\n"
            "      try {\n"
            "        if (typeof js.Action === 'function') js.Action('Submit');\n"
            "        else if (panel.mainButtons && panel.mainButtons.submit &&\n"
            "                 typeof panel.mainButtons.submit.action === 'function') {\n"
            "          panel.mainButtons.submit.action();\n"
            "        }\n"
            "      } catch (e2) {}\n"
            "    }\n"
            "    keepFiltersOpen(js);\n"
            "    if (params) {\n"
            "      Object.keys(params).forEach(function(n){\n"
            "        var p = params[n];\n"
            "        if (p && p.params && p.params.basicType === 'List') refreshListLabels(p);\n"
            "      });\n"
            "    }\n"
            "    if (tries > 40) clearInterval(timer);\n"
            "  }\n"
            "  var timer = setInterval(tick, 250);\n"
            "  setTimeout(tick, 0);\n"
            "})();\n"
        )
    repl = (
        auto_block
        + "viewer.report = report;\n"
        f"try {{ report.parameterWidth = {filter_param_width()}; }} catch (ePw) {{}}\n"
        "window.__dashViewer = viewer;\n"
        "window.__dashReport = report;\n"
        "window.viewer = viewer;\n"
        "viewer.renderHtml('viewerContent');\n"
        + auto_submit_js
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
    filter_token: str | None = None,
    *,
    handler_path: str = "/open",
) -> str:
    """Прописать Stimulsoft.handler.url = чистый путь (без ?rf=)."""
    if not isinstance(html, str):
        return html
    # filter_token специально не в URL — см. build_viewer
    url = handler_path or "/open"
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
    # GetResource/handler: inject_screenshot=False — иначе ломаем JS
    # (в бандлах есть «viewerContent»/«StiViewer»).
    if inject_screenshot and hasattr(resp, "get_data") and hasattr(resp, "set_data"):
        raw = resp.get_data(as_text=True)
        if isinstance(raw, str) and ("viewerContent" in raw or "StiViewer" in raw):
            # сначала __dashFilters, потом патч renderHtml (auto_render)
            html = inject_filter_values_js(raw, filters, auto_render=auto_render)
            html = inject_screenshot_ui(html, auto_render=auto_render)
            html = inject_handler_url(html, filter_token, handler_path=handler_path)
            html = inject_filter_panel_layout(html)
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
                "post": {
                    "tags": ["filters"],
                    "summary": "Фильтры / pick / result",
                    "description": (
                        "{} → count+filters (MRT_PATH). "
                        "{question} → LLM pick + result. "
                        "{mrt,question} → без pick, result из вопроса."
                    ),
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "mrt": {
                                            "type": "string",
                                            "description": "Уже выбранный .mrt — только вместе с question",
                                            "example": "demo.mrt",
                                        },
                                        "question": {"type": "string"},
                                        "run_queries": {
                                            "type": "boolean",
                                            "default": True,
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Фильтры (+ dashboard при выборе)",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/FiltersResponse"}
                                }
                            },
                        }
                    },
                },
            },
            "/dashboards": {
                "get": {
                    "tags": ["filters"],
                    "summary": "Список dashboards/*.md и наличие .mrt",
                    "responses": {
                        "200": {
                            "description": "Каталог",
                            "content": {"application/json": {"schema": {"type": "object"}}},
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
                        "Открывает браузер на /open (без токена). "
                        "Accept: application/json → {ok,url,filters}. ?open=0 — без браузера."
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
            "/shot": {
                "get": {
                    "tags": ["export"],
                    "summary": "Сигнал открытому /open: скрин + download (без reload, без токена)",
                    "description": (
                        "Держи /open в браузере. GET /shot — общий сигнал любой вкладке, "
                        "токен не нужен. Дашборд не перезагружается."
                    ),
                    "responses": {
                        "200": {
                            "description": "ok + files (png/pdfs)",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                        "504": {
                            "description": "таймаут — нет открытого /open",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "FiltersResponse": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer"},
                        "filters": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/FilterItem"},
                        },
                        "dashboard": {
                            "type": "object",
                            "description": "Только при question",
                            "properties": {
                                "mrt": {"type": "string"},
                                "mrt_path": {"type": "string"},
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "Секция instruction.md про этот .mrt "
                                        "(заголовок + тело целиком)"
                                    ),
                                },
                            },
                        },
                        "result": {
                            "type": "object",
                            "description": (
                                "Только при question: name→value из вопроса, "
                                "недостающее — дефолты из filters. Готово для POST /open."
                            ),
                            "additionalProperties": True,
                            "example": {"Region": "Брестская", "Year": 2024},
                        },
                    },
                },
                "FilterItem": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string"},
                        "choices": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {},
                                    "value": {},
                                    "checked": {"type": "boolean"},
                                },
                            },
                        },
                        "name": {"type": "string"},
                        "value": {},
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


@app.get("/vendor/html2canvas.min.js")
def vendor_html2canvas():
    """Локальная копия html2canvas — скрин без CDN/интернета."""
    path = ROOT / "vendor" / "html2canvas.min.js"
    if not path.is_file():
        return jsonify({"ok": False, "error": "vendor/html2canvas.min.js missing"}), 404
    resp = Response(path.read_bytes(), mimetype="application/javascript; charset=utf-8")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.post("/filters")
def filters():
    """Фильтры из .mrt.

    Режимы body:
      {}                    → count+filters из MRT_PATH
      {question}            → LLM pick .mrt + result из вопроса
      {mrt, question}       → дашборд уже выбран, без pick, result из вопроса
    """
    from filters_mrt import extract_filters
    from pick_dashboard import (
        build_result,
        extract_filter_values,
        pick_dashboard,
        resolve_dashboard,
    )

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    question = str(body.get("question") or "").strip()
    # пустой mrt → LLM pick; заполненный → только filters/result по этому файлу
    mrt_arg = str(body.get("mrt") or body.get("dashboard") or "").strip()
    rq = body.get("run_queries", True)
    run_queries = str(rq).strip().lower() not in {"0", "false", "no", "off"}

    dashboard_info: dict[str, object] | None = None
    mrt_file = MRT_PATH

    try:
        if not question:
            pass
        elif mrt_arg:
            dashboard_info = resolve_dashboard(mrt_arg)
            mrt_file = Path(str(dashboard_info["mrt_path"]))
        else:
            dashboard_info = pick_dashboard(question)
            mrt_file = Path(str(dashboard_info["mrt_path"]))

        plan = extract_filters(
            mrt_file,
            run_queries=run_queries,
            database_url=_db or None,
        )
    except Exception as exc:
        payload: dict[str, object] = {
            "count": 0,
            "filters": [],
            "error": str(exc),
        }
        if dashboard_info:
            payload["dashboard"] = {
                "mrt": dashboard_info.get("mrt"),
                "mrt_path": dashboard_info.get("mrt_path"),
                "description": dashboard_info.get("description"),
            }
            payload["result"] = {}
        return jsonify(payload), 500

    out: dict[str, object] = {
        "count": plan.get("count", 0),
        "filters": plan.get("filters") or [],
    }
    if plan.get("mrt_stripped"):
        out["error"] = plan.get("error")
        out["mrt_stripped"] = True

    if question and dashboard_info:
        filt_list = list(out["filters"]) if isinstance(out["filters"], list) else []
        try:
            extracted = extract_filter_values(question, filt_list)
        except Exception as exc:
            print(f"extract_filter_values failed: {exc}", flush=True)
            extracted = {}
        out["dashboard"] = {
            "mrt": dashboard_info.get("mrt"),
            "mrt_path": dashboard_info.get("mrt_path"),
            "description": dashboard_info.get("description"),
        }
        out["result"] = build_result(filt_list, extracted)

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    if out.get("mrt_stripped"):
        return resp, 422
    return resp


@app.get("/dashboards")
def dashboards_list():
    """Список .mrt в dashboards/ + есть ли instruction.md."""
    from pick_dashboard import dashboards_dir, list_mrt_files, load_instruction

    base = dashboards_dir()
    items = list_mrt_files(base)
    instruction = load_instruction(base)
    return jsonify(
        {
            "ok": True,
            "dir": str(base.resolve()),
            "instruction": bool(instruction),
            "instruction_preview": (instruction[:400] + "…")
            if len(instruction) > 400
            else instruction,
            "count": len(items),
            "mrt": [d["mrt"] for d in items],
        }
    )


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


# Общий сигнал: GET /shot → открытая вкладка /open|/viewer
# pending не снимаем на poll (иначе сигнал терялся) — только на shot-done / timeout
_SHOT_STATE: dict[str, object] = {"id": None, "at": 0.0}


def _shot_wait_ms() -> int:
    raw = (os.environ.get("SHOT_WAIT_MS") or "90000").strip() or "90000"
    try:
        return max(5000, int(raw))
    except ValueError:
        return 90000


@app.get("/api/shot-poll")
def api_shot_poll():
    """Long-poll: ждёт сигнал GET /shot до ~25с. Pending не снимает."""
    since = (request.args.get("since") or "").strip() or None
    deadline = time.time() + 25.0
    while time.time() < deadline:
        with _SHOT_LOCK:
            sid = _SHOT_STATE.get("id")
            if sid and sid != since:
                return jsonify({"shoot": True, "id": sid})
        time.sleep(0.1)
    return jsonify({"shoot": False, "id": None})


@app.post("/api/shot-done")
def api_shot_done():
    """Вкладка отдала результат; снимаем pending."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {"ok": False, "error": "bad body"}
    sid = body.get("id")
    with _SHOT_LOCK:
        if sid and _SHOT_STATE.get("id") == sid:
            _SHOT_STATE["id"] = None
        elif not sid:
            _SHOT_STATE["id"] = None
        _SHOT_RESULTS["*"] = body
    return jsonify({"ok": True})


@app.get("/shot")
def api_shot():
    """Сигнал открытой вкладке /open: скрин + download, без reload.

    Нужна открытая вкладка /open|/viewer (html2canvas локально, без CDN).
    """
    sid = secrets.token_urlsafe(8)
    with _SHOT_LOCK:
        _SHOT_STATE["id"] = sid
        _SHOT_STATE["at"] = time.time()
        _SHOT_RESULTS.pop("*", None)
    print(f"shot signal id={sid}", flush=True)

    deadline = time.time() + (_shot_wait_ms() / 1000.0)
    result: dict[str, object] | None = None
    while time.time() < deadline:
        with _SHOT_LOCK:
            if "*" in _SHOT_RESULTS:
                result = _SHOT_RESULTS.pop("*")
                break
        time.sleep(0.15)

    if result is None:
        with _SHOT_LOCK:
            if _SHOT_STATE.get("id") == sid:
                _SHOT_STATE["id"] = None
        return jsonify(
            {
                "ok": False,
                "error": "timeout: открой /open в браузере и повтори GET /shot",
                "id": sid,
            }
        ), 504

    ok = bool(result.get("ok"))
    payload = {
        "ok": ok,
        "id": result.get("id") or sid,
        "png": result.get("png"),
        "pdfs": result.get("pdfs") or [],
        "files": result.get("files") or [],
        "error": result.get("error"),
        "exports": str(EXPORT_DIR.resolve()),
    }
    return jsonify(payload), (200 if ok else 500)


@app.route("/mrt-render", methods=["GET", "POST"])
def serve_mrt_render():
    """Шаблон: Value фильтров из session/LATEST (или legacy ?rf=). Без фильтров — сырой .mrt."""
    token = (request.args.get("rf") or "").strip() or None
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    filters = _resolve_filters(token) or {}
    if not filters:
        resp = Response(raw, mimetype="application/json; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store"
        return resp
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
    designer.options.localization = "ru"
    apply_filter_panel_ui(designer.options.viewerOptions)
    designer.onBeginProcessData += begin_process_data
    designer.onSaveReport += save_report
    designer.onSaveAsReport += save_report

    if sti_process(designer):
        return sti_response(designer)

    report = StiReport()
    report.loadFile("/mrt-render")
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

    Отдельно от /viewer (preview с ручным Submit). Без ?rf= в URL.

    GET  /open?trendtype=nocumulative&typenum=1000
    GET  /open?filters={"trendtype":"nocumulative","typenum":1000}
    POST /open  JSON {"filters":{"trendtype":"nocumulative","typenum":1000}}
         → открывает браузер на /open (или ?open=0 без браузера)
    """
    legacy_rf = (request.args.get("rf") or "").strip() or None
    incoming = _collect_incoming_filters()
    is_handler = _is_stimulsoft_handler_request()

    if incoming:
        incoming = _normalize_filters_against_mrt(incoming)
        _remember_filters(incoming)
        if request.method == "POST" and not is_handler:
            view_path = "/open"
            view_url = f"http://{HOST}:{PORT}{view_path}"
            open_flag = (request.args.get("open") or "1").strip().lower()
            if open_flag not in {"0", "false", "no", "off"}:
                try:
                    webbrowser.open(view_url)
                    print(f"opened browser: {view_url}", flush=True)
                except Exception as exc:
                    print(f"browser open failed: {exc}", flush=True)
            wants_json = "application/json" in (request.headers.get("Accept") or "")
            if wants_json:
                return jsonify(
                    {
                        "ok": True,
                        "url": view_url,
                        "path": view_path,
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

    filters = _resolve_filters(legacy_rf, incoming)
    if filters:
        filters = _normalize_filters_against_mrt(filters)
        _remember_filters(filters)

    print(f"open filters={filters}", flush=True)
    return build_viewer(
        filters=filters,
        filter_token=None,
        inject_screenshot=True,
        auto_render=True,
    )


@app.route("/open", methods=["GET", "POST"])
def open_with_filters():
    """Передал фильтры → открылся дашборд уже с ними (без Submit)."""
    return open_dashboard_with_filters()


def main() -> None:
    print("Stimulsoft Dashboards.PYTHON 2024.2.2 — official Flask wiring", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    print(f"Designer: http://{HOST}:{PORT}/designer", flush=True)
    print(f"Viewer:   http://{HOST}:{PORT}/viewer   (preview + Submit)", flush=True)
    print(f"Filters:  POST http://{HOST}:{PORT}/filters  ({{question}} → LLM pick)", flush=True)
    print(f"Boards:   http://{HOST}:{PORT}/dashboards", flush=True)
    print(f"Open:     http://{HOST}:{PORT}/open     (filters → ready dashboard)", flush=True)
    print(f"Shot:     http://{HOST}:{PORT}/shot     (signal → open tab shoots)", flush=True)
    print(f"Swagger:  http://{HOST}:{PORT}/docs", flush=True)
    print(f"Screens:  {EXPORT_DIR.resolve()}", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/viewer")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
