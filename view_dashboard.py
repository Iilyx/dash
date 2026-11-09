#!/usr/bin/env python3
"""Viewer Stimulsoft Dashboards.PYTHON для .mrt + Postgres.

  - открыть дашборд + панель фильтров (Parameters Panel)
  - передать выбранные фильтры в SQL
  - писать в sql_trace JSON: params, sql_template(.mrt), sql_from_client, sql_substituted

Запуск:
  MRT_PATH=full.mrt .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
import webbrowser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

# Нужен пакет dashboards — иначе в HTML не подтянется stimulsoft.dashboards.js
import stimulsoft_dashboards  # noqa: F401
from flask import Flask, Response, jsonify, request, url_for
from stimulsoft_data_adapters.StiSqlAdapter import StiSqlAdapter
from stimulsoft_reports.events import StiDataEventArgs, StiVariablesEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer
from stimulsoft_reports.viewer.enums import StiParametersPanelPosition

from extract_mrt import (
    catalog_from_report,
    flag_false,
    flag_true,
    load_mrt,
    numbered,
    repair_mrt_json,
)

ROOT = Path(__file__).resolve().parent
SQL_LOG: deque[dict] = deque(maxlen=80)
LAST_VARIABLES: dict[str, object] = {}
# SqlCommand из .mrt (с @param). Клиент часто шлёт queryString уже без @.
SOURCE_SQL_TEMPLATES: dict[str, str] = {}

_NUM_TYPE_HINTS = (
    "number",
    "int",
    "long",
    "short",
    "byte",
    "decimal",
    "double",
    "float",
    "single",
    "money",
    "numeric",
)

FILTER_ELEMENT_TYPES = {
    "StiComboBoxElement",
    "StiListBoxElement",
    "StiTreeViewElement",
    "StiTreeViewBoxElement",
    "StiDatePickerElement",
    "StiNumberBoxElement",
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
    # Stimulsoft PostgreSQL adapter: host/port/database/user id/password
    return (
        f"Host={host}; Port={port}; Database={database}; "
        f"User Id={user}; Password={password};"
    )


def mask_pg_conn(conn: str) -> str:
    if not conn:
        return ""
    return re.sub(r"(Password|Pwd)\s*=\s*[^;]*", r"\1=***", conn, flags=re.IGNORECASE)


def probe_database() -> dict:
    """Проверка, что Postgres из .env реально отвечает."""
    url = database_url()
    info = {
        "configured": bool(url),
        "pg_conn": bool(PG_CONN),
        "pg_conn_masked": mask_pg_conn(PG_CONN),
        "ok": False,
        "error": None,
    }
    if not url:
        info["error"] = (
            "Нет DATABASE_URL / POSTGRES_USER+PASSWORD+DB в .env — "
            "viewer не подставит connection string."
        )
        return info
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user")
                db, user = cur.fetchone()
        info["ok"] = True
        info["database"] = db
        info["user"] = user
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def resolve_mrt_path() -> Path:
    """Сначала MRT_PATH из .env, опционально переопределение аргументом CLI."""
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

PG_CONN = stimulsoft_pg_connection_string(database_url()) if database_url() else ""

# Файл: параметры → заполненный SQL → результат (append)
_sql_log_raw = os.environ.get("SQL_LOG_PATH", "sql_trace.json").strip() or "sql_trace.json"
SQL_LOG_PATH = path_of(_sql_log_raw)

# RequestParameters=true → dashboard НЕ ходит в SQL, пока не нажали Submit.
# По умолчанию 0: сразу грузим DataSources (панель фильтров всё равно сверху).
FORCE_REQUEST_PARAMETERS = os.environ.get("FORCE_REQUEST_PARAMETERS", "0").lower() in {
    "1",
    "true",
    "yes",
}
# Panel = полоса параметров сверху; Dialog = модальное окно
_explicit = os.environ.get("FORCE_PARAMETERS_MODE", "").strip()
if _explicit.lower() in {"0", "false", "no", "off"}:
    FORCE_PARAMETERS_MODE = None
elif _explicit.lower() == "dialog":
    FORCE_PARAMETERS_MODE = "Dialog"
elif _explicit.lower() == "panel":
    FORCE_PARAMETERS_MODE = "Panel"
elif _explicit:
    FORCE_PARAMETERS_MODE = _explicit
else:
    # legacy FORCE_PARAMETERS_PANEL=1 / без MODE → Panel сверху
    _legacy = (
        os.environ.get("FORCE_PARAMETERS_PANEL")
        or os.environ.get("FORCE_PARAMETERS_DIALOG")
        or "1"
    ).strip()
    if _legacy.lower() in {"0", "false", "no", "off"}:
        FORCE_PARAMETERS_MODE = None
    elif _legacy.lower() == "dialog":
        FORCE_PARAMETERS_MODE = "Dialog"
    else:
        FORCE_PARAMETERS_MODE = "Panel"
FORCE_PARAMETERS_PANEL = FORCE_PARAMETERS_MODE is not None  # совместимость /debug
FORCE_SHOW_ON_PARAMETERS_PANEL = os.environ.get(
    "FORCE_SHOW_ON_PARAMETERS_PANEL", "1"
).lower() in {"1", "true", "yes"}
# Columns→Items режет списки значений. По умолчанию OFF: DialogInfo грузит Keys/Values SQL.
# Пустые int смягчаем в begin_process_data + Value=0 у RFU.
FORCE_SKIP_COLUMNS_PRELOAD = os.environ.get(
    "FORCE_SKIP_COLUMNS_PRELOAD", "0"
).lower() in {"1", "true", "yes"}
# ширина полей на панели параметров (px); 0 в .mrt = дефолт Stimulsoft ~200
PARAMETER_WIDTH = int(os.environ.get("PARAMETER_WIDTH", "280") or "280")

# StiDashboardViewerSettings (биты):
# ShowToolBar=1 Refresh=2 Open=4 FullScreen=8 Menu=16 Edit=32
# Snapshots=64 Exports=128 ResetAllFilters=256 Parameters=512  All=1023
# ResetAllFilters НЕ включаем: без ComboBox/TreeView на холсте это пустая кнопка.
DASHBOARD_VIEWER_SETTINGS = 1 | 2 | 4 | 8 | 16 | 128 | 512  # = 671
FORCE_DASHBOARD_TOOLBAR = os.environ.get("FORCE_DASHBOARD_TOOLBAR", "1").lower() in {
    "1",
    "true",
    "yes",
}

# JS: RequestParameters + Dialog/Panel + RFU
_OPENED_REPORT_JS = f"""
(function () {{
  var r = args.report;
  if (!r) return;
  r.requestParameters = {str(FORCE_REQUEST_PARAMETERS).lower()};
  try {{
    r.parametersDisplayMode = Stimulsoft.Report.StiParametersDisplayMode.{FORCE_PARAMETERS_MODE or "Panel"};
  }} catch (e) {{
    try {{ r.parametersDisplayMode = {"0" if FORCE_PARAMETERS_MODE == "Dialog" else "1"}; }} catch (e2) {{}}
  }}
  try {{ r.dashboardViewerSettings = 671; }} catch (e4) {{}}
  try {{ r.parameterWidth = {PARAMETER_WIDTH}; }} catch (e5) {{}}
  try {{
    var dict = r.dictionary;
    var vars = dict && dict.variables;
    if (vars) {{
      var count = vars.count || 0;
      for (var i = 0; i < count; i++) {{
        var v = vars.getByIndex ? vars.getByIndex(i) : null;
        if (!v && vars.list) v = vars.list[i];
        if (!v) continue;
        // в 2026.x showOnParametersPanel ↔ requestFromUser
        if (v.requestFromUser || v.showOnParametersPanel) {{
          v.requestFromUser = true;
          v.showOnParametersPanel = true;
        }}
      }}
    }}
    // SQL DataSources — подключаться при открытии
    var dss = dict && dict.dataSources;
    if (dss) {{
      var n = dss.count || 0;
      for (var j = 0; j < n; j++) {{
        var ds = dss.getByIndex ? dss.getByIndex(j) : (dss.list && dss.list[j]);
        if (!ds) continue;
        try {{ ds.connectOnStart = true; }} catch (e6) {{}}
      }}
    }}
  }} catch (e3) {{}}
}})();
""".strip()

# Раскрыть полосу Parameters + Submit только когда paramsVariables уже пришли.
# Крестик на панели закрывает фильтры — прячем его и сразу открываем панель снова.
_AFTER_INIT_HEAD = f"""
<style>
  /* не давать свернуть панель параметров крестиком */
  .stiJsViewerParametersPanel [title="Close"],
  .stiJsViewerParametersPanel [title="Закрыть"],
  .stiJsViewerParametersPanel .stiJsViewerClearButton,
  .stiJsViewerParametersPanel button[name="Close"],
  .stiJsViewerParametersPanel .stiJsViewerButtonClose {{
    display: none !important;
  }}
  /* расширить поля фильтров на полосе Parameters */
  .stiJsViewerParametersPanel input[type="text"],
  .stiJsViewerParametersPanel input:not([type]),
  .stiJsViewerParametersPanel select,
  .stiJsViewerParametersPanel textarea,
  .stiJsViewerParametersPanel .stiJsViewerTextBox,
  .stiJsViewerParametersPanel .stiJsViewerDropDownList,
  .stiJsViewerParametersPanel td > div {{
    min-width: {PARAMETER_WIDTH}px !important;
    width: {PARAMETER_WIDTH}px !important;
    max-width: none !important;
    box-sizing: border-box !important;
  }}
  .stiJsViewerParametersPanel {{
    min-height: 72px !important;
  }}
  .stiJsViewerParametersPanel label,
  .stiJsViewerParametersPanel .stiJsViewerParametersPanelDescription {{
    font-size: 13px !important;
  }}
