#!/usr/bin/env python3
"""Отдельный viewer Stimulsoft Dashboards.PYTHON для .mrt.

Документация Stimulsoft (панель параметров = «панель фильтров»):
  https://www.stimulsoft.com/en/documentation/online/user-manual/data_data_dictionary_variables.htm
  https://stimulsoft.github.io/Stimulsoft.Reports.Dashboards.Programming.Manual/Reports_and_Dashboards_for_Python/Web_Viewer/Work_with_Parameters

Чтобы переменная была на панели фильтров + Submit:
  1) RequestFromUser = true
  2) Report.RequestParameters = true
  3) ParametersDisplayMode = Panel|Dialog  (Panel — полоса сверху; Dialog — окно)
  В JS 2026.x ShowOnParametersPanel = алиас RequestFromUser (отдельного флага нет).
  DialogInfo Columns: список значений грузится SQL (пустые int смягчаем, не режем Columns).

Canvas ComboBox/TreeView — другие фильтры (без Submit).

Запуск:
  MRT_PATH=full.mrt .venv/bin/python view_dashboard.py
  # http://127.0.0.1:8040/  и  /debug

FORCE_REQUEST_PARAMETERS=0 — не ждать Submit (сразу грузить SQL DataSources)
FORCE_PARAMETERS_MODE=Panel|Dialog — по умолчанию Panel (сверху)
FORCE_SKIP_COLUMNS_PRELOAD=0 — не трогать DialogInfo Columns
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
import time
import webbrowser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

# Нужен пакет dashboards — иначе в HTML не подтянется stimulsoft.dashboards.js
import stimulsoft_dashboards  # noqa: F401
from flask import Flask, Response, jsonify, request, url_for
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
# Последние SQL/data-запросы к handler (видно в /debug → sql_log)
SQL_LOG: deque[dict] = deque(maxlen=80)
# Выбранные RFU с панели (onPrepareVariables) — для лога вместе с SQL
LAST_VARIABLES: dict[str, object] = {}
# SqlCommand из .mrt (ключ — Name и Alias источника)
SOURCE_SQL_TEMPLATES: dict[str, str] = {}
SOURCE_SQL_META: dict[str, dict] = {}

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
_sql_log_raw = os.environ.get("SQL_LOG_PATH", "sql_trace.txt").strip() or "sql_trace.txt"
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
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return True
    return False


def _is_number_param(param) -> bool:
    group = str(getattr(param, "typeGroup", "") or "").lower()
    name = str(getattr(param, "typeName", "") or "").lower()
    if group == "number":
        return True
    return any(h in group or h in name for h in _NUM_TYPE_HINTS)


def _soften_empty_param(query: str, name: str) -> str:
    """Пустой @Param/@:Param → no-op/NULL, не '' (PG: integer \"\")."""
    if not query:
        return query
    pat = re.escape(name)
    # касты раньше голого `=` иначе `@p::int` → `TRUE::int`
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
    """Клиент уже подставил '' — убрать до PG (integer \"\")."""
    if not query or "''" not in query:
        return query
    # касты раньше голого `=` иначе `= ''::int` → `TRUE::int`
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
    query = re.sub(r"[\w.\"`\[\]]+?\s*=\s*''", "TRUE", query)
    # выражения вроде f(x) = '' / оставшиеся литералы — не слать '' в int
    query = re.sub(r"=\s*''", " = NULL", query)
    query = re.sub(r"IN\s*\(\s*''\s*\)", "IN (NULL)", query, flags=re.IGNORECASE)
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
    print(f"[viewer][1/3 PARAMETERS] selected={selected}", flush=True)
    _sql_log_add(
        {
            "phase": "parameters",
            "step": "1/3",
            "selected": selected,
        }
    )


def _format_sql_log_block(entry: dict) -> str:
    """Короткий лог: Шаг 1 / Шаг 2 / Шаг 3."""
    ts = entry.get("ts") or ""
    phase = entry.get("phase") or ""
    lines: list[str] = []
    if phase == "parameters":
        lines.append(f"===== {ts} =====")
        lines.append("Шаг 1. Параметры")
        selected = entry.get("selected") or {}
        if not selected:
            lines.append("(пусто)")
        else:
            for key, value in selected.items():
                lines.append(f"  {key} = {value!r}")
    elif phase == "sql":
        lines.append("Шаг 2. SQL")
        lines.append(f"  source: {entry.get('source')}")
        # итоговый SQL — что реально уходит (filled или from_client)
        final_sql = (
            entry.get("query_filled")
            or entry.get("query_from_client")
            or entry.get("query")
            or ""
        )
        lines.append("  sql:")
        lines.append(final_sql)
        params = entry.get("sql_parameters") or {}
        if params:
            lines.append("  params:")
            for key, meta in params.items():
                if isinstance(meta, dict):
                    val = meta.get("value_after", meta.get("value"))
                else:
                    val = meta
                lines.append(f"    @{key} = {val!r}")
    elif phase == "result":
        ok = entry.get("success")
        rows = entry.get("rows")
        notice = entry.get("notice")
        lines.append("Шаг 3. Результат")
        lines.append(f"  source: {entry.get('source')}")
        lines.append(f"  ok: {ok}")
        lines.append(f"  rows: {rows}")
        if notice:
            lines.append(f"  error: {notice}")
        lines.append("")  # пустая строка между запросами
    else:
        return ""
    lines.append("")
    return "\n".join(lines) + "\n"


def _sql_log_write_file(entry: dict) -> None:
    try:
        SQL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        block = _format_sql_log_block(entry)
        if not block:
            return
        with SQL_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(block)
    except Exception as exc:
        print(f"[viewer] sql log file write failed: {exc}", flush=True)


def _sql_log_add(entry: dict) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **entry,
    }
    SQL_LOG.appendleft(entry)
    phase = entry.get("phase") or "log"
    # в консоль — коротко, без простыни
    if phase == "parameters":
        print(f"[viewer] Шаг 1. Параметры {entry.get('selected')}", flush=True)
    elif phase == "sql":
        q = (entry.get("query_filled") or entry.get("query_from_client") or "")[:200]
        print(
            f"[viewer] Шаг 2. SQL source={entry.get('source')!r} {q!r}",
            flush=True,
        )
    elif phase == "result":
        print(
            f"[viewer] Шаг 3. Результат source={entry.get('source')!r} "
            f"ok={entry.get('success')} rows={entry.get('rows')} "
            f"error={entry.get('notice')!r}",
            flush=True,
        )
    else:
        print(f"[viewer][sql:{phase}] {entry}", flush=True)
    _sql_log_write_file(entry)


