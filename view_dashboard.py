#!/usr/bin/env python3
"""Отдельный viewer Stimulsoft Dashboards.PYTHON для .mrt.

Документация Stimulsoft (панель + Submit):
  https://stimulsoft.github.io/Stimulsoft.Reports.Dashboards.User.Manual/Dashboards/Data_Filtering/Variables
  https://stimulsoft.github.io/Stimulsoft.Reports.Dashboards.Programming.Manual/Reports_and_Dashboards_for_Python/Web_Viewer/Work_with_Parameters

Чтобы была панель фильтров и кнопка Submit/Apply:
  1) в .mrt у variable: RequestFromUser = true (+ DialogInfo / Items)
  2) filters элементов ссылаются на {Variable}
  3) Report.RequestParameters = true  → не рендерить, пока не нажали Submit
  4) ParametersDisplayMode = Dialog|Panel (Dialog = окно с Submit)

Canvas ComboBox/TreeView — это ДРУГИЕ фильтры: применяются сразу, БЕЗ Submit.

Запуск (путь к .mrt — из .env MRT_PATH):
  # в .env:  MRT_PATH=/path/to/full.mrt
  .venv/bin/python view_dashboard.py
  # http://127.0.0.1:8040/  и  /debug

FORCE_REQUEST_PARAMETERS=1 — RequestParameters=true в отдаваемом .mrt
FORCE_PARAMETERS_PANEL=1 — ParametersDisplayMode=Panel (полоса + Submit)
"""

from __future__ import annotations

import json
import os
import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from flask import Flask, Response, jsonify, request, url_for
from stimulsoft_reports.events import StiDataEventArgs, StiVariablesEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer
from stimulsoft_reports.viewer.enums import StiParametersPanelPosition

from extract_mrt import catalog_from_report, flag_false, flag_true, load_mrt, numbered

ROOT = Path(__file__).resolve().parent

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
    return (
        f"Host={host}; Port={port}; Database={database}; "
        f"User Id={user}; Password={password};"
    )


def resolve_mrt_path() -> Path:
    """Сначала MRT_PATH из .env, опционально переопределение аргументом CLI."""
    env_path = os.environ.get("MRT_PATH", "").strip()
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        return path_of(sys.argv[1])
    if env_path:
        return path_of(env_path)
    return path_of("PatientHealthKPI.mrt")


load_env()

MRT_PATH = resolve_mrt_path()
if not MRT_PATH.is_file():
    raise SystemExit(f"Нет .mrt: {MRT_PATH} — задай MRT_PATH в .env")

VIEWER_HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "8040") or "8040")
OPEN_BROWSER = os.environ.get("VIEWER_OPEN", "1").lower() in {"1", "true", "yes"}

PG_CONN = stimulsoft_pg_connection_string(database_url()) if database_url() else ""

# По докам: RequestParameters + Panel → полоса параметров и кнопка Submit
FORCE_REQUEST_PARAMETERS = os.environ.get("FORCE_REQUEST_PARAMETERS", "1").lower() in {
    "1",
    "true",
    "yes",
}
FORCE_PARAMETERS_PANEL = os.environ.get(
    "FORCE_PARAMETERS_PANEL",
    os.environ.get("FORCE_PARAMETERS_DIALOG", "1"),  # старое имя
).lower() in {"1", "true", "yes"}

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

# JS после открытия отчёта: Panel + RequestParameters + ShowOnParametersPanel
_OPENED_REPORT_JS = """
(function () {
  var r = args.report;
  if (!r) return;
  r.requestParameters = true;
  try {
    r.parametersDisplayMode = Stimulsoft.Report.StiParametersDisplayMode.Panel;
  } catch (e) {
    try { r.parametersDisplayMode = 1; } catch (e2) {}
  }
  // Toolbar + Parameters, без ResetAllFilters (256)
  try {
    r.dashboardViewerSettings = 671;
  } catch (e4) {}
  try {
    var dict = r.dictionary;
    var vars = dict && dict.variables;
    if (!vars) return;
    var count = vars.count || 0;
    for (var i = 0; i < count; i++) {
      var v = vars.getByIndex ? vars.getByIndex(i) : null;
      if (!v && vars.list) v = vars.list[i];
      if (!v) continue;
      if (v.requestFromUser) {
        v.showOnParametersPanel = true;
      }
    }
  } catch (e3) {}
})();
""".strip()