</style>
<script>
window.__stiFindViewer = function () {{
  var names = ['jsViewer', 'jsStiViewer', 'viewer'];
  for (var i = 0; i < names.length; i++) {{
    if (window[names[i]] && window[names[i]].controls) return window[names[i]];
  }}
  for (var k in window) {{
    if (k.indexOf('js') === 0 && window[k] && window[k].controls && window[k].options) {{
      return window[k];
    }}
  }}
  return null;
}};
window.__stiCountParams = function (v) {{
  var pv = v && v.options && v.options.paramsVariables;
  if (!pv) return 0;
  var n = 0;
  for (var key in pv) {{
    // isCategory=true — папка в словаре, не поле фильтра
    if (pv[key] && pv[key].isCategory !== true) n++;
  }}
  return n;
}};
window.__stiWidenParameterFields = function (v) {{
  try {{
    if (v && v.options) v.options.currentParameterWidth = {PARAMETER_WIDTH};
  }} catch (e) {{}}
  var root = v && v.controls && v.controls.parametersPanel;
  if (!root) return;
  var nodes = root.querySelectorAll('input, select, textarea, .stiJsViewerTextBox');
  for (var i = 0; i < nodes.length; i++) {{
    try {{
      nodes[i].style.minWidth = '{PARAMETER_WIDTH}px';
      nodes[i].style.width = '{PARAMETER_WIDTH}px';
    }} catch (e2) {{}}
  }}
}};
window.__stiForceParametersPanel = function () {{
  var v = window.__stiFindViewer();
  if (!v || !v.controls) return false;
  var count = window.__stiCountParams(v);
  if (v.controls.buttons) {{
    if (v.controls.buttons.ResetAllFilters) {{
      try {{ v.controls.buttons.ResetAllFilters.style.display = 'none'; }} catch (e1) {{}}
    }}
    if (v.controls.buttons.Parameters) {{
      try {{
        v.controls.buttons.Parameters.setEnabled(count > 0);
        v.controls.buttons.Parameters.style.display = '';
      }} catch (e2) {{}}
    }}
  }}
  if (count < 1) {{
    console.log('[viewer] waiting paramsVariables…');
    return false;
  }}
  // Сначала полоса сверху (Panel); Dialog-окно — только если полосы нет
  if (v.controls.parametersPanel) {{
    try {{
      v.waitingToShowParameters = true;
      v.controls.parametersPanel.changeVisibleState(true);
      if (v.controls.buttons && v.controls.buttons.Parameters) {{
        v.controls.buttons.Parameters.setSelected(true);
      }}
      window.__stiWidenParameterFields(v);
      console.log('[viewer] parameters Panel opened, params=', count, v.options.paramsVariables);
      return true;
    }} catch (e) {{
      console.warn('[viewer] open parameters panel', e);
    }}
  }}
  try {{
    var form = v.controls.forms && v.controls.forms.parametersForm;
    if (form && typeof form.changeVisibleState === 'function') {{
      form.changeVisibleState(true);
      console.log('[viewer] parameters Dialog opened, params=', count);
      return true;
    }}
  }} catch (e0) {{}}
  return false;
}};
window.__stiKeepParametersPanel = function () {{
  var v = window.__stiFindViewer();
  if (!v || !v.controls || !v.controls.parametersPanel) return;
  if (!v.controls.parametersPanel.visible) {{
    window.__stiForceParametersPanel();
  }} else {{
    window.__stiWidenParameterFields(v);
  }}
}};
window.__stiAfterInit = function (args) {{
  var tries = 0;
  var timer = setInterval(function () {{
    tries += 1;
    if (window.__stiForceParametersPanel() || tries > 80) {{
      clearInterval(timer);
      setInterval(window.__stiKeepParametersPanel, 1000);
    }}
  }}, 250);
}};
</script>
""".strip()

app = Flask(__name__)


def package_versions() -> dict:
    out = {}
    for name in (
        "stimulsoft-dashboards",
        "stimulsoft-reports",
        "stimulsoft-data-adapters",
    ):
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def _is_empty(value: object) -> bool:
    """Пустой фильтр: None / '' / {} / [] / '{}'."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() in {"", "{}", "[]", "null", "NULL", "None"}:
        return True
    if isinstance(value, (list, tuple, dict, set)) and len(value) == 0:
        return True
    return False


