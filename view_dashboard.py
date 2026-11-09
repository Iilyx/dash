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

FORCE_REQUEST_PARAMETERS=1 — ждать Submit (передать выбранные фильтры в SQL)
FORCE_ALLOW_SQL_PARAMETER=1 — AllowUseAsSqlParameter у RFU (иначе @param пустой)
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
from stimulsoft_data_adapters.classes.StiParameter import StiParameter
from stimulsoft_data_adapters.enums import StiDatabaseType
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

# Последние значения RFU (prepareVariables + /panel-vars) → SQL @params
LAST_VARIABLES: dict[str, object] = {}
PANEL_VARS: dict[str, object] = {}
# Оригинальные SqlCommand из .mrt (имя источника → SQL с @param)
SOURCE_SQL_TEMPLATES: dict[str, str] = {}

_FILE_DB_IDENTS = (
    "Excel",
    "Csv",
    "JSON",
    "Xml",
    "File",
    "DBase",
    "Gis",
    "Firebase",
    "GoogleSheets",
    "AzureBlob",
    "AzureTable",
)
_FILE_DB_TYPES = {
    StiDatabaseType.XML,
    StiDatabaseType.JSON,
    StiDatabaseType.CSV,
    "XML",
    "JSON",
    "CSV",
}
_SQL_DB_IDENT_HINTS = (
    "Sql",
    "Postgre",
    "MySql",
    "Maria",
    "Oracle",
    "Firebird",
    "Odbc",
    "ClickHouse",
    "Redshift",
    "DB2",
    "Informix",
    "Snowflake",
)

ROOT = Path(__file__).resolve().parent
# Последние SQL/data-запросы к handler (видно в /debug → sql_log)
SQL_LOG: deque[dict] = deque(maxlen=80)

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