def _sql_at_names(query: str) -> set[str]:
    if not query:
        return set()
    return {m.group(1) for m in re.finditer(r"@([A-Za-z_][A-Za-z0-9_]*)", query)}


def _sql_brace_names(query: str) -> set[str]:
    """Stimulsoft-выражения {Variable} в тексте SQL."""
    if not query:
        return set()
    return {m.group(1) for m in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", query)}


def _reset_sql_trace_file() -> None:
    """Новый сеанс — чистый файл."""
    try:
        SQL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SQL_LOG_PATH.open("w", encoding="utf-8") as fh:
            fh.write(
                f"sql_trace {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                f"mrt: {MRT_PATH.resolve()}\n"
                "Шаг 1 = параметры, Шаг 2 = SQL, Шаг 3 = результат\n\n"
            )
        print(f"[viewer] sql_trace → {SQL_LOG_PATH.resolve()}", flush=True)
    except Exception as exc:
        print(f"[viewer] sql_trace reset failed: {exc}", flush=True)


def _refresh_sql_templates(report: dict | None = None) -> dict[str, str]:
    """Запомнить SqlCommand из .mrt по Name и Alias (клиент шлёт query уже заполненным)."""
    global SOURCE_SQL_TEMPLATES, SOURCE_SQL_META
    try:
        if report is None:
            report = load_mrt(MRT_PATH)
    except Exception as exc:
        print(f"[viewer] sql templates: {exc}", flush=True)
        return SOURCE_SQL_TEMPLATES
    dictionary = report.get("Dictionary") if isinstance(report, dict) else {}
    templates: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for source in numbered((dictionary or {}).get("DataSources")):
        if not isinstance(source, dict):
            continue
        name = (source.get("Name") or "").strip()
        alias = (source.get("Alias") or "").strip()
        sql = str(
            source.get("SqlCommand")
            or source.get("Query")
            or source.get("SqlQuery")
            or ""
        ).strip()
        if not sql:
            continue
        param_names = []
        for p in numbered(source.get("Parameters")):
            if isinstance(p, dict) and p.get("Name"):
                param_names.append(str(p.get("Name")))
        info = {
            "name": name or None,
            "alias": alias or None,
            "sql": sql,
            "at_names": sorted(_sql_at_names(sql)),
            "parameter_names": param_names,
        }
        for key in {name, alias, name.lower() if name else "", alias.lower() if alias else ""}:
            if not key:
                continue
            templates[key] = sql
            meta[key] = info
    SOURCE_SQL_TEMPLATES = templates
    SOURCE_SQL_META = meta
    print(
        f"[viewer] sql templates loaded: {len({t for t in templates})} keys, "
        f"unique sql={len({id(v) for v in templates.values()})}",
        flush=True,
    )
    return templates


def _lookup_sql_template(data_source: str | None) -> str:
    if not data_source:
        return ""
    if not SOURCE_SQL_TEMPLATES:
        _refresh_sql_templates()
    if data_source in SOURCE_SQL_TEMPLATES:
        return SOURCE_SQL_TEMPLATES[data_source]
    lower = data_source.lower()
    if lower in SOURCE_SQL_TEMPLATES:
        return SOURCE_SQL_TEMPLATES[lower]
    for name, sql in SOURCE_SQL_TEMPLATES.items():
        if name.lower() == lower:
            return sql
    return ""


def _lookup_sql_meta(data_source: str | None) -> dict:
    if not data_source:
        return {}
    if not SOURCE_SQL_META:
        _refresh_sql_templates()
    if data_source in SOURCE_SQL_META:
        return SOURCE_SQL_META[data_source]
    lower = data_source.lower()
    if lower in SOURCE_SQL_META:
        return SOURCE_SQL_META[lower]
    for name, info in SOURCE_SQL_META.items():
        if name.lower() == lower:
            return info
    return {}


def _filled_sql_preview(query: str, params: dict) -> str:
    """Как Stimulsoft подставит @param в SQL (только для лога)."""
    if not query:
        return ""
    if not params or not _sql_at_names(query):
        return query
    try:
        from stimulsoft_data_adapters.StiSqlAdapter import StiSqlAdapter

        return StiSqlAdapter.applyQueryParameters(query, params, True)
    except Exception as exc:
        return f"{query}\n/* fill preview error: {exc} */"


def begin_process_data(args: StiDataEventArgs) -> None:
    """Подмена connection string; лог: параметры → шаблон из .mrt → заполненный SQL."""
    t0 = time.perf_counter()
    src = args.dataSource or "?"
    connection = args.connection or "?"
    command = getattr(args.command, "value", args.command)
    database = getattr(args.database, "value", args.database)
    if PG_CONN:
        was = args.connectionString
        args.connectionString = PG_CONN
        print(
            f"[viewer] DB force connection={connection!r} source={src!r} "
            f"command={command!r} was={mask_pg_conn(was or '')!r} "
            f"now={mask_pg_conn(PG_CONN)!r} "
            f"database={database!r}",
            flush=True,
        )
    else:
        print(
            f"[viewer] DB MISSING .env — connection={connection!r} source={src!r} "
            f"command={command!r} conn={mask_pg_conn(args.connectionString or '')!r}",
            flush=True,
        )

    # то, что прислал клиент (часто уже без @param — значения вшиты)
    query_from_client = args.queryString or ""
    query = query_from_client
    # шаблон ТОЛЬКО из .mrt (никогда не подменяем клиентским SQL)
    mrt_meta = _lookup_sql_meta(src)
    query_template = (mrt_meta.get("sql") if mrt_meta else None) or _lookup_sql_template(
        src
    ) or ""
    template_at = sorted(_sql_at_names(query_template)) if query_template else []
    template_braces = sorted(_sql_brace_names(query_template)) if query_template else []
    client_already_filled = bool(query_from_client) and not _sql_at_names(
        query_from_client
    )
    same_as_client = bool(
        query_template
        and query_from_client
        and query_template.strip() == query_from_client.strip()
    )
    available_keys = sorted({k for k in SOURCE_SQL_TEMPLATES if k})[:40]
    if not query_template:
        template_note = (
            f"для source={src!r} в .mrt НЕ найден SqlCommand. "
            f"Доступные Name/Alias: {available_keys}. "
            "Блок A пустой — заполненный SQL только в B (от клиента)."
        )
    elif same_as_client:
        template_note = (
            "A == B: текст в .mrt совпадает с тем, что прислал клиент. "
            "Либо в SqlCommand уже литералы (нет @param), либо клиент не менял запрос."
        )
    elif not template_at and not template_braces:
        template_note = (
            "в A (SqlCommand из .mrt) НЕТ @param и нет {Variable}. "
            "Запрос в файле уже с литералами или без фильтров. "
            "sql_parameters ниже — это то, что клиент прислал ОТДЕЛЬНО; "
            "они могли НЕ попасть в текст A. Смотри B — что реально ушло в PG."
        )
    elif template_at and client_already_filled:
        template_note = (
            "нормально: A = шаблон с @param из .mrt; B = клиент уже подставил значения."
        )
    elif template_braces and not template_at:
        template_note = (
            "в A есть {Variable}, но нет @param — Stimulsoft-выражения в SQL."
        )
    else:
        template_note = "см. A/B/C ниже."

    params = args.parameters or {}
    param_dump: dict[str, object] = {}
    selected_sql: dict[str, object] = {}
    had_empty = False
    for name, param in params.items():
        param_dump[name] = {
            "typeGroup": param.typeGroup,
            "typeName": param.typeName,
            "value": param.value,
        }
        selected_sql[name] = param.value
        print(
            f"[viewer]   @{name} typeGroup={param.typeGroup!r} "
            f"typeName={param.typeName!r} value={param.value!r}",
            flush=True,
        )
        if not _is_empty(param.value):
            continue
        had_empty = True
        kind = "number" if _is_number_param(param) else (param.typeGroup or "value")
        query = _soften_empty_param(query, name)
        if _is_number_param(param) or _type_looks_numeric(
            str(param.typeName or ""), name
        ):
            param.typeGroup = "number"
            param.value = 0
        else:
            param.value = "*"
        param_dump[name]["value_after"] = param.value
        selected_sql[name] = param.value
        print(
            f"[viewer] empty @{name} ({kind}) on {src} → soften + default={param.value!r}",
            flush=True,
        )

    if had_empty or "''" in (query or ""):
        before = query
        query = _soften_inlined_empty_literals(query)
        if "''" in query:
            query = query.replace("''", "NULL")
        if query != before:
            print(f"[viewer] softened inlined '' on {src}", flush=True)

    if query != query_from_client:
        args.queryString = query

    # 1/3 — что выбрали (панель + sql params)
    print(
        f"[viewer][1/3 PARAMETERS] source={src!r} "
        f"panel={LAST_VARIABLES} sql_params={selected_sql}",
        flush=True,
    )

    # 2/3 — A .mrt / B клиент / C filled preview
    if template_at:
        filled = _filled_sql_preview(query_template, params)
    else:
        filled = query  # нечего подставлять в шаблон — уйдёт то, что после soften
    print(
        f"[viewer][2/3 SQL] source={src!r} connection={connection!r}\n"
        f"  note: {template_note}\n"
        f"  A .mrt:      {(query_template or '(нет)')[:400].replace(chr(10), ' ')}\n"
        f"  B client:    {query_from_client[:400].replace(chr(10), ' ')}\n"
        f"  C filled:    {filled[:400].replace(chr(10), ' ')}",
        flush=True,
    )

    _sql_log_add(
        {
            "phase": "sql",
            "step": "2/3",
            "connection": connection,
            "source": src,
            "command": command,
            "database": str(database),
            "pg_forced": bool(PG_CONN),
            "client_already_filled": client_already_filled,
            "template_same_as_client": same_as_client,
            "template_has_at": bool(template_at),
            "template_at_names": template_at,
            "template_brace_names": template_braces,
            "template_note": template_note,
            "mrt_path": str(MRT_PATH.resolve()),
            "mrt_source_name": (mrt_meta or {}).get("name"),
            "mrt_parameter_names": (mrt_meta or {}).get("parameter_names") or [],
            "lookup_keys_sample": available_keys,
            "panel_selected": dict(LAST_VARIABLES),
            "sql_parameters": param_dump,
            "query_template": (query_template or "")[:4000],
            "query_from_client": query_from_client[:4000],
            "query_filled": filled[:4000],
            "query": (args.queryString or "")[:4000],
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    )


def end_process_data(args: StiDataEventArgs) -> None:
    """3/3 — результат SQL."""
    src = args.dataSource or "?"
    connection = args.connection or "?"
    command = getattr(args.command, "value", args.command)
    result = args.result
    success = getattr(result, "success", None) if result is not None else None
    notice = getattr(result, "notice", None) if result is not None else None
    rows = None
    sample = None
    try:
        data = getattr(result, "data", None) if result is not None else None
        if data is not None:
            rows = len(data)
            # первые строки для лога (если список)
            if isinstance(data, list) and data:
                sample = data[:3]
            elif hasattr(data, "__getitem__") and rows:
                try:
                    sample = [data[i] for i in range(min(3, rows))]
                except Exception:
                    sample = None
    except Exception:
        rows = None
    # SQL из предыдущего шага 2/3 для этого source
    filled = None
    for item in SQL_LOG:
        if item.get("phase") == "sql" and item.get("source") == src:
            filled = item.get("query_filled") or item.get("query")
            break
    entry = {
        "phase": "result",
        "step": "3/3",
        "connection": connection,
        "source": src,
        "command": command,
        "success": success,
        "notice": notice,
        "rows": rows,
        "sample": sample,
        "query_filled": filled,
    }
    _sql_log_add(entry)
    print(
        f"[viewer][3/3 RESULT] source={src!r} success={success} rows={rows} "
        f"notice={notice!r}",
        flush=True,
    )
    if success is False and notice:
        print(f"[viewer][3/3 RESULT] ERROR sql={filled!r}", flush=True)


@app.get("/mrt")
def serve_mrt():
    """Отдать .mrt с RequestParameters/Dialog — иначе viewer без Submit."""
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
        "Пишется в файл SQL_LOG_PATH (по умолчанию sql_trace.txt) и в /debug.sql_log. "
        "Шаги: parameters (1/3) → sql (2/3 template+filled) → result (3/3)."
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


def main() -> None:
    info = dashboard_debug(MRT_PATH)
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    vers = package_versions()
    _reset_sql_trace_file()
    _refresh_sql_templates()
    print(f"MRT:     {MRT_PATH.resolve()}")
    print(f"sql_log: {SQL_LOG_PATH.resolve()}")
    print(f"sti:     {vers}")
    print(f"mode:    ParametersDisplayMode={FORCE_PARAMETERS_MODE}")
    print(f"anon:    {info.get('anonymized')}")
    print(f"RFU:     {info.get('request_from_user_count')} variables")
    print(f"panel:   {info.get('show_on_parameters_panel_count')} ShowOnParametersPanel")
    print(f"filters: {info.get('filter_elements_count')} canvas elements")
    print(
        f"sources: {info.get('data_sources_count')} "
        f"(sql={info.get('sql_sources_count')}) "
        f"db={info.get('databases_count')}"
    )
    if info.get("sql_sources"):
        print(
            "sql:",
            [
                f"{s.get('name')}→{s.get('database') or '?'}"
                for s in info["sql_sources"][:12]
            ],
        )
    if info.get("show_on_parameters_panel_variables"):
        print(
            "panel names:",
            [v.get("name") for v in info["show_on_parameters_panel_variables"]],
        )
    elif info.get("request_from_user_variables"):
        print(
            "RFU names:",
            [v.get("name") for v in info["request_from_user_variables"]],
        )
    if info.get("filter_elements_on_canvas"):
        print(
            "canvas:",
            [
                f"{e.get('type')}:{e.get('name')}"
                for e in info["filter_elements_on_canvas"]
            ],
        )
    db = probe_database()
    print(f"DB:      configured={db.get('configured')} ok={db.get('ok')}")
    print(f"DB conn: {db.get('pg_conn_masked') or '(empty)'}")
    if db.get("error"):
        print(f"DB ERR:  {db.get('error')}", flush=True)
    elif db.get("ok"):
        print(f"DB user: {db.get('user')} @ {db.get('database')}", flush=True)
    print(f"Open:    {url}")
    print(f"Debug:   {url}debug")
    if not db.get("configured"):
        print(
            "WARNING: нет Postgres в .env — DialogInfo Columns и SQL не смогут "
            "подтянуть данные. Задай POSTGRES_* или DATABASE_URL.",
            flush=True,
        )
    if info.get("anonymized"):
        print(
            "WARNING: .mrt обезличен (пустые строки) — фильтров не будет. "
            "Запусти: python view_dashboard.py /path/to/full.mrt",
            flush=True,
        )
    if OPEN_BROWSER:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    app.run(host=VIEWER_HOST, port=VIEWER_PORT, debug=False)


if __name__ == "__main__":
    main()