def _is_number_param(param) -> bool:
    group = str(getattr(param, "typeGroup", "") or "").lower()
    name = str(getattr(param, "typeName", "") or "").lower()
    if group == "number":
        return True
    return any(h in group or h in name for h in _NUM_TYPE_HINTS)


def _soften_empty_param(query: str, name: str) -> str:
    """Пустой фильтр: только вхождения @Param → TRUE/NULL. Остальной SqlCommand не трогаем."""
    if not query:
        return query
    pat = re.escape(name)
    query = re.sub(
        rf"[\w.\"`\[\]]+?\s*=\s*[@:]{pat}\s*::\s*\w+",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        rf"[@:]{pat}\s*::\s*\w+",
        "NULL",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        rf"CAST\s*\(\s*[@:]{pat}\s+AS\s+[^)]+\)",
        "NULL",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        rf"[\w.\"`\[\]]+?\s+IN\s*\(\s*[@:]{pat}\s*\)",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        rf"[\w.\"`\[\]]+?\s*=\s*ANY\s*\(\s*[@:]{pat}\s*\)",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        rf"[\w.\"`\[\]]+?\s*=\s*[@:]{pat}\b",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    return re.sub(rf"[@:]{pat}\b", "NULL", query, flags=re.IGNORECASE)


def _soften_inlined_empty_literals(query: str) -> str:
    """Клиент уже вшил '' вместо пустого фильтра — убрать до PG (integer \"\").

    Только сравнения/касты, не coalesce(x,'') и не SELECT ''.
    """
    if not query or "''" not in query:
        return query
    query = re.sub(
        r"[\w.\"`\[\]]+?\s*=\s*''\s*::\s*(int|integer|bigint|smallint|numeric|decimal|float|real|double)\b",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"''\s*::\s*(int|integer|bigint|smallint|numeric|decimal|float|real|double)\b",
        "NULL",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"CAST\s*\(\s*''\s+AS\s+(int|integer|bigint|smallint|numeric|decimal|float|real|double)\s*\)",
        "NULL",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"[\w.\"`\[\]]+?\s+IN\s*\(\s*''\s*\)",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"[\w.\"`\[\]]+?\s*=\s*ANY\s*\(\s*''\s*\)",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    # col = '' от пустого фильтра
    query = re.sub(r"[\w.\"`\[\]]+?\s*=\s*''", "TRUE", query)
    return query


def mrt_looks_anonymized(path: Path) -> bool:
    """Обезличка: все строковые значения пустые."""
    raw = path.read_text(encoding="utf-8-sig")
    nonempty = [v for v in re.findall(r':\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw) if v]
    return len(nonempty) == 0


def variable_is_parameters_panel_candidate(variable: dict) -> bool:
    """Кандидат на полосу фильтров: RFU / ShowOnParametersPanel / ключ после обезлички."""
    if flag_false(variable.get("RequestFromUser")) and flag_false(
        variable.get("ShowOnParametersPanel")
    ):
        return False
    if flag_true(variable.get("RequestFromUser")):
        return True
    if flag_true(variable.get("ShowOnParametersPanel")):
        return True
    # Обезличка стирает true→""; ключ RequestFromUser остаётся у бывших RFU
    if "RequestFromUser" in variable and not flag_false(variable.get("RequestFromUser")):
        return True
    if "ShowOnParametersPanel" in variable and not flag_false(
        variable.get("ShowOnParametersPanel")
    ):
        return True
    return False


def _dialog_keys_values(dialog: dict) -> tuple[list, list]:
    keys = dialog.get("Keys")
    values = dialog.get("Values")
    return numbered(keys) if keys is not None else [], numbered(values) if values is not None else []


def _dialog_needs_sql_preload(dialog: dict) -> bool:
    """Как StiVariablesHelper.fillDialogInfoItems: Columns + пустые Keys → connectToData."""
    if not isinstance(dialog, dict):
        return False
    init = dialog.get("ItemsInitializationType")
    is_columns = flag_true(init) or str(init).lower() in {"columns", "1"}
    if not is_columns and not (dialog.get("KeysColumn") or dialog.get("ValuesColumn")):
        return False
    keys, values = _dialog_keys_values(dialog)
    nonempty_keys = [k for k in keys if k not in (None, "")]
    nonempty_vals = [v for v in values if v not in (None, "")]
    return not nonempty_keys or not nonempty_vals


def soften_variable_dialog_for_panel(variable: dict) -> bool:
    """Опционально Columns→Items (FORCE_SKIP_COLUMNS_PRELOAD). По умолчанию DialogInfo не трогаем."""
    dialog = variable.get("DialogInfo")
    if not isinstance(dialog, dict) or not _dialog_needs_sql_preload(dialog):
        return False
    name = variable.get("Name") or "value"
    dialog["ItemsInitializationType"] = "Items"
    dialog.pop("KeysColumn", None)
    dialog.pop("ValuesColumn", None)
    keys, values = _dialog_keys_values(dialog)
    nonempty_keys = [k for k in keys if k not in (None, "")]
    if not nonempty_keys:
        typ = str(variable.get("Type") or "").lower()
        if _type_looks_numeric(typ, name):
            dialog["Keys"] = {"0": "0"}
            dialog["Values"] = {"0": f"(all) {name}"}
        else:
            dialog["Keys"] = {"0": "*"}
            dialog["Values"] = {"0": f"(all) {name}"}
    elif not any(v not in (None, "") for v in values):
        dialog["Values"] = {str(i): str(k) for i, k in enumerate(nonempty_keys)}
    dialog["AllowUserValues"] = True
    variable["DialogInfo"] = dialog
    return True


def _type_looks_numeric(typ: str, name: str = "") -> bool:
    t = (typ or "").lower()
    n = (name or "").lower()
    if any(
        h in t
        for h in (
            "int",
            "long",
            "short",
            "byte",
            "decimal",
            "double",
            "float",
            "single",
            "uint",
            "number",
        )
    ):
        return True
    return any(
        h in n
        for h in (
            "month",
            "year",
            "num",
            "id",
            "type",
            "code",
            "trend",
            "count",
            "qty",
        )
    )


def variable_has_dialog_list(variable: dict) -> bool:
    """Есть DialogInfo со списком (Items/Keys/Columns) — Value не форсим, нужен SelectionMode=First."""
    dialog = variable.get("DialogInfo")
    if not isinstance(dialog, dict) or not dialog:
        return False
    keys, _values = _dialog_keys_values(dialog)
    if any(k not in (None, "") for k in keys):
        return True
    if dialog.get("KeysColumn") or dialog.get("ValuesColumn"):
        return True
    init = str(dialog.get("ItemsInitializationType") or "").lower()
    return init in {"columns", "items", "1"}


def default_value_for_variable(variable: dict) -> str | None:
    """Дефолт вместо ''. None = не ставить Value (DialogInfo сам выберет First)."""
    if variable_has_dialog_list(variable):
        return None
    typ = str(variable.get("Type") or "")
    name = str(variable.get("Name") or "")
    t = typ.lower()
    if "bool" in t:
        return "False"
    if "datetime" in t or t.endswith(".date") or t == "date":
        return "Today"
    if _type_looks_numeric(typ, name):
        return "0"
    # без DialogInfo строковый placeholder; '' нельзя (PG int "")
    return "0" if _type_looks_numeric(typ, name) else "_"


def dashboard_debug(path: Path) -> dict:
    """Что Stimulsoft должен показать как фильтры."""
    try:
        catalog = catalog_from_report(load_mrt(path))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}

    variables = []
    for var in catalog.get("variables") or []:
        if not isinstance(var, dict):
            continue
        show = var.get("show_on_parameters_panel")
        if show is None and var.get("request_from_user"):
            show = True
        variables.append(
            {
                "name": var.get("name"),
                "alias": var.get("alias"),
                "type": var.get("type"),
                "category": var.get("category"),
                "value": var.get("value"),
                "request_from_user": bool(var.get("request_from_user")),
                "show_on_parameters_panel": bool(show) if show is not None else None,
                "sql_parameter": bool(var.get("sql_parameter")),
                "has_dialog": isinstance(var.get("dialog"), dict),
                "dialog": var.get("dialog"),
            }
        )

    filter_elements = []
    for page in catalog.get("pages") or []:
        if not isinstance(page, dict):
            continue
        page_name = page.get("alias") or page.get("name")
        for el in page.get("elements") or []:
            if not isinstance(el, dict):
                continue
            typ = el.get("type")
            role = el.get("role")
            if typ in FILTER_ELEMENT_TYPES or role == "filter":
                filter_elements.append(
                    {
                        "page": page_name,
                        "name": el.get("name"),
                        "type": typ,
                        "title": el.get("title") or el.get("text"),
                        "data_source": el.get("data_source"),
                    }
                )

    request_vars = [v for v in variables if v.get("request_from_user")]
    panel_vars = [
        v
        for v in variables
        if v.get("request_from_user") and v.get("show_on_parameters_panel") is not False
    ]
    databases = catalog.get("databases") or []
    sources = catalog.get("sources") or []
    sql_sources = [
        s
        for s in sources
        if isinstance(s, dict)
        and (
            str(s.get("type") or "").lower().find("sql") >= 0
            or s.get("sql")
            or s.get("database")
        )
    ]
    return {
        "ok": True,
        "path": str(path.resolve()),
        "anonymized": mrt_looks_anonymized(path),
        "report": catalog.get("report"),
        "request_from_user_variables": request_vars,
        "request_from_user_count": len(request_vars),
        "show_on_parameters_panel_variables": panel_vars,
        "show_on_parameters_panel_count": len(panel_vars),
        "all_variables_count": len(variables),
        "variables": variables,
        "databases": databases,
        "databases_count": len(databases),
        "data_sources": sources,
        "data_sources_count": len(sources),
        "sql_sources": sql_sources,
        "sql_sources_count": len(sql_sources),
        "filter_elements_on_canvas": filter_elements,
        "filter_elements_count": len(filter_elements),
        "hint": (
            "Панель фильтров ← RequestFromUser=true + ShowOnParametersPanel=true "
            "+ RequestParameters. Canvas ComboBox/TreeView ← filter_elements (без Submit)."
        ),
        "force_request_parameters": FORCE_REQUEST_PARAMETERS,
        "force_parameters_mode": FORCE_PARAMETERS_MODE,
        "force_parameters_panel": FORCE_PARAMETERS_PANEL,
        "force_show_on_parameters_panel": FORCE_SHOW_ON_PARAMETERS_PANEL,
        "force_skip_columns_preload": FORCE_SKIP_COLUMNS_PRELOAD,
        "stimulsoft_versions": package_versions(),
        "note_show_on_parameters_panel": (
            "Панель сверху = Panel. У DialogInfo → SelectionMode=First (без Value=0/*). "
            "Списки Keys/Values грузит Columns SQL. Пустые SQL-параметры смягчаем отдельно."
        ),
        "note_data_sources": (
            "SQL DataSources ходят в Postgres через onBeginProcessData (viewer+report). "
            "Нужны POSTGRES_* в .env и ConnectOnStart у источников."
        ),
    }


def mrt_has_request_from_user(report: dict) -> bool:
    dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}
    for variable in numbered(dictionary.get("Variables")):
        if isinstance(variable, dict) and variable_is_parameters_panel_candidate(variable):
            return True
    return False


def patch_mrt_for_parameters_panel(raw: str) -> tuple[str, dict]:
    """RequestParameters + Panel + ShowOnParametersPanel (+ RFU) для кандидатов панели."""
    meta = {
        "patched": False,
        "request_parameters": False,
        "parameters_display_mode": None,
        "has_request_from_user": False,
        "rfu_forced": 0,
        "show_on_panel_forced": 0,
        "panel_candidates": 0,
    }
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        try:
            raw = repair_mrt_json(raw)
            report = json.loads(raw)
            meta["json_repaired"] = True
        except json.JSONDecodeError as exc:
            meta["error"] = f"json: {exc}"
            return raw, meta
    if not isinstance(report, dict):
        return raw, meta

    meta["has_request_from_user"] = mrt_has_request_from_user(report)
    changed = False

    if FORCE_REQUEST_PARAMETERS:
        if not report.get("RequestParameters"):
            report["RequestParameters"] = True
            changed = True
    else:
        # иначе dashboard ждёт Submit и SQL «молчит»
        if report.get("RequestParameters") not in (False, 0, "false", "False"):
            report["RequestParameters"] = False
            changed = True
    meta["request_parameters"] = bool(report.get("RequestParameters"))

    if FORCE_PARAMETERS_MODE:
        want = FORCE_PARAMETERS_MODE  # Dialog|Panel
        mode = report.get("ParametersDisplayMode")
        ok = {
            "Dialog": ("Dialog", 0, "0"),
            "Panel": ("Panel", 1, "1"),
        }.get(want, ("Dialog", 0, "0"))
        if mode not in ok:
            report["ParametersDisplayMode"] = want
            changed = True
        meta["parameters_display_mode"] = report.get("ParametersDisplayMode")
    else:
        meta["parameters_display_mode"] = report.get("ParametersDisplayMode")

    if FORCE_DASHBOARD_TOOLBAR:
        prev = report.get("DashboardViewerSettings")
        try:
            prev_i = int(prev) if prev is not None else 0
        except (TypeError, ValueError):
            prev_i = 0
        if prev_i != DASHBOARD_VIEWER_SETTINGS:
            report["DashboardViewerSettings"] = DASHBOARD_VIEWER_SETTINGS
            changed = True
        meta["dashboard_viewer_settings"] = report.get("DashboardViewerSettings")
        meta["dashboard_viewer_settings_was"] = prev
    else:
        meta["dashboard_viewer_settings"] = report.get("DashboardViewerSettings")

    # ширина полей Parameters panel
    if PARAMETER_WIDTH > 0:
        prev_w = report.get("ParameterWidth")
        try:
            prev_w_i = int(prev_w) if prev_w not in (None, "") else 0
        except (TypeError, ValueError):
            prev_w_i = 0
        if prev_w_i != PARAMETER_WIDTH:
            report["ParameterWidth"] = PARAMETER_WIDTH
            changed = True
        meta["parameter_width"] = report.get("ParameterWidth")

    dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}

    # DataSources: ConnectOnStart=true, чтобы dashboard реально ходил в SQL
    connect_forced = 0
    for source in numbered(dictionary.get("DataSources")):
        if not isinstance(source, dict):
            continue
        ident = str(source.get("Ident") or "")
        # SqlCommand может быть ключом даже с пустым телом (обезличка)
        has_sql_key = any(k in source for k in ("SqlCommand", "Query", "SqlQuery"))
        has_sql = bool(
            source.get("SqlCommand") or source.get("Query") or source.get("SqlQuery")
        )
        is_sql = (
            "Sql" in ident
            or has_sql
            or has_sql_key
            or bool(source.get("NameInSource"))
            or bool(source.get("Connection"))
        )
        if not is_sql:
            continue
        if source.get("ConnectOnStart") is False or flag_false(source.get("ConnectOnStart")):
            source["ConnectOnStart"] = True
            connect_forced += 1
            changed = True
        elif "ConnectOnStart" not in source or not flag_true(source.get("ConnectOnStart")):
            source["ConnectOnStart"] = True
            connect_forced += 1
            changed = True
    meta["connect_on_start_forced"] = connect_forced
    rfu_forced = 0
    show_forced = 0
    columns_softened = 0
    defaults_set = 0
    candidates = 0
    for variable in numbered(dictionary.get("Variables")):
        if not isinstance(variable, dict):
            continue
        if not variable_is_parameters_panel_candidate(variable):
            continue
        candidates += 1
        if not flag_true(variable.get("RequestFromUser")):
            variable["RequestFromUser"] = True
            rfu_forced += 1
            changed = True
        if FORCE_SHOW_ON_PARAMETERS_PANEL and not flag_true(
            variable.get("ShowOnParametersPanel")
        ):
            variable["ShowOnParametersPanel"] = True
            show_forced += 1
            changed = True
        # DialogInfo Columns/Items не режем (кроме явного FORCE_SKIP_COLUMNS_PRELOAD)
        if FORCE_SKIP_COLUMNS_PRELOAD and soften_variable_dialog_for_panel(variable):
            columns_softened += 1
            changed = True
        if variable_has_dialog_list(variable):
            # как в Stimulsoft-сэмплах: SelectionMode=First, без Value=0/*
            if variable.get("SelectionMode") not in ("First", "first", 1, "1"):
                variable["SelectionMode"] = "First"
                changed = True
            if "Value" in variable and _is_empty(variable.get("Value")):
                del variable["Value"]
                defaults_set += 1
                changed = True
        elif _is_empty(variable.get("Value")):
            default = default_value_for_variable(variable)
            if default is not None:
                variable["Value"] = default
                defaults_set += 1
                changed = True
    meta["panel_candidates"] = candidates
    meta["rfu_forced"] = rfu_forced
    meta["show_on_panel_forced"] = show_forced
    meta["columns_dialog_softened"] = columns_softened
    meta["empty_defaults_set"] = defaults_set
    meta["has_request_from_user"] = candidates > 0 or meta["has_request_from_user"]

    meta["patched"] = changed
    if not changed:
        return raw, meta
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n", meta


def prepare_variables(args: StiVariablesEventArgs) -> None:
    """Лог выбранных параметров панели. Пустые с DialogInfo не трогаем (First)."""
    global LAST_VARIABLES
    variables = args.variables or {}
    names = list(variables.keys())
    print(f"[viewer] onPrepareVariables count={len(names)} names={names}", flush=True)
    dialog_list_names: set[str] = set()
    try:
        report = load_mrt(MRT_PATH)
        dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}
        for variable in numbered(dictionary.get("Variables")):
            if isinstance(variable, dict) and variable_has_dialog_list(variable):
                n = variable.get("Name")
                if n:
                    dialog_list_names.add(str(n))
    except Exception as exc:
        print(f"[viewer] prepare_variables dialog map: {exc}", flush=True)

    selected: dict[str, object] = {}
    for name, var in variables.items():
        vtype = var.type.value if getattr(var, "type", None) is not None else "?"
        empty = _is_empty(var.value)
        print(
            f"[viewer]   var {name} type={vtype} empty={empty} value={var.value!r} "
            f"dialog_list={name in dialog_list_names}",
            flush=True,
        )
        if not empty:
            selected[str(name)] = var.value
            continue
        if name in dialog_list_names:
            print(f"[viewer]   var {name}: empty + DialogInfo → leave for First", flush=True)
            continue
        fake = {"Name": name, "Type": vtype, "DialogInfo": {}}
        default = default_value_for_variable(fake) or "0"
        if _type_looks_numeric(vtype, name) and "list" not in vtype.lower():
            try:
                var.value = int(default)
            except (TypeError, ValueError):
                var.value = default
        else:
            var.value = default
        selected[str(name)] = var.value
        print(f"[viewer]   var {name}: empty → {var.value!r}", flush=True)

    LAST_VARIABLES = selected
    _sql_log_add({"phase": "parameters", "params": selected})