# Раскрыть полосу Parameters + Submit только когда paramsVariables уже пришли.
# Крестик на панели закрывает фильтры — прячем его и сразу открываем панель снова.
_AFTER_INIT_HEAD = """
<style>
  /* не давать свернуть панель параметров крестиком */
  .stiJsViewerParametersPanel [title="Close"],
  .stiJsViewerParametersPanel [title="Закрыть"],
  .stiJsViewerParametersPanel .stiJsViewerClearButton,
  .stiJsViewerParametersPanel button[name="Close"],
  .stiJsViewerParametersPanel .stiJsViewerButtonClose {
    display: none !important;
  }
</style>
<script>
window.__stiFindViewer = function () {
  var names = ['jsViewer', 'jsStiViewer', 'viewer'];
  for (var i = 0; i < names.length; i++) {
    if (window[names[i]] && window[names[i]].controls) return window[names[i]];
  }
  for (var k in window) {
    if (k.indexOf('js') === 0 && window[k] && window[k].controls && window[k].options) {
      return window[k];
    }
  }
  return null;
};
window.__stiCountParams = function (v) {
  var pv = v && v.options && v.options.paramsVariables;
  if (!pv) return 0;
  var n = 0;
  for (var key in pv) {
    // isCategory=true — папка в словаре, не поле фильтра
    if (pv[key] && pv[key].isCategory !== true) n++;
  }
  return n;
};
window.__stiForceParametersPanel = function () {
  var v = window.__stiFindViewer();
  if (!v || !v.controls) return false;
  var count = window.__stiCountParams(v);
  var jo = v.jsObject || v;
  // native API — показать панель из уже собранных variables
  if (count > 0 && typeof jo.showParametersPanel === 'function') {
    try {
      jo.showParametersPanel(v.options.paramsVariables, jo);
    } catch (e0) {}
  }
  if (v.controls.buttons) {
    // ResetAllFilters не нужен без canvas-фильтров
    if (v.controls.buttons.ResetAllFilters) {
      try { v.controls.buttons.ResetAllFilters.style.display = 'none'; } catch (e1) {}
    }
    if (v.controls.buttons.Parameters) {
      try {
        v.controls.buttons.Parameters.setEnabled(count > 0);
        v.controls.buttons.Parameters.style.display = '';
      } catch (e2) {}
    }
  }
  if (count < 1) {
    console.log('[viewer] waiting paramsVariables…');
    return false;
  }
  if (!v.controls.parametersPanel) return false;
  try {
    v.waitingToShowParameters = true;
    v.controls.parametersPanel.changeVisibleState(true);
    if (v.controls.buttons && v.controls.buttons.Parameters) {
      v.controls.buttons.Parameters.setSelected(true);
    }
    console.log('[viewer] parameters panel opened, params=', count, v.options.paramsVariables);
    return true;
  } catch (e) {
    console.warn('[viewer] open parameters panel', e);
    return false;
  }
};
window.__stiKeepParametersPanel = function () {
  var v = window.__stiFindViewer();
  if (!v || !v.controls || !v.controls.parametersPanel) return;
  if (!v.controls.parametersPanel.visible) {
    window.__stiForceParametersPanel();
  }
};
window.__stiAfterInit = function (args) {
  var tries = 0;
  var timer = setInterval(function () {
    tries += 1;
    if (window.__stiForceParametersPanel() || tries > 80) {
      clearInterval(timer);
      setInterval(window.__stiKeepParametersPanel, 1000);
    }
  }, 250);
};
</script>
""".strip()