# RequestParameters=true → SQL после Submit (с выбранными фильтрами).
# По умолчанию 1: иначе первый запрос уходит с пустыми @param (soften→TRUE),
# и кажется, что выбранное «не передаётся» в источники.
FORCE_REQUEST_PARAMETERS = os.environ.get("FORCE_REQUEST_PARAMETERS", "1").lower() in {
    "1",
    "true",
    "yes",
}
FORCE_ALLOW_SQL_PARAMETER = os.environ.get("FORCE_ALLOW_SQL_PARAMETER", "1").lower() in {
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
          // иначе клиент не кладёт переменную в parameters{{}} запроса
          try {{ v.allowUseAsSqlParameter = true; }} catch (eA) {{}}
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
window.__stiPushPanelVars = function (varsObj, sync) {{
  try {{
    if (!varsObj) {{
      var v = window.__stiFindViewer();
      varsObj = v && v.options && v.options.paramsVariables;
    }}
    if (!varsObj) return;
    window.__stiLastPanelVars = varsObj;
    var body = JSON.stringify(varsObj);
    // sync на Submit: иначе ExecuteQuery уходит раньше, чем /panel-vars
    if (sync) {{
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/panel-vars', false);
      xhr.setRequestHeader('Content-Type', 'application/json');
      try {{ xhr.send(body); }} catch (eSync) {{ console.warn('[viewer] panel-vars sync', eSync); }}
      console.log('[viewer] panel-vars sync status', xhr.status, xhr.responseText);
      return;
    }}
    fetch('/panel-vars', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: body,
      credentials: 'same-origin',
      keepalive: true
    }}).then(function (r) {{ return r.json(); }}).then(function (j) {{
      console.log('[viewer] panel-vars pushed', j);
    }}).catch(function (e) {{
      console.warn('[viewer] panel-vars', e);
    }});
  }} catch (e0) {{
    console.warn('[viewer] push panel vars', e0);
  }}
}};
window.__stiOnInteraction = function (args) {{
  try {{
    if (!args) return;
    var act = args.action || '';
    if (act === 'Variables' || act === 'variables' || act === 'DashboardElementApplyEvents') {{
      var varsObj = args.variables;
      if (!varsObj) {{
        var vv = window.__stiFindViewer();
        varsObj = vv && vv.options && vv.options.paramsVariables;
      }}
      window.__stiPushPanelVars(varsObj, true);
    }}
  }} catch (e) {{
    console.warn('[viewer] onInteraction', e);
  }}
}};
window.__stiAfterInit = function (args) {{
  var tries = 0;
  var timer = setInterval(function () {{
    tries += 1;
    if (window.__stiForceParametersPanel() || tries > 80) {{
      clearInterval(timer);
      setInterval(window.__stiKeepParametersPanel, 1000);
      window.__stiPushPanelVars();
      setInterval(function () {{ window.__stiPushPanelVars(null, false); }}, 2000);
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
        "force_allow_sql_parameter": FORCE_ALLOW_SQL_PARAMETER,
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
            "SQL DataSources → PostgreSQL через patch Ident/NameInSource + "
            "onBeginProcessData (database=PostgreSQL, connection из .env). "
            "Фильтры: AllowUseAsSqlParameter + Submit (RequestParameters) + "
            "inject из onPrepareVariables. Смотри sql_log.parameters / injected_from_variables."
        ),
    }


def mrt_has_request_from_user(report: dict) -> bool:
    dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}
    for variable in numbered(dictionary.get("Variables")):
        if isinstance(variable, dict) and variable_is_parameters_panel_candidate(variable):
            return True
    return False


def _sql_at_names(query: str) -> set[str]:
    if not query:
        return set()
    return {m.group(1) for m in re.finditer(r"[@:]([A-Za-z_][A-Za-z0-9_]*)", query)}


def _collect_sql_at_names(dictionary: dict) -> set[str]:
    names: set[str] = set()
    for source in numbered(dictionary.get("DataSources")):
        if not isinstance(source, dict):
            continue
        sql = (
            source.get("SqlCommand")
            or source.get("Query")
            or source.get("SqlQuery")
            or ""
        )
        names |= _sql_at_names(str(sql))
    return names


def _param_type_group_for_value(value: object, name: str = "") -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "number"
    if _type_looks_numeric("", name) and not isinstance(value, (list, dict)):
        return "number"
    return "string"


def _merged_panel_values() -> dict[str, object]:
    """PANEL_VARS (с JS Submit) перекрывают LAST_VARIABLES (prepareVariables)."""
    merged = dict(LAST_VARIABLES)
    merged.update({k: v for k, v in PANEL_VARS.items() if not _is_empty(v)})
    return merged


def _lookup_sql_template(data_source: str | None) -> str:
    if not data_source:
        return ""
    if data_source in SOURCE_SQL_TEMPLATES:
        return SOURCE_SQL_TEMPLATES[data_source]
    lower = data_source.lower()
    for name, sql in SOURCE_SQL_TEMPLATES.items():
        if name.lower() == lower:
            return sql
    return ""


def _refresh_sql_templates(report: dict | None = None) -> dict[str, str]:
    """Запомнить SqlCommand с @param из .mrt — клиент часто присылает уже без @ и parameters={}."""
    global SOURCE_SQL_TEMPLATES
    try:
        if report is None:
            report = load_mrt(MRT_PATH)
    except Exception as exc:
        print(f"[viewer] sql templates load: {exc}", flush=True)
        return SOURCE_SQL_TEMPLATES
    dictionary = report.get("Dictionary") if isinstance(report, dict) else {}
    templates: dict[str, str] = {}
    for source in numbered((dictionary or {}).get("DataSources")):
        if not isinstance(source, dict):
            continue
        name = (source.get("Name") or "").strip()
        sql = (
            source.get("SqlCommand")
            or source.get("Query")
            or source.get("SqlQuery")
            or ""
        )
        sql = str(sql).strip()
        if name and sql and _sql_at_names(sql):
            templates[name] = sql
    SOURCE_SQL_TEMPLATES = templates
    print(
        f"[viewer] sql templates: {len(templates)} sources with @params "
        f"{list(templates)[:12]}",
        flush=True,
    )
    return templates


def _normalize_panel_payload(raw: object) -> dict[str, object]:
    """paramsVariables / onInteraction → плоский dict имя→значение."""
    out: dict[str, object] = {}
    if not isinstance(raw, dict):
        return out
    for key, item in raw.items():
        if key in ("event", "action", "sender"):
            continue
        if isinstance(item, dict):
            if item.get("isCategory") is True:
                continue
            name = item.get("name") or item.get("Name") or key
            if "value" in item:
                value = item.get("value")
            elif "Value" in item:
                value = item.get("Value")
            else:
                value = item.get("key", item.get("Key"))
            if name:
                out[str(name)] = value
        else:
            out[str(key)] = item
    return out


def _inject_variables_into_sql_params(
    args: StiDataEventArgs, query: str
) -> tuple[str, list[str], bool]:
    """Вернуть (query, injected_names, restored_template).

    Клиент часто шлёт parameters={} и SQL уже без @param (пустые литералы).
    Тогда берём шаблон из .mrt и заполняем @param из панели.
    """
    injected: list[str] = []
    restored = False
    values = _merged_panel_values()
    template = _lookup_sql_template(args.dataSource)
    at_in_query = _sql_at_names(query)
    at_in_template = _sql_at_names(template) if template else set()

    # Restore: query без @ / parameters={} → шаблон из .mrt с @param
    if template and at_in_template:
        params_empty = not args.parameters
        values_lower = {k.lower(): v for k, v in values.items()}
        has_values_for_template = any(
            not _is_empty(values_lower.get(n.lower())) for n in at_in_template
        )
        if (not at_in_query and at_in_template) or (
            params_empty and has_values_for_template
        ):
            if query.strip() != template.strip():
                print(
                    f"[viewer] restore SQL template for {args.dataSource!r} "
                    f"(@params={sorted(at_in_template)})",
                    flush=True,
                )
            query = template
            restored = True
            at_in_query = set(at_in_template)

    if not values and not at_in_query:
        return query, injected, restored

    params = args.parameters
    if params is None:
        params = {}
        args.parameters = params
    at_lower = {n.lower(): n for n in at_in_query}
    params_lower = {str(k).lower(): k for k in params}

    # Создаём/заполняем param для каждого @ в query из values
    for at_name in list(at_in_query):
        key_lower = at_name.lower()
        value = None
        for vk, vv in values.items():
            if vk.lower() == key_lower and not _is_empty(vv):
                value = vv
                break
        if value is None:
            continue
        param_key = params_lower.get(key_lower)
        existing = params.get(param_key) if param_key is not None else None
        if existing is not None and not _is_empty(existing.value) and not restored:
            continue
        group = _param_type_group_for_value(value, at_name)
        if existing is None:
            params[at_name] = StiParameter(at_name, 0, group, group, 0, value)
            injected.append(at_name)
        else:
            if not existing.typeGroup:
                existing.typeGroup = group
            existing.value = value
            injected.append(str(param_key))
        print(
            f"[viewer] inject @{injected[-1]}={value!r} into {args.dataSource!r}",
            flush=True,
        )

    # Также values→params даже если имя только в values и в params пустое
    for var_name, value in values.items():
        if _is_empty(value):
            continue
        key_lower = var_name.lower()
        if key_lower not in at_lower and key_lower not in params_lower:
            continue
        if key_lower in {i.lower() for i in injected}:
            continue
        param_key = params_lower.get(key_lower)
        existing = params.get(param_key) if param_key is not None else None
        use_name = at_lower.get(key_lower, param_key or var_name)
        if existing is not None and not _is_empty(existing.value) and not restored:
            continue
        group = _param_type_group_for_value(value, var_name)
        if existing is None:
            params[use_name] = StiParameter(use_name, 0, group, group, 0, value)
            injected.append(use_name)
        else:
            if not existing.typeGroup:
                existing.typeGroup = group
            existing.value = value
            injected.append(str(param_key))
        print(
            f"[viewer] inject @{injected[-1]}={value!r} into {args.dataSource!r}",
            flush=True,
        )

    return query, injected, restored


def _is_file_database(ident: str) -> bool:
    return any(h in (ident or "") for h in _FILE_DB_IDENTS)


def _source_looks_sql(source: dict) -> bool:
    if not isinstance(source, dict):
        return False
    ident = str(source.get("Ident") or "")
    if any(h in ident for h in _FILE_DB_IDENTS) or "DataTable" in ident:
        return False
    has_sql_key = any(k in source for k in ("SqlCommand", "Query", "SqlQuery", "Parameters"))
    has_sql = bool(source.get("SqlCommand") or source.get("Query") or source.get("SqlQuery"))
    return (
        "Sql" in ident
        or "Postgre" in ident
        or has_sql
        or has_sql_key
        or bool(source.get("NameInSource"))
        or bool(source.get("Connection"))
    )


def _patch_postgres_dictionary(dictionary: dict) -> dict:
    """Ident/NameInSource → PostgreSQL, иначе handler: Unknown database type / нет ExecuteQuery."""
    meta = {
        "db_ident_forced": 0,
        "db_named": 0,
        "source_ident_forced": 0,
        "name_in_source_forced": 0,
        "connect_on_start_forced": 0,
        "primary_connection": None,
        "changed": False,
    }
    if not isinstance(dictionary, dict) or not PG_CONN:
        return meta

    sql_dbs: list[dict] = []
    for db in numbered(dictionary.get("Databases")):
        if not isinstance(db, dict):
            continue
        ident = str(db.get("Ident") or "")
        if _is_file_database(ident):
            continue
        looks_sql = (
            not ident
            or any(h in ident for h in _SQL_DB_IDENT_HINTS)
            or "ConnectionString" in db
            or bool(db.get("ConnectionString"))
        )
        if not looks_sql:
            continue
        if ident != "StiPostgreSQLDatabase":
            db["Ident"] = "StiPostgreSQLDatabase"
            meta["db_ident_forced"] += 1
            meta["changed"] = True
        # Клиенту нужен Recognizable PostgreSQL connection string; пароль подставит handler
        if not (db.get("ConnectionString") or "").strip():
            url = database_url()
            parsed = urlparse(url) if url else None
            host = (parsed.hostname if parsed else None) or "127.0.0.1"
            port = (parsed.port if parsed else None) or 5432
            database = (
                parsed.path.lstrip("/").split("?", 1)[0] if parsed and parsed.path else "app"
            )
            db["ConnectionString"] = (
                f"Host={host}; Port={port}; Database={database}; User Id=; Password=;"
            )
            meta["changed"] = True
        if db.get("ConnectionStringEncrypted"):
            db["ConnectionStringEncrypted"] = ""
            meta["changed"] = True
        sql_dbs.append(db)

    primary = next(
        ((db.get("Name") or "").strip() for db in sql_dbs if (db.get("Name") or "").strip()),
        "",
    )
    if sql_dbs and not primary:
        primary = "Connection"
        sql_dbs[0]["Name"] = primary
        if not (sql_dbs[0].get("Alias") or "").strip():
            sql_dbs[0]["Alias"] = primary
        meta["db_named"] += 1
        meta["changed"] = True
    meta["primary_connection"] = primary or None
    db_names = {
        (db.get("Name") or "").strip()
        for db in sql_dbs
        if (db.get("Name") or "").strip()
    }

    # Если SQL-источники есть, а Databases пуст — создаём Connection
    sql_sources = [
        s for s in numbered(dictionary.get("DataSources")) if _source_looks_sql(s)
    ]
    if sql_sources and not sql_dbs:
        primary = "Connection"
        databases = dictionary.get("Databases")
        if not isinstance(databases, dict):
            databases = {}
            dictionary["Databases"] = databases
        next_key = str(len(databases))
        databases[next_key] = {
            "Ident": "StiPostgreSQLDatabase",
            "Name": primary,
            "Alias": primary,
            "ConnectionString": "",
        }
        sql_dbs = [databases[next_key]]
        db_names = {primary}
        meta["primary_connection"] = primary
        meta["db_ident_forced"] += 1
        meta["db_named"] += 1
        meta["changed"] = True

    for source in sql_sources:
        ident = str(source.get("Ident") or "")
        if ident != "StiPostgreSQLSource":
            source["Ident"] = "StiPostgreSQLSource"
            meta["source_ident_forced"] += 1
            meta["changed"] = True
        nis = (source.get("NameInSource") or "").strip()
        if primary and (not nis or (db_names and nis not in db_names and len(db_names) == 1)):
            if nis != primary:
                source["NameInSource"] = primary
                meta["name_in_source_forced"] += 1
                meta["changed"] = True
        if source.get("ConnectOnStart") is False or flag_false(source.get("ConnectOnStart")):
            source["ConnectOnStart"] = True
            meta["connect_on_start_forced"] += 1
            meta["changed"] = True
        elif "ConnectOnStart" not in source or not flag_true(source.get("ConnectOnStart")):
            source["ConnectOnStart"] = True
            meta["connect_on_start_forced"] += 1
            meta["changed"] = True

    return meta


def analyze_source_issues(report: dict) -> dict:
    """Диагностика Dictionary: пустой Ident, битый NameInSource, пустой SqlCommand."""
    dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}
    dbs = [d for d in numbered(dictionary.get("Databases")) if isinstance(d, dict)]
    sources = [s for s in numbered(dictionary.get("DataSources")) if isinstance(s, dict)]
    db_names = {(d.get("Name") or "").strip() for d in dbs if (d.get("Name") or "").strip()}
    issues: list[str] = []
    sql_sources = []
    for src in sources:
        if not _source_looks_sql(src):
            continue
        name = (src.get("Name") or "").strip() or "(unnamed)"
        ident = str(src.get("Ident") or "")
        nis = (src.get("NameInSource") or "").strip()
        sql = (src.get("SqlCommand") or src.get("Query") or src.get("SqlQuery") or "").strip()
        entry = {
            "name": name,
            "ident": ident or None,
            "name_in_source": nis or None,
            "has_sql": bool(sql),
            "sql_preview": sql[:160] if sql else None,
            "connect_on_start": src.get("ConnectOnStart"),
        }
        sql_sources.append(entry)
        if not ident:
            issues.append(f"{name}: пустой Ident (нужен StiPostgreSQLSource)")
        elif "Postgre" not in ident and "Sql" not in ident:
            issues.append(f"{name}: Ident={ident!r} — не SQL/PostgreSQL")
        if not nis:
            issues.append(f"{name}: пустой NameInSource (имя Databases.Name)")
        elif db_names and nis not in db_names:
            issues.append(
                f"{name}: NameInSource={nis!r} не совпадает с Databases {sorted(db_names)}"
            )
        if not sql:
            issues.append(f"{name}: пустой SqlCommand")
    for db in dbs:
        dname = (db.get("Name") or "").strip() or "(unnamed)"
        ident = str(db.get("Ident") or "")
        if _is_file_database(ident):
            continue
        if not ident:
            issues.append(f"Database {dname}: пустой Ident (нужен StiPostgreSQLDatabase)")
        elif "Postgre" not in ident and any(h in ident for h in _SQL_DB_IDENT_HINTS):
            issues.append(f"Database {dname}: Ident={ident!r} — не PostgreSQL")
        if not (db.get("Name") or "").strip():
            issues.append(f"Database: пустое Name (источники не привяжутся)")
    return {
        "databases": [
            {
                "name": (d.get("Name") or "") or None,
                "ident": (d.get("Ident") or "") or None,
                "has_connection_string": bool(d.get("ConnectionString")),
            }
            for d in dbs
        ],
        "sql_sources": sql_sources,
        "issues": issues,
        "ok": len(issues) == 0 and bool(sql_sources),
    }


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
    if not isinstance(report.get("Dictionary"), dict):
        report["Dictionary"] = dictionary

    # Databases/DataSources → StiPostgreSQL* + NameInSource + ConnectOnStart
    pg_meta = _patch_postgres_dictionary(dictionary)
    if pg_meta.get("changed"):
        changed = True
    meta.update(
        {
            "connect_on_start_forced": pg_meta.get("connect_on_start_forced", 0),
            "db_ident_forced": pg_meta.get("db_ident_forced", 0),
            "db_named": pg_meta.get("db_named", 0),
            "source_ident_forced": pg_meta.get("source_ident_forced", 0),
            "name_in_source_forced": pg_meta.get("name_in_source_forced", 0),
            "primary_connection": pg_meta.get("primary_connection"),
        }
    )
    meta["source_issues"] = analyze_source_issues(report)
    rfu_forced = 0
    show_forced = 0
    sql_param_forced = 0
    columns_softened = 0
    defaults_set = 0
    candidates = 0
    sql_at = _collect_sql_at_names(dictionary)
    for variable in numbered(dictionary.get("Variables")):
        if not isinstance(variable, dict):
            continue
        name = str(variable.get("Name") or "")
        is_panel = variable_is_parameters_panel_candidate(variable)
        used_in_sql = bool(name) and name.lower() in {n.lower() for n in sql_at}
        # AllowUseAsSqlParameter — иначе движок не кладёт переменную в @param источника
        if FORCE_ALLOW_SQL_PARAMETER and (is_panel or used_in_sql):
            if not flag_true(variable.get("AllowUseAsSqlParameter")):
                variable["AllowUseAsSqlParameter"] = True
                sql_param_forced += 1
                changed = True
        if not is_panel:
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
    meta["allow_sql_parameter_forced"] = sql_param_forced
    meta["sql_at_names"] = sorted(sql_at)
    meta["columns_dialog_softened"] = columns_softened
    meta["empty_defaults_set"] = defaults_set
    meta["has_request_from_user"] = candidates > 0 or meta["has_request_from_user"]

    meta["patched"] = changed
    if not changed:
        return raw, meta
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n", meta


def prepare_variables(args: StiVariablesEventArgs) -> None:
    """Лог RFU + кэш значений для SQL. Пустые с DialogInfo не трогаем (First)."""
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

    cached: dict[str, object] = {}
    for name, var in variables.items():
        vtype = var.type.value if getattr(var, "type", None) is not None else "?"
        empty = _is_empty(var.value)
        print(
            f"[viewer]   var {name} type={vtype} empty={empty} value={var.value!r} "
            f"dialog_list={name in dialog_list_names}",
            flush=True,
        )
        if not empty:
            cached[str(name)] = var.value
            continue
        if name in dialog_list_names:
            # Stimulsoft выберет First после fillsDialogInfoItems — не затирать 0/*
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
        cached[str(name)] = var.value
        print(f"[viewer]   var {name}: empty → {var.value!r}", flush=True)
    LAST_VARIABLES = cached
    # также в PANEL_VARS — чтобы beginProcessData видел значения даже без /panel-vars
    global PANEL_VARS
    merged_panel = dict(PANEL_VARS)
    for key, value in cached.items():
        if _is_empty(value) and key in merged_panel and not _is_empty(merged_panel.get(key)):
            continue
        merged_panel[key] = value
    PANEL_VARS = merged_panel
    print(f"[viewer] cached variables for SQL: {cached}", flush=True)


def _sql_log_add(entry: dict) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **entry,
    }
    SQL_LOG.appendleft(entry)
    print(f"[viewer][sql] {entry}", flush=True)


def _database_type_value(database) -> object:
    return getattr(database, "value", database)


def begin_process_data(args: StiDataEventArgs) -> None:
    """PostgreSQL adapter + connection из .env; пустые params → не ''."""
    t0 = time.perf_counter()
    src = args.dataSource or "?"
    connection = args.connection or "?"
    command = getattr(args.command, "value", args.command)
    database = _database_type_value(args.database)
    db_forced = False
    # Важно: пустой/чужой Ident → Unknown database type. Форсим PostgreSQL до getDataAdapter.
    if PG_CONN and database not in _FILE_DB_TYPES and _database_type_value(database) not in (
        "XML",
        "JSON",
        "CSV",
    ):
        was = args.connectionString
        args.connectionString = PG_CONN
        if args.database != StiDatabaseType.POSTGRESQL:
            args.database = StiDatabaseType.POSTGRESQL
            db_forced = True
        database = _database_type_value(args.database)
        print(
            f"[viewer] DB force connection={connection!r} source={src!r} "
            f"command={command!r} was={mask_pg_conn(was or '')!r} "
            f"now={mask_pg_conn(PG_CONN)!r} "
            f"database={database!r} db_forced={db_forced}",
            flush=True,
        )
    else:
        print(
            f"[viewer] DB skip/missing — connection={connection!r} source={src!r} "
            f"command={command!r} database={database!r} "
            f"conn={mask_pg_conn(args.connectionString or '')!r} pg={bool(PG_CONN)}",
            flush=True,
        )

    query = args.queryString or ""
    qpreview = query[:180].replace("\n", " ")
    print(
        f"[viewer] SQL source={src} params={len(args.parameters or {})} "
        f"q={qpreview!r}",
        flush=True,
    )

    query, injected, restored = _inject_variables_into_sql_params(args, query)
    if restored or query != (args.queryString or ""):
        args.queryString = query

    params = args.parameters or {}
    param_dump = {}
    had_empty = False
    # Не смягчаем @param, если только что восстановили шаблон и подставили значения
    for name, param in params.items():
        param_dump[name] = {
            "typeGroup": param.typeGroup,
            "typeName": param.typeName,
            "value": param.value,
        }
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

    if query != (args.queryString or ""):
        args.queryString = query
        print(
            f"[viewer] SQL after soften q={(query[:180]).replace(chr(10), ' ')!r}",
            flush=True,
        )

    merged = _merged_panel_values()
    _sql_log_add(
        {
            "phase": "begin",
            "connection": connection,
            "source": src,
            "command": command,
            "database": str(database),
            "pg_forced": bool(PG_CONN),
            "database_forced": db_forced,
            "template_restored": restored,
            "injected_from_variables": injected,
            "panel_variables": {k: merged.get(k) for k in list(merged)[:40]},
            "query": (args.queryString or "")[:2000],
            "parameters": param_dump,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    )


def end_process_data(args: StiDataEventArgs) -> None:
    """Лог результата SQL — чтобы было видно, ходили ли в базу."""
    src = args.dataSource or "?"
    connection = args.connection or "?"
    command = getattr(args.command, "value", args.command)
    result = args.result
    success = getattr(result, "success", None) if result is not None else None
    notice = getattr(result, "notice", None) if result is not None else None
    rows = None
    try:
        data = getattr(result, "data", None) if result is not None else None
        if data is not None:
            rows = len(data)
    except Exception:
        rows = None
    entry = {
        "phase": "end",
        "connection": connection,
        "source": src,
        "command": command,
        "success": success,
        "notice": notice,
        "rows": rows,
    }
    _sql_log_add(entry)
    print(
        f"[viewer] SQL done source={src!r} success={success} rows={rows} notice={notice!r}",
        flush=True,
    )


@app.get("/mrt")
def serve_mrt():
    """Отдать .mrt с RequestParameters/Dialog — иначе viewer без Submit."""
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    patched, meta = patch_mrt_for_parameters_panel(raw)
    try:
        _refresh_sql_templates(json.loads(patched))
    except Exception as exc:
        print(f"[viewer] refresh templates: {exc}", flush=True)
        _refresh_sql_templates()
    print(f"[viewer] serve /mrt patch={meta}", flush=True)
    resp = Response(patched, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/panel-vars")
def panel_vars():
    """Клиент шлёт выбранные фильтры с панели — кладём в PANEL_VARS для SQL."""
    global PANEL_VARS
    payload = request.get_json(silent=True)
    if payload is None and request.data:
        try:
            payload = json.loads(request.data.decode("utf-8"))
        except Exception:
            payload = None
    normalized = _normalize_panel_payload(payload or {})
    # не затираем известные значения пустыми
    merged = dict(PANEL_VARS)
    for key, value in normalized.items():
        if _is_empty(value) and key in merged and not _is_empty(merged.get(key)):
            continue
        merged[key] = value
    PANEL_VARS = merged
    print(f"[viewer] /panel-vars ← {PANEL_VARS}", flush=True)
    return jsonify({"ok": True, "count": len(PANEL_VARS), "variables": PANEL_VARS})


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
    info["panel_vars"] = dict(PANEL_VARS)
    info["last_variables"] = dict(LAST_VARIABLES)
    info["sql_templates"] = {
        name: sql[:200] for name, sql in list(SOURCE_SQL_TEMPLATES.items())[:20]
    }
    info["force_request_parameters"] = FORCE_REQUEST_PARAMETERS
    if FORCE_REQUEST_PARAMETERS:
        info["note_request_parameters"] = (
            "RequestParameters=true: выбери фильтры и нажми Submit — "
            "тогда значения уйдут в SQL @param источников."
        )
    else:
        info["note_request_parameters"] = (
            "RequestParameters=false: первый SQL может уйти с пустыми @param. "
            "После смены фильтров всё равно нужен Submit; "
            "для первого запроса с выбором поставь FORCE_REQUEST_PARAMETERS=1."
        )
    if not SQL_LOG:
        info["note_sql_log_empty"] = (
            "sql_log пуст — handler ещё не получил ExecuteQuery. "
            "Открой / (не только /debug), проверь database.ok и консоль [viewer][sql]."
        )
    source_issues = (meta.get("source_issues") or {}).get("issues") or []
    if source_issues:
        info["source_problems"] = source_issues
        info["note_source_problems"] = (
            "Проблемы Dictionary после patch — пустой SqlCommand/Ident/NameInSource. "
            "Обезличенный test.mrt SQL не выполнит; нужен полный .mrt (MRT_PATH)."
        )
    db_probe = info.get("database") or {}
    if not db_probe.get("ok"):
        info["note_database"] = (
            "Postgres из .env недоступен — источники упадут на connect. "
            f"error={db_probe.get('error')}"
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
    sti.onInteraction += "__stiOnInteraction"
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
    print(f"MRT:     {MRT_PATH.resolve()}")
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