def _params_dict(params: dict | None) -> dict[str, object]:
    out: dict[str, object] = {}
    for name, param in (params or {}).items():
        out[str(name)] = getattr(param, "value", param)
    return out


def _sql_at_names(query: str) -> set[str]:
    if not query:
        return set()
    return {m.group(1) for m in re.finditer(r"@([A-Za-z_][A-Za-z0-9_]*)", query)}


def _unescape_sql(query: str) -> str:
    """Убрать JS/JSON-экранирование из SQL до PG.

    c.\\\"fff\\\" → c.\"fff\"
    \\'fff\\' → 'fff'
    \\n → перевод строки; голый \\\\ убираем.
    """
    if not query or "\\" not in query:
        return query
    esc = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "'": "'"}
    for _ in range(8):
        if "\\" not in query:
            break
        out: list[str] = []
        i = 0
        n = len(query)
        while i < n:
            ch = query[i]
            if ch != "\\" or i + 1 >= n:
                out.append(ch)
                i += 1
                continue
            nxt = query[i + 1]
            # \\\\n / \\\\r / \\\\t (двойной слеш + буква)
            if nxt == "\\" and i + 2 < n and query[i + 2] in esc:
                out.append(esc[query[i + 2]])
                i += 3
                continue
            # \" \' \n \r \t
            if nxt in esc:
                out.append(esc[nxt])
                i += 2
                continue
            # голый \\ — убрать оба слеша
            if nxt == "\\":
                i += 2
                continue
            # неизвестное \X — выкинуть слеш, оставить символ
            out.append(nxt)
            i += 2
        new_query = "".join(out)
        if new_query == query:
            break
        query = new_query
    return query