app = Flask(__name__)


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
    """Пустой @Param → no-op, не '' (PG: invalid input syntax for type integer \"\")."""
    if not query:
        return query
    pat = re.escape(name)
    query = re.sub(
        rf"[\w.\"`]+?\s+IN\s*\(\s*@{pat}\s*\)",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        rf"[\w.\"`]+?\s*=\s*@{pat}\b",
        "TRUE",
        query,
        flags=re.IGNORECASE,
    )
    return re.sub(rf"@{pat}\b", "NULL", query, flags=re.IGNORECASE)


def mrt_looks_anonymized(path: Path) -> bool:
    """Обезличка: все строковые значения пустые."""
    raw = path.read_text(encoding="utf-8-sig")
    nonempty = [v for v in re.findall(r':\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw) if v]
    return len(nonempty) == 0


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
        variables.append(
            {
                "name": var.get("name"),
                "alias": var.get("alias"),
                "type": var.get("type"),
                "category": var.get("category"),
                "value": var.get("value"),
                "request_from_user": bool(var.get("request_from_user")),
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
    return {
        "ok": True,
        "path": str(path.resolve()),
        "anonymized": mrt_looks_anonymized(path),
        "report": catalog.get("report"),
        "request_from_user_variables": request_vars,
        "request_from_user_count": len(request_vars),
        "all_variables_count": len(variables),
        "variables": variables,
        "filter_elements_on_canvas": filter_elements,
        "filter_elements_count": len(filter_elements),
        "hint": (
            "Панель + Submit ← request_from_user_variables "
            "(нужны RequestFromUser=true в .mrt и RequestParameters). "
            "Canvas ComboBox/TreeView ← filter_elements_on_canvas (без Submit)."
        ),
        "force_request_parameters": FORCE_REQUEST_PARAMETERS,
        "force_parameters_panel": FORCE_PARAMETERS_PANEL,
    }


def mrt_has_request_from_user(report: dict) -> bool:
    dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}
    for variable in numbered(dictionary.get("Variables")):
        if isinstance(variable, dict) and flag_true(variable.get("RequestFromUser")):
            return True
    return False


def patch_mrt_for_parameters_panel(raw: str) -> tuple[str, dict]:
    """В .mrt: RequestParameters + Panel + ShowOnParametersPanel для RFU.

    Docs: Request Parameters for Dashboards; Variables → Submit на options bar.
    """
    meta = {
        "patched": False,
        "request_parameters": False,
        "parameters_display_mode": None,
        "has_request_from_user": False,
        "show_on_panel_forced": 0,
    }
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        meta["error"] = f"json: {exc}"
        return raw, meta
    if not isinstance(report, dict):
        return raw, meta

    meta["has_request_from_user"] = mrt_has_request_from_user(report)
    changed = False

    if FORCE_REQUEST_PARAMETERS and not report.get("RequestParameters"):
        report["RequestParameters"] = True
        changed = True
    meta["request_parameters"] = bool(report.get("RequestParameters"))

    if FORCE_PARAMETERS_PANEL:
        mode = report.get("ParametersDisplayMode")
        # Panel = классическая полоса с Submit (не Dialog-окно)
        if mode not in ("Panel", 1, "1"):
            report["ParametersDisplayMode"] = "Panel"
            changed = True
        meta["parameters_display_mode"] = report.get("ParametersDisplayMode")
    else:
        meta["parameters_display_mode"] = report.get("ParametersDisplayMode")

    # Dashboard toolbar: Parameters да, ResetAllFilters нет (путает при 0 canvas-фильтров)
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

    # RFU-переменные обязаны быть на панели параметров
    dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}
    forced = 0
    for variable in numbered(dictionary.get("Variables")):
        if not isinstance(variable, dict):
            continue
        if not flag_true(variable.get("RequestFromUser")):
            continue
        if variable.get("ShowOnParametersPanel") is False or flag_false(
            variable.get("ShowOnParametersPanel")
        ):
            variable["ShowOnParametersPanel"] = True
            forced += 1
            changed = True
        elif "ShowOnParametersPanel" not in variable:
            variable["ShowOnParametersPanel"] = True
            forced += 1
            changed = True
    meta["show_on_panel_forced"] = forced

    meta["patched"] = changed
    if not changed:
        return raw, meta
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n", meta


def prepare_variables(args: StiVariablesEventArgs) -> None:
    """Только лог: какие RequestFromUser пришли. Values не трогаем (иначе сбивается dialog)."""
    variables = args.variables or {}
    names = list(variables.keys())
    print(f"[viewer] onPrepareVariables count={len(names)} names={names}", flush=True)
    for name, var in variables.items():
        vtype = var.type.value if getattr(var, "type", None) is not None else "?"
        empty = _is_empty(var.value)
        print(
            f"[viewer]   var {name} type={vtype} empty={empty} value={var.value!r}",
            flush=True,
        )


def begin_process_data(args: StiDataEventArgs) -> None:
    """Connection string из .env; пустые @Param не слать как ''."""
    if PG_CONN and args.connectionString is not None:
        args.connectionString = PG_CONN

    src = args.dataSource or "?"
    qpreview = (args.queryString or "")[:180].replace("\n", " ")
    print(f"[viewer] SQL source={src} q={qpreview!r}", flush=True)

    params = args.parameters or {}
    if not params:
        return

    query = args.queryString or ""
    for name, param in params.items():
        print(
            f"[viewer]   @{name} typeGroup={param.typeGroup!r} "
            f"typeName={param.typeName!r} value={param.value!r}",
            flush=True,
        )
        if not _is_empty(param.value):
            continue
        kind = "number" if _is_number_param(param) else (param.typeGroup or "value")
        query = _soften_empty_param(query, name)
        param.value = None
        print(f"[viewer] empty @{name} ({kind}) on {src} → no-op", flush=True)

    if query != (args.queryString or ""):
        args.queryString = query


@app.get("/mrt")
def serve_mrt():
    """Отдать .mrt с RequestParameters/Dialog — иначе viewer без Submit."""
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    patched, meta = patch_mrt_for_parameters_panel(raw)
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
    if info.get("filter_elements_count") == 0 and info.get("request_from_user_count"):
        info["note"] = (
            "Canvas-фильтров нет (filter_elements_count=0). "
            "Ожидай полосу Parameters + Submit сверху (RequestFromUser). "
            f"RFU: {[v.get('name') for v in info.get('request_from_user_variables') or []]}"
        )
    if not meta.get("has_request_from_user") and not info.get("request_from_user_count"):
        info["why_no_submit_panel"] = (
            "В .mrt нет variables с RequestFromUser=true. "
            "По докам Stimulsoft панель + Submit появляются только для таких variables. "
            "Canvas ComboBox/TreeView Submit не показывают."
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
    sti.options.toolbar.visible = True
    sti.options.toolbar.autoHide = False
    sti.options.toolbar.showParametersButton = True
    sti.options.toolbar.showAboutButton = True
    sti.options.toolbar.showRefreshButton = True
    sti.javascript.appendHead(_AFTER_INIT_HEAD)
    sti.onAfterInitialize += "__stiAfterInit"
    sti.onPrepareVariables += prepare_variables
    sti.onBeginProcessData += begin_process_data
    # после открытия .mrt — Panel + RequestParameters (полоса Submit)
    sti.onOpenedReport += _OPENED_REPORT_JS

    if sti.processRequest(request):
        return sti.getFrameworkResponse()

    report = StiReport()
    report.onBeforeRender += _OPENED_REPORT_JS
    report.loadFile(url_for("serve_mrt", _external=True))
    sti.report = report
    return sti.getFrameworkResponse()


def main() -> None:
    info = dashboard_debug(MRT_PATH)
    url = f"http://{VIEWER_HOST}:{VIEWER_PORT}/"
    print(f"MRT:     {MRT_PATH.resolve()}")
    print(f"anon:    {info.get('anonymized')}")
    print(f"RFU:     {info.get('request_from_user_count')} variables")
    print(f"filters: {info.get('filter_elements_count')} canvas elements")
    if info.get("request_from_user_variables"):
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
    print(f"DB:      {'yes' if PG_CONN else 'no'}")
    print(f"Open:    {url}")
    print(f"Debug:   {url}debug")
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