def _sql_literal(param) -> str:
    """Литерал для PG: числа как есть, строки в '...'. Пустой/'{}' → NULL."""
    value = getattr(param, "value", param)
    group = str(getattr(param, "typeGroup", "") or "").lower()
    type_name = str(getattr(param, "typeName", "") or "")
    is_num = (
        group == "number"
        or (isinstance(value, (int, float)) and not isinstance(value, bool))
        or _type_looks_numeric(type_name)
        or (hasattr(param, "typeGroup") and _is_number_param(param))
    )
    if _is_empty(value):
        return "NULL"
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            if _is_empty(item):
                continue
            if is_num or isinstance(item, (int, float)):
                s = str(item).strip()
                if re.fullmatch(r"-?\d+(\.\d+)?", s):
                    parts.append(s)
            else:
                parts.append("'" + str(item).replace("'", "''") + "'")
        return ", ".join(parts) if parts else "NULL"
    if is_num:
        if isinstance(value, str):
            s = value.strip()
            try:
                float(s)
            except ValueError:
                return "NULL"
            return s
        return str(value)
    if group == "datetime":
        return "'" + str(value).replace("'", "''") + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _merge_sql_params(args_params: dict | None) -> dict:
    """args.parameters + LAST_VARIABLES (панель фильтров)."""
    merged: dict[str, object] = {}
    for name, param in (args_params or {}).items():
        merged[str(name)] = param
    for name, value in LAST_VARIABLES.items():
        key = str(name)
        cur = merged.get(key)
        cur_val = getattr(cur, "value", cur) if cur is not None else None
        if cur is None or _is_empty(cur_val):
            merged[key] = value
    return merged


def _substitute_sql(query: str, params: dict | None) -> str:
    """Подстановка @Param без Stimulsoft JS-escape (иначе в SQL остаются \\ и \\')."""
    if not query or not params or not _sql_at_names(query):
        return query or ""
    result = ""
    rest = query
    lower_map = {str(k).lower(): v for k, v in params.items()}
    while True:
        at = rest.find("@")
        if at < 0:
            result += rest
            break
        result += rest[:at]
        rest = rest[at + 1 :]
        name = ""
        while rest and (rest[0].isalnum() or rest[0] == "_"):
            name += rest[0]
            rest = rest[1:]
        item = lower_map.get(name.lower()) if name else None
        if item is None:
            result += "@" + name
        else:
            result += _sql_literal(item)
    return result


def _refresh_sql_templates(report: dict | None = None) -> dict[str, str]:
    """Запомнить SqlCommand с @param из .mrt (клиент шлёт query уже заполненным)."""
    global SOURCE_SQL_TEMPLATES
    try:
        if report is None:
            report = load_mrt(MRT_PATH)
    except Exception as exc:
        print(f"[viewer] sql templates: {exc}", flush=True)
        return SOURCE_SQL_TEMPLATES
    dictionary = report.get("Dictionary") if isinstance(report, dict) else {}
    templates: dict[str, str] = {}
    for source in numbered((dictionary or {}).get("DataSources")):
        if not isinstance(source, dict):
            continue
        name = (source.get("Name") or "").strip()
        sql = _unescape_sql(
            str(
                source.get("SqlCommand")
                or source.get("Query")
                or source.get("SqlQuery")
                or ""
            )
        ).strip()
        if name and sql:
            templates[name] = sql
    SOURCE_SQL_TEMPLATES = templates
    print(f"[viewer] sql templates loaded: {len(templates)}", flush=True)
    return templates


def _lookup_sql_template(data_source: str | None) -> str:
    if not data_source:
        return ""
    if not SOURCE_SQL_TEMPLATES:
        _refresh_sql_templates()
    if data_source in SOURCE_SQL_TEMPLATES:
        return SOURCE_SQL_TEMPLATES[data_source]
    lower = data_source.lower()
    for name, sql in SOURCE_SQL_TEMPLATES.items():
        if name.lower() == lower:
            return sql
    return ""


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


_SQL_LOG_TEXT_KEYS = (
    "sql_template",
    "sql_from_client",
    "sql_sent",
    "sql_substituted",
)


def _sql_log_write_file(entry: dict) -> None:
    """JSON-мета + SQL сырым текстом (чтобы \" в файле не путал с реальным SQL)."""
    try:
        SQL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        meta = {k: v for k, v in entry.items() if k not in _SQL_LOG_TEXT_KEYS}
        with SQL_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_json_safe(meta), ensure_ascii=False, indent=2))
            fh.write("\n")
            for key in _SQL_LOG_TEXT_KEYS:
                val = entry.get(key)
                if not val:
                    continue
                fh.write(f"--- {key} ---\n")
                fh.write(str(val))
                if not str(val).endswith("\n"):
                    fh.write("\n")
                fh.write(f"--- end {key} ---\n")
            fh.write("\n")
    except Exception as exc:
        print(f"[viewer] sql log file write failed: {exc}", flush=True)


def _sql_log_add(entry: dict) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **entry,
    }
    SQL_LOG.appendleft(entry)
    phase = entry.get("phase")
    if phase == "parameters":
        print(f"[viewer] Параметры {entry.get('params')}", flush=True)
    elif phase == "sql":
        print(
            f"[viewer] SQL {entry.get('source')} "
            f"filled={entry.get('client_already_filled')} "
            f"sub={(entry.get('sql_substituted') or '')[:100]!r}",
            flush=True,
        )
    elif phase == "result":
        if entry.get("error"):
            print(
                f"[viewer] SQL result {entry.get('source')} ERROR: {entry.get('error')}",
                flush=True,
            )
        else:
            print(
                f"[viewer] SQL result {entry.get('source')} rows={entry.get('rows')}",
                flush=True,
            )
    if phase in {"parameters", "sql", "result"}:
        _sql_log_write_file(entry)


def _reset_sql_trace_file() -> None:
    """Новый сеанс — чистый JSONL."""
    try:
        SQL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "phase": "session",
            "mrt": str(MRT_PATH.resolve()),
            "format": (
                "JSONL: params → sql_template(.mrt @param) → "
                "sql_from_client → sql_substituted → result"
            ),
        }
        with SQL_LOG_PATH.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(header, ensure_ascii=False, indent=2))
            fh.write("\n")
        print(f"[viewer] sql_trace → {SQL_LOG_PATH.resolve()}", flush=True)
    except Exception as exc:
        print(f"[viewer] sql_trace reset failed: {exc}", flush=True)


def _should_log_sql_command(args: StiDataEventArgs) -> bool:
    cmd = getattr(args.command, "value", None) or str(args.command or "")
    return cmd in {"ExecuteQuery", "Execute", "GetData"} or bool(args.queryString)


def begin_process_data(args: StiDataEventArgs) -> None:
    """SqlCommand из .mrt: только @Param → значение фильтра. Текст SQL не переписываем."""
    src = args.dataSource or "?"
    connection = args.connection or "?"
    if PG_CONN:
        args.connectionString = PG_CONN
    else:
        print(
            f"[viewer] DB MISSING .env — source={src!r} connection={connection!r}",
            flush=True,
        )

    raw_from_client = args.queryString or ""
    query_from_client = _unescape_sql(raw_from_client)
    query_template = _unescape_sql(_lookup_sql_template(src) or "")
    client_already_filled = bool(query_from_client) and not _sql_at_names(
        query_from_client
    )

    # шаблон .mrt с @param важнее уже «заполненного» client SQL
    used_template = bool(query_template and _sql_at_names(query_template))
    query = query_template if used_template else query_from_client

    raw_params = args.parameters or {}
    params = _merge_sql_params(raw_params)

    # пустые параметры фильтров → TRUE/NULL на месте @Param
    had_empty = False
    for name, param in list(params.items()):
        if _is_empty(getattr(param, "value", param)):
            had_empty = True
            query = _soften_empty_param(query, str(name))

    # заполненные @Param → литерал (пустой → NULL)
    if params and _sql_at_names(query):
        query = _substitute_sql(query, params)

    # клиент уже подставил '' вместо пустого фильтра — иначе PG: integer ""
    if had_empty or "''" in (query or ""):
        query = _soften_inlined_empty_literals(query)

    query = _unescape_sql(query)
    args.queryString = query
    args.parameters = {}

    if not _should_log_sql_command(args):
        return

    _sql_log_add(
        {
            "phase": "sql",
            "source": src,
            "connection": connection,
            "command": getattr(args.command, "value", None) or str(args.command or ""),
            "client_already_filled": client_already_filled,
            "used_mrt_template": used_template,
            "had_backslash_in_raw": "\\" in raw_from_client,
            "params": dict(LAST_VARIABLES),
            "sql_params": _params_dict(raw_params)
            or {str(k): getattr(v, "value", v) for k, v in params.items()},
            "sql_template": query_template,
            "sql_from_client": query_from_client,
            "sql_sent": query,
            "sql_substituted": query,
        }
    )


def end_process_data(args: StiDataEventArgs) -> None:
    """Результат запроса — JSON в sql_trace."""
    if not _should_log_sql_command(args):
        return
    src = args.dataSource or "?"
    result = args.result
    entry: dict = {"phase": "result", "source": src}
    if result is None:
        entry["rows"] = None
        entry["columns"] = None
    elif getattr(result, "success", True) is False:
        entry["error"] = getattr(result, "notice", None) or str(result)
    else:
        cols = getattr(result, "columns", None) or []
        rows = getattr(result, "rows", None)
        count = getattr(result, "count", None)
        entry["columns"] = list(cols) if cols else []
        entry["rows"] = count if count is not None else (len(rows) if rows is not None else 0)
    _sql_log_add(entry)


@app.get("/mrt")
def serve_mrt():
    """Отдать .mrt с патчем панели фильтров."""
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    patched, meta = patch_mrt_for_parameters_panel(raw)
    try:
        _refresh_sql_templates(json.loads(patched))
    except Exception:
        _refresh_sql_templates()
    print(f"[viewer] serve /mrt patch={meta}", flush=True)
    resp = Response(patched, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/debug")
def debug():
    """JSON: RequestFromUser + filter elements + patch flags."""
    info = dashboard_debug(MRT_PATH)
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    _, meta = patch_mrt_for_parameters_panel(raw)
    info["mrt_patch"] = meta
    info["database"] = probe_database()
    info["sql_log_count"] = len(SQL_LOG)
    info["sql_log"] = list(SQL_LOG)[:40]
    info["sql_log_path"] = str(SQL_LOG_PATH.resolve())
    info["last_variables"] = dict(LAST_VARIABLES)
    info["force_request_parameters"] = FORCE_REQUEST_PARAMETERS
    info["note_sql_log"] = (
        "sql_trace JSONL: params → sql_template(.mrt @param) → "
        "sql_from_client → sql_substituted → result."
    )
    if FORCE_REQUEST_PARAMETERS:
        info["note_request_parameters"] = (
            "RequestParameters=true: dashboard SQL ждёт кнопку Submit на панели. "
            "Поставь FORCE_REQUEST_PARAMETERS=0 чтобы грузить DataSources сразу."
        )
    elif not SQL_LOG:
        info["note_sql_log_empty"] = (
            "sql_log пуст — handler ещё не получил ExecuteQuery. "
            "Открой / (не только /debug), проверь database.ok и консоль [viewer]."
        )
    if info.get("filter_elements_count") == 0 and (
        info.get("show_on_parameters_panel_count") or meta.get("panel_candidates")
    ):
        names = [
            v.get("name")
            for v in info.get("show_on_parameters_panel_variables") or []
        ]
        info["note"] = (
            "Canvas-фильтров нет. Ожидай Parameters panel "
            "(RequestFromUser + ShowOnParametersPanel). "
            f"panel vars: {names or meta.get('panel_candidates')}"
        )
    if not meta.get("panel_candidates") and not info.get("request_from_user_count"):
        info["why_no_submit_panel"] = (
            "Нет variables с RequestFromUser / ShowOnParametersPanel. "
            "В редакторе: Request from User + Show on Parameters Panel. "
            "Обезличенный test.mrt без имён/true не даст рабочую панель."
        )
    return jsonify(info)


@app.route("/", methods=["GET", "POST"])
@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    sti = StiViewer()
    sti.id = "viewer"
    # fullScreen ок, но toolbar обязан быть visible (ещё патчим DashboardViewerSettings)
    sti.options.appearance.fullScreenMode = True
    sti.options.appearance.parametersPanelPosition = StiParametersPanelPosition.TOP
    # фильтры в полосе сверху; меньше колонок → шире каждое поле
    sti.options.appearance.parametersPanelColumnsCount = int(
        os.environ.get("PARAMETERS_PANEL_COLUMNS", "3") or "3"
    )
    sti.options.appearance.minParametersCountForMultiColumns = 1
    sti.options.appearance.parametersPanelMaxHeight = int(
        os.environ.get("PARAMETERS_PANEL_MAX_HEIGHT", "220") or "220"
    )
    sti.options.toolbar.visible = True
    sti.options.toolbar.autoHide = False
    sti.options.toolbar.showParametersButton = True
    sti.options.toolbar.showAboutButton = True
    sti.options.toolbar.showRefreshButton = True
    sti.javascript.appendHead(_AFTER_INIT_HEAD)
    sti.onAfterInitialize += "__stiAfterInit"
    sti.onPrepareVariables += prepare_variables
    # SQL DataSources: событие и на viewer, и на report (доки Stimulsoft)
    sti.onBeginProcessData += begin_process_data
    sti.onEndProcessData += end_process_data
    # после открытия .mrt — Panel + RequestParameters (полоса Submit)
    sti.onOpenedReport += _OPENED_REPORT_JS

    if sti.processRequest(request):
        return sti.getFrameworkResponse()

    report = StiReport()
    report.onBeforeRender += _OPENED_REPORT_JS
    report.onBeginProcessData += begin_process_data
    report.onEndProcessData += end_process_data
    report.onPrepareVariables += prepare_variables
    report.loadFile(url_for("serve_mrt", _external=True))
    sti.report = report
    return sti.getFrameworkResponse()


def _install_sql_unescape_hooks() -> None:
    """Последний рубеж: любой SQL в PG проходит через _unescape_sql."""
    if getattr(StiSqlAdapter, "_pg_unescape_hooks", False):
        return
    _orig_apply = StiSqlAdapter.applyQueryParameters
    _orig_native = StiSqlAdapter.executeNative

    @staticmethod
    def _apply(query: str, parameters: dict, escape: bool) -> str:
        # escape=False: не вшивать \\ и \'; escape игнорируем
        out = _orig_apply(_unescape_sql(query or ""), parameters or {}, False)
        return _unescape_sql(out)

    def _native(self, queryString: str, maxDataRows: int, result):
        return _orig_native(self, _unescape_sql(queryString or ""), maxDataRows, result)

    StiSqlAdapter.applyQueryParameters = _apply
    StiSqlAdapter.executeNative = _native
    StiSqlAdapter._pg_unescape_hooks = True
    print("[viewer] SQL unescape hooks on applyQueryParameters/executeNative", flush=True)


def main() -> None:
    info = dashboard_debug(MRT_PATH)
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    _install_sql_unescape_hooks()
    _reset_sql_trace_file()
    print(f"MRT:     {MRT_PATH.resolve()}")
    print(f"params:  {SQL_LOG_PATH.resolve()}")
    print(f"panel:   {FORCE_PARAMETERS_MODE} / RFU={info.get('request_from_user_count')}")
    db = probe_database()
    print(f"DB:      ok={db.get('ok')} {db.get('pg_conn_masked') or ''}")
    if db.get("error"):
        print(f"DB ERR:  {db.get('error')}", flush=True)
    print(f"Open:    {url}")
    if info.get("anonymized"):
        print("WARNING: .mrt обезличен — нужен полный файл (MRT_PATH)", flush=True)
    if OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=VIEWER_HOST, port=VIEWER_PORT, debug=False)


if __name__ == "__main__":
    main()
