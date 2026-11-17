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

import json
import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import stimulsoft_dashboards  # noqa: F401 — подтягивает dashboards.js
from flask import Flask, Response, request
from stimulsoft_data_adapters.enums.StiFrameworkType import StiFrameworkType
from stimulsoft_reports.classes.StiResult import StiResult
from stimulsoft_reports.designer import StiDesigner
from stimulsoft_reports.events import StiDataEventArgs, StiReportEventArgs
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer

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


def license_key() -> str:
    return (
        os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_KEY", "").strip()
        or os.environ.get("STIMULSOFT_REPORTS_LICENSE_KEY", "").strip()
        or os.environ.get("STIMULSOFT_LICENSE_KEY", "").strip()
    )


def license_file() -> str:
    raw = (
        os.environ.get("STIMULSOFT_DASHBOARDS_LICENSE_FILE", "").strip()
        or os.environ.get("STIMULSOFT_REPORTS_LICENSE_FILE", "").strip()
        or os.environ.get("STIMULSOFT_LICENSE_FILE", "").strip()
    )
    if not raw:
        return ""
    return str(path_of(raw).resolve())


def apply_license(component) -> bool:
    """Docs (How to Activate): ключ → Stimulsoft.Base.StiLicense.Key в HTML.

    В 2024.2.2: component.license.setKey/setFile; при viewer.report=report
    лицензия копируется с viewer на report и попадает в page через report.getHtml().
    .env в gitignore — ключ только локально, на git не уезжает.
    """
    key = license_key()
    file_path = license_file()
    if key:
        component.license.setKey(key)
        return True
    if file_path:
        if not Path(file_path).is_file():
            print(f"WARN: license file not found: {file_path}", flush=True)
            return False
        component.license.setFile(file_path)
        return True
    return False


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


def begin_process_data(args: StiDataEventArgs) -> None:
    args.connectionString = PG_CONN


def resolve_save_path(file_name: str | None) -> Path:
    """Save / Save As: новое имя → файл рядом с MRT_PATH (без path traversal)."""
    base = MRT_PATH.parent.resolve()
    if not file_name:
        return MRT_PATH
    name = Path(str(file_name)).name.strip()
    if not name:
        return MRT_PATH
    if not name.lower().endswith(".mrt"):
        name += ".mrt"
    target = (base / name).resolve()
    if target.parent != base:
        return MRT_PATH
    return target


def save_report(args: StiReportEventArgs):
    """Save / Save As из designer → .mrt на диске (MRT_PATH или новое имя)."""
    if args.report is None:
        return StiResult.getError("empty report")
    target = resolve_save_path(getattr(args, "fileName", None))
    try:
        target.write_text(
            json.dumps(args.report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return StiResult.getError(str(exc))
    print(f"Saved dashboard → {target}", flush=True)
    return f"Saved to {target.name}"


def normalize_sti_body(body: bytes | str | None) -> bytes | str | None:
    """Stimulsoft иногда шлёт report/settings уже объектом — StiRequest._setField
    делает json.loads(value) и падает TypeError → Save 500, «не сохраняет».
    """
    if body is None:
        return body
    raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
    if not raw or raw[:1] != b"{":
        return body
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(obj, dict):
        return body
    changed = False
    for key in ("report", "settings"):
        val = obj.get(key)
        if isinstance(val, (dict, list)):
            obj[key] = json.dumps(val, ensure_ascii=False)
            changed = True
    if not changed:
        return body
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def sti_process(component) -> bool:
    """2024.2.2: ImportError django в __processFrameworkRequest ломает Flask-детект.

    Не передаём flask.request — кормим query/body вручную и форсим framework=Flask.
    """
    handled = component.processRequest(
        query=request.args.to_dict(),
        body=normalize_sti_body(request.get_data(False)),
    )
    component.handler.framework = StiFrameworkType.FLASK
    component.handler.origin = request.origin or "*"
    # Не затираем реальные ошибки Save/Data — только django-детект шум.
    if component.handler.error and "django" in str(component.handler.error).lower():
        component.handler.error = None
    return handled


def sti_response(component):
    component.handler.framework = StiFrameworkType.FLASK
    if not component.handler.origin:
        component.handler.origin = request.origin or "*"
    return component.getFrameworkResponse()


# PDF = снимок fullscreen + карта (iframe → PNG через Stimulsoft).
# Excel = exportDocumentAsync. Fullscreen z-index до 1000000 — кнопки выше.
_EXPORT_UI = """
<script>
(function () {
  // Button: серая заливка в PDF, но userFilters (клики) и фильтры карты
  // живут на элементах — нельзя detach/disable/уводить за край.
  // Только прозрачная заливка на время экспорта.
  var __stiHiddenButtons = [];
  function typeName(c) {
    try {
      return (
        (c.constructor && (c.constructor.ssTypeName || c.constructor.name)) ||
        ""
      );
    } catch (e) {
      return "";
    }
  }
  function transparentBrush() {
    try {
      var C = Stimulsoft.System.Drawing.Color;
      var B = Stimulsoft.Base.Drawing.StiSolidBrush;
      return new B(C.fromArgb(0, 0, 0, 0));
    } catch (e) {
      return null;
    }
  }
  function isButtonElement(c) {
    if (!c) return false;
    try {
      var I =
        Stimulsoft.Report &&
        Stimulsoft.Report.Dashboard &&
        Stimulsoft.Report.Dashboard.IStiButtonElement;
      if (I && c.is && c.is(I)) return true;
    } catch (e) {}
    try {
      var Btn =
        Stimulsoft.Dashboard &&
        Stimulsoft.Dashboard.Components &&
        Stimulsoft.Dashboard.Components.Button &&
        Stimulsoft.Dashboard.Components.Button.StiButtonElement;
      if (Btn && ((c.is && c.is(Btn)) || c instanceof Btn)) return true;
    } catch (e) {}
    var tn = typeName(c);
    if (tn === "StiButtonElement" || /ButtonElement/i.test(tn)) return true;
    try {
      if (
        c.iconSet != null &&
        c.visualStates != null &&
        "text" in c &&
        c.cards == null &&
        c.columns == null
      )
        return true;
    } catch (e) {}
    return false;
  }
  function walkComponents(root, fn) {
    if (!root) return;
    fn(root);
    var kids = root.components;
    if (!kids || kids.count == null) return;
    var arr = [];
    for (var i = 0; i < kids.count; i++) arr.push(kids.getByIndex(i));
    for (var j = 0; j < arr.length; j++) walkComponents(arr[j], fn);
  }
  function collectAll(report) {
    var list = [];
    var seen = [];
    function add(c) {
      if (!c || seen.indexOf(c) >= 0) return;
      seen.push(c);
      list.push(c);
    }
    try {
      if (typeof report.getComponents === "function") {
        var comps = report.getComponents();
        for (var i = 0; i < comps.count; i++) add(comps.getByIndex(i));
      }
    } catch (e) {}
    try {
      if (report.pages) {
        for (var p = 0; p < report.pages.count; p++)
          walkComponents(report.pages.getByIndex(p), add);
      }
    } catch (e) {}
    return list;
  }
  function hideButtonsForExport(report) {
    var saved = [];
    if (!report) return saved;
    var tb = transparentBrush();
    collectAll(report).forEach(function (c) {
      if (!isButtonElement(c)) return;
      var item = {
        el: c,
        brush: c.brush,
        textBrush: c.textBrush,
        text: c.text,
        border: c.border,
        icon: null,
        checkedIcon: null,
        uncheckedIcon: null
      };
      // enabled и позиция не трогаем — иначе пропадают фильтры кликов Button/Map
      try {
        if (tb) {
          c.brush = tb;
          if ("textBrush" in c) c.textBrush = tb;
        }
      } catch (e) {}
      try {
        item.text = c.text;
        c.text = "";
      } catch (e) {}
      try {
        if (c.border) {
          item.border = c.border;
          if (typeof c.border.clone === "function") {
            var b = c.border.clone();
            if ("side" in b) b.side = 0;
            c.border = b;
          }
        }
      } catch (e) {}
      try {
        if (c.iconSet) {
          item.icon = c.iconSet.icon;
          item.checkedIcon = c.iconSet.checkedIcon;
          item.uncheckedIcon = c.iconSet.uncheckedIcon;
          c.iconSet.icon = null;
          c.iconSet.checkedIcon = null;
          c.iconSet.uncheckedIcon = null;
        }
      } catch (e) {}
      saved.push(item);
    });
    __stiHiddenButtons = saved;
    console.log("[sti-export] soft-hidden buttons:", saved.length);
    return saved;
  }
  function restoreButtons(saved) {
    (saved || __stiHiddenButtons || []).forEach(function (x) {
      try {
        if (x.brush !== undefined) x.el.brush = x.brush;
      } catch (e) {}
      try {
        if (x.textBrush !== undefined) x.el.textBrush = x.textBrush;
      } catch (e) {}
      try {
        if (x.text != null) x.el.text = x.text;
      } catch (e) {}
      try {
        if (x.border !== undefined) x.el.border = x.border;
      } catch (e) {}
      try {
        if (x.el.iconSet) {
          x.el.iconSet.icon = x.icon;
          x.el.iconSet.checkedIcon = x.checkedIcon;
          x.el.iconSet.uncheckedIcon = x.uncheckedIcon;
        }
      } catch (e) {}
    });
    __stiHiddenButtons = [];
  }
  function report() {
    if (window.viewer && window.viewer.report) return window.viewer.report;
    return null;
  }
  function firstDashboardPage(r) {
    if (!r || !r.pages) return null;
    for (var i = 0; i < r.pages.count; i++) {
      var p = r.pages.getByIndex(i);
      if (p && Number(p.width) > 0 && Number(p.height) > 0) return p;
    }
    return r.pages.count ? r.pages.getByIndex(0) : null;
  }
  function makeSettings(kind, r) {
    try {
      var ns =
        (Stimulsoft.Dashboard &&
          Stimulsoft.Dashboard.Export &&
          Stimulsoft.Dashboard.Export.Settings) ||
        null;
      if (!ns) return null;
      var C =
        kind === "pdf"
          ? ns.StiPdfDashboardExportSettings
          : kind === "xlsx"
            ? ns.StiExcelDashboardExportSettings
            : null;
      if (!C) return null;
      var s = new C();
      s.imageQuality = 600;
      try {
        var Or =
          Stimulsoft.Report &&
          Stimulsoft.Report.Components &&
          Stimulsoft.Report.Components.StiPageOrientation;
        if (Or && Or.Landscape != null) s.orientation = Or.Landscape;
      } catch (e) {}
      // Custom page = пропорции дашборда (иначе A4 сжимает по осям)
      try {
        var page = firstDashboardPage(r);
        var PK =
          Stimulsoft.System &&
          Stimulsoft.System.Drawing &&
          Stimulsoft.System.Drawing.Printing &&
          Stimulsoft.System.Drawing.Printing.PaperKind;
        if (page && Number(page.width) > 0 && Number(page.height) > 0) {
          var factor = 2;
          s.width = Number(page.width) * factor;
          s.height = Number(page.height) * factor;
          if (PK && PK.Custom != null) s.paperSize = PK.Custom;
          console.log(
            "[sti-export] page",
            s.width,
            "x",
            s.height,
            "dashboard",
            page.width,
            "x",
            page.height
          );
        }
      } catch (e) {
        console.warn("paper size skipped", e);
      }
      return s;
    } catch (e) {
      console.warn("export settings skipped", e);
    }
    return null;
  }
  function mount() {
    if (document.getElementById("sti-export-bar")) return;
    var style = document.createElement("style");
    style.textContent =
      "#sti-export-bar{position:fixed!important;z-index:2000001!important;top:12px!important;" +
      "right:12px!important;display:flex!important;gap:6px!important;" +
      "font:13px/1.2 system-ui,sans-serif!important;pointer-events:auto!important}" +
      "#sti-export-bar button{cursor:pointer;padding:8px 14px;border:1px solid #222;" +
      "background:#111;color:#fff;border-radius:4px;font:inherit}" +
      "#sti-export-bar button:hover{background:#333}" +
      "#sti-export-bar button:disabled{opacity:.5;cursor:wait}";
    document.head.appendChild(style);

    var bar = document.createElement("div");
    bar.id = "sti-export-bar";
    bar.innerHTML =
      '<button type="button" id="sti-export-pdf">PDF</button>' +
      '<button type="button" id="sti-export-xlsx">Excel</button>';
    document.body.appendChild(bar);

    function lockBar(label) {
      var btns = bar.querySelectorAll("button");
      var labels = [];
      btns.forEach(function (b) {
        labels.push(b.textContent);
        b.disabled = true;
      });
      if (btns[0] && label) btns[0].textContent = label;
      return { btns: btns, labels: labels };
    }
    function unlockBar(state) {
      if (!state) return;
      state.btns.forEach(function (b, i) {
        b.disabled = false;
        if (state.labels[i]) b.textContent = state.labels[i];
      });
    }
    function loadScript(src) {
      return new Promise(function (resolve, reject) {
        var existing = document.querySelector('script[data-sti-cdn="' + src + '"]');
        if (existing) {
          if (existing.getAttribute("data-loaded") === "1") resolve();
          else existing.addEventListener("load", function () { resolve(); });
          return;
        }
        var s = document.createElement("script");
        s.src = src;
        s.async = true;
        s.setAttribute("data-sti-cdn", src);
        s.onload = function () {
          s.setAttribute("data-loaded", "1");
          resolve();
        };
        s.onerror = function () {
          reject(new Error("Не загрузился " + src));
        };
        document.head.appendChild(s);
      });
    }
    function captureRoot() {
      return (
        document.querySelector(".stiJsViewerDashboardBackPanel") ||
        document.querySelector(".stiJsViewerReportPanel") ||
        document.querySelector(".stiJsViewerMainPanel") ||
        document.querySelector("#StiViewer") ||
        document.body
      );
    }
    function isOnlineMapElement(c) {
      if (!c) return false;
      try {
        var I =
          Stimulsoft.Report &&
          Stimulsoft.Report.Dashboard &&
          Stimulsoft.Report.Dashboard.IStiOnlineMapElement;
        if (I && c.is && c.is(I)) return true;
      } catch (e) {}
      try {
        var M =
          Stimulsoft.Dashboard &&
          Stimulsoft.Dashboard.Components &&
          Stimulsoft.Dashboard.Components.OnlineMap &&
          Stimulsoft.Dashboard.Components.OnlineMap.StiOnlineMapElement;
        if (M && ((c.is && c.is(M)) || c instanceof M)) return true;
      } catch (e) {}
      return /OnlineMap/i.test(typeName(c));
    }
    function findDomByElementName(name) {
      var root = captureRoot();
      var nodes = root.querySelectorAll("*");
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        var a = el.elementAttributes;
        if (!a) continue;
        if (name && a.name === name) return el;
        if (!name && a.type === "StiOnlineMapElement") return el;
      }
      return null;
    }
    function bytesToPngDataUrl(data) {
      if (!data) return null;
      if (typeof data === "string") {
        if (data.indexOf("data:") === 0) return data;
        return "data:image/png;base64," + data;
      }
      var arr =
        data instanceof Uint8Array
          ? data
          : new Uint8Array(data.buffer || data);
      var bin = "";
      var step = 0x8000;
      for (var i = 0; i < arr.length; i += step) {
        bin += String.fromCharCode.apply(
          null,
          arr.subarray(i, Math.min(i + step, arr.length))
        );
      }
      return "data:image/png;base64," + btoa(bin);
    }
    function exportMapPng(mapEl) {
      return new Promise(function (resolve, reject) {
        try {
          var Exp = Stimulsoft.Dashboard.Export;
          var Tools =
            (Exp && Exp.StiDashboardExportTools) ||
            (Exp && Exp.Tools && Exp.Tools.StiDashboardExportTools);
          var ImgSet =
            Exp && Exp.Settings && Exp.Settings.StiImageDashboardExportSettings;
          var ImageType =
            Stimulsoft.Report &&
            Stimulsoft.Report.Export &&
            Stimulsoft.Report.Export.StiImageType;
          if (!Tools || !ImgSet) {
            reject(new Error("нет Image export tools"));
            return;
          }
          var s = new ImgSet();
          if (ImageType && ImageType.Png != null) s.imageType = ImageType.Png;
          s.imageQuality = 400;
          var w = Number(mapEl.width) || 0;
          var h = Number(mapEl.height) || 0;
          if (w > 0 && h > 0) {
            s.width = w;
            s.height = h;
          }
          var finish = function (data) {
            resolve(data);
          };
          if (typeof Tools.exportAsync === "function") {
            Tools.exportAsync(finish, mapEl, s);
          } else {
            Promise.resolve(Tools.export(mapEl, s)).then(finish).catch(reject);
          }
        } catch (e) {
          reject(e);
        }
      });
    }
    // iframe-карту html2canvas не видит → подменяем PNG из Stimulsoft
    function bakeMapsIntoDom(r) {
      var restorers = [];
      var maps = collectAll(r).filter(isOnlineMapElement);
      var chain = Promise.resolve();
      maps.forEach(function (mapEl) {
        chain = chain.then(function () {
          var dom = findDomByElementName(mapEl.name);
          if (!dom) {
            console.warn("[sti-export] DOM map not found", mapEl.name);
            return;
          }
          return exportMapPng(mapEl)
            .then(function (data) {
              var url = bytesToPngDataUrl(data);
              if (!url) return;
              var iframe = dom.frame || dom.querySelector("iframe");
              var img = document.createElement("img");
              img.setAttribute("data-sti-map-bake", "1");
              img.src = url;
              img.alt = "map";
              img.style.cssText =
                "position:absolute;left:0;top:0;width:100%;height:100%;" +
                "object-fit:fill;border:none;z-index:2;pointer-events:none";
              if (iframe) {
                restorers.push({
                  iframe: iframe,
                  display: iframe.style.display
                });
                iframe.style.display = "none";
              }
              restorers.push({ img: img });
              dom.appendChild(img);
              console.log("[sti-export] baked map", mapEl.name);
            })
            .catch(function (e) {
              console.warn("[sti-export] map bake failed", mapEl.name, e);
            });
        });
      });
      return chain.then(function () {
        return function restoreMaps() {
          restorers.forEach(function (x) {
            try {
              if (x.img && x.img.parentNode) x.img.parentNode.removeChild(x.img);
            } catch (e) {}
            try {
              if (x.iframe) x.iframe.style.display = x.display || "";
            } catch (e) {}
          });
        };
      });
    }
    function exportPdfAsScreen() {
      var r = report();
      if (!captureRoot()) {
        alert("Дашборд ещё не загружен");
        return;
      }
      var state = lockBar("…");
      var done = false;
      var restoreMaps = function () {};
      function unlock() {
        if (done) return;
        done = true;
        try {
          restoreMaps();
        } catch (e) {}
        unlockBar(state);
      }
      setTimeout(unlock, 180000);
      Promise.resolve()
        .then(function () {
          return loadScript(
            "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"
          );
        })
        .then(function () {
          return loadScript(
            "https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"
          );
        })
        .then(function () {
          if (!r) return function () {};
          return bakeMapsIntoDom(r);
        })
        .then(function (restore) {
          restoreMaps = restore || function () {};
          return new Promise(function (res) {
            requestAnimationFrame(function () {
              setTimeout(res, 80);
            });
          });
        })
        .then(function () {
          return window.html2canvas(captureRoot(), {
            scale: 2,
            useCORS: true,
            allowTaint: true,
            backgroundColor: "#ffffff",
            logging: false,
            ignoreElements: function (el) {
              return (
                el &&
                (el.id === "sti-export-bar" ||
                  el.id === "sti-lic-banner" ||
                  (el.className &&
                    String(el.className).indexOf("stiJsViewerToolBar") >= 0))
              );
            }
          });
        })
        .then(function (canvas) {
          var JsPDF =
            (window.jspdf && window.jspdf.jsPDF) || window.jsPDF || null;
          if (!JsPDF) throw new Error("jsPDF не найден");
          var pdf = new JsPDF({
            orientation: canvas.width >= canvas.height ? "landscape" : "portrait",
            unit: "pt",
            format: [canvas.width, canvas.height],
            compress: true
          });
          pdf.addImage(
            canvas.toDataURL("image/png"),
            "PNG",
            0,
            0,
            canvas.width,
            canvas.height,
            undefined,
            "FAST"
          );
          var name =
            ((r && (r.reportAlias || r.reportName)) || "dashboard") + ".pdf";
          pdf.save(name);
        })
        .catch(function (e) {
          console.error(e);
          alert("Ошибка PDF: " + (e && e.message ? e.message : e));
        })
        .then(unlock);
    }
    function exportDoc(fmt, ext, mime, settings) {
      var r = report();
      if (!r) { alert("Дашборд ещё не загружен"); return; }
      if (typeof r.exportDocumentAsync !== "function") {
        alert("exportDocumentAsync недоступен");
        return;
      }
      var state = lockBar("…");
      var hiddenButtons = hideButtonsForExport(r);
      if (state.btns[0])
        state.btns[0].textContent =
          "…" + (hiddenButtons.length ? "(" + hiddenButtons.length + ")" : "(0)");
      var done = false;
      function unlock() {
        if (done) return;
        done = true;
        restoreButtons(hiddenButtons);
        unlockBar(state);
      }
      setTimeout(unlock, 120000);
      try {
        var args = [
          function (data) {
            try {
              if (!data) {
                alert("Экспорт вернул пустой файл");
                return;
              }
              var name = (r.reportAlias || r.reportName || "dashboard") + ext;
              Stimulsoft.System.StiObject.saveAs(data, name, mime);
            } catch (e) {
              console.error(e);
              alert("Ошибка сохранения: " + (e && e.message ? e.message : e));
            } finally {
              unlock();
            }
          },
          fmt
        ];
        if (settings) {
          args.push(null);
          args.push(settings);
        }
        r.exportDocumentAsync.apply(r, args);
      } catch (e) {
        console.error(e);
        unlock();
        alert("Ошибка экспорта: " + (e && e.message ? e.message : e));
      }
    }
    document.getElementById("sti-export-pdf").onclick = function () {
      exportPdfAsScreen();
    };
    document.getElementById("sti-export-xlsx").onclick = function () {
      var r = report();
      exportDoc(
        Stimulsoft.Report.StiExportFormat.Excel2007,
        ".xlsx",
        "application/vnd.ms-excel",
        makeSettings("xlsx", r)
      );
    };
  }
  function hookViewerExport() {
    var v = window.viewer;
    if (!v || v.__stiHideButtonsHooked) return;
    v.__stiHideButtonsHooked = true;
    var prevBegin = v.onBeginExportReport;
    var prevEnd = v.onEndExportReport;
    v.onBeginExportReport = function (args, callback) {
      try {
        hideButtonsForExport(v.report || (args && args.report));
      } catch (e) {}
      if (typeof prevBegin === "function") return prevBegin(args, callback);
      if (typeof callback === "function") callback();
    };
    v.onEndExportReport = function (args, callback) {
      try {
        restoreButtons();
      } catch (e) {}
      if (typeof prevEnd === "function") return prevEnd(args, callback);
      if (typeof callback === "function") callback();
    };
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { setTimeout(mount, 0); });
  } else {
    setTimeout(mount, 0);
  }
  window.addEventListener("load", function () {
    setTimeout(mount, 50);
    setTimeout(hookViewerExport, 100);
    setTimeout(hookViewerExport, 1000);
  });
})();
</script>
"""


# Включает Save / View Data / Full Screen на всех dashboard-элементах
# и полный набор пунктов в меню Save (PDF, Excel, Image, Data, Document…).
_ELEMENT_SAVE_JS = """
<script>
window.stiEnableElementSaves = function (report) {
  if (!report) return;
  function enable(c) {
    var di = c && c.dashboardInteraction;
    if (!di) return;
    di.showSaveButton = true;
    if ("showViewDataButton" in di) di.showViewDataButton = true;
    if ("showFullScreenButton" in di) di.showFullScreenButton = true;
  }
  if (typeof report.getComponents === "function") {
    var comps = report.getComponents();
    for (var i = 0; i < comps.count; i++) enable(comps.getByIndex(i));
    return;
  }
  if (!report.pages) return;
  for (var p = 0; p < report.pages.count; p++) {
    var page = report.pages.getByIndex(p);
    if (!page || !page.components) continue;
    for (var c = 0; c < page.components.count; c++) enable(page.components.getByIndex(c));
  }
};
</script>
"""


def inject_export_ui(html: str) -> str:
    """window.viewer + кнопки PDF/Excel + Save на всех элементах до assign в viewer."""
    html = html.replace(
        "let viewer = new Stimulsoft.Viewer.StiViewer",
        "var viewer = window.viewer = new Stimulsoft.Viewer.StiViewer",
        1,
    )
    # Docs: после load, до assign в viewer. loadFile может догрузить async — ещё раз через timeout.
    html = html.replace(
        "viewer.report = report;",
        (
            "stiEnableElementSaves(report); viewer.report = report; "
            "setTimeout(function(){ stiEnableElementSaves(viewer.report||report); }, 0); "
            "setTimeout(function(){ stiEnableElementSaves(viewer.report||report); }, 800);"
        ),
        1,
    )
    inject = _ELEMENT_SAVE_JS + _EXPORT_UI
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx == -1:
        return html + inject
    return html[:idx] + inject + html[idx:]


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/mrt", methods=["GET", "POST"])
def serve_mrt():
    raw = MRT_PATH.read_text(encoding="utf-8-sig")
    resp = Response(raw, mimetype="application/json; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


_LICENSE_BANNER = """
<script>
(function () {
  if (document.getElementById("sti-lic-banner")) return;
  var b = document.createElement("div");
  b.id = "sti-lic-banner";
  b.textContent =
    "Нет STIMULSOFT_*LICENSE* в .env — серый trial на Cards/Button и watermark. " +
    "Ключ локальный (.env в gitignore), не из Designer login.";
  b.style.cssText =
    "position:fixed;z-index:2000002;left:12px;bottom:12px;max-width:420px;" +
    "padding:8px 10px;background:#7a1f1f;color:#fff;font:12px/1.35 system-ui,sans-serif;" +
    "border-radius:4px;opacity:.95";
  document.body.appendChild(b);
})();
</script>
"""


def inject_license_banner(html: str) -> str:
    if license_key() or license_file():
        return html
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx == -1:
        return html + _LICENSE_BANNER
    return html[:idx] + _LICENSE_BANNER + html[idx:]


@app.route("/", methods=["GET", "POST"])
@app.route("/designer", methods=["GET", "POST"])
def designer():
    designer = StiDesigner()
    licensed = apply_license(designer)
    designer.options.appearance.fullScreenMode = True
    # По умолчанию showSaveButton=False — только File→Save. Включаем кнопку на тулбаре.
    designer.options.toolbar.showSaveButton = True
    designer.options.toolbar.showFileMenu = True
    designer.options.toolbar.showFileMenuSave = True
    designer.options.toolbar.showFileMenuSaveAs = True
    designer.options.toolbar.showFileMenuNew = True
    designer.options.toolbar.showFileMenuNewDashboard = True
    designer.options.appearance.showSaveDialog = True
    designer.options.dashboardElements.showCardsElement = True
    designer.options.dashboardElements.showButtonElement = True
    designer.onBeginProcessData += begin_process_data
    designer.onSaveReport += save_report
    designer.onSaveAsReport += save_report

    if sti_process(designer):
        return sti_response(designer)

    report = StiReport()
    apply_license(report)
    report.loadFile("/mrt")
    designer.report = report  # копирует designer.license → report.license
    if licensed:
        apply_license(designer.report)
    resp = sti_response(designer)
    html = resp.get_data(as_text=True)
    if isinstance(html, str) and "</body>" in html.lower():
        resp.set_data(inject_license_banner(html))
    return resp


def enable_full_exports(viewer: StiViewer) -> None:
    """Полное меню Save: диалог настроек + все форматы (элемент и весь дашборд)."""
    ex = viewer.options.exports
    viewer.options.toolbar.showSaveButton = True
    ex.showExportDialog = True
    ex.showExportToDocument = True
    ex.showExportToPdf = True
    ex.showExportToExcel = True
    ex.showExportToImageSvg = True
    ex.showExportToCsv = True
    ex.showExportToJson = True
    ex.showExportToHtml = True
    ex.showExportToHtml5 = True
    ex.showExportToWord = True
    ex.showExportToText = True
    ex.showExportToPowerPoint = True
    ex.showExportToOpenDocumentCalc = True
    ex.showExportToOpenDocumentWriter = True
    ex.showExportToXps = True


@app.route("/viewer", methods=["GET", "POST"])
def viewer():
    viewer = StiViewer()
    licensed = apply_license(viewer)
    viewer.options.appearance.fullScreenMode = True
    enable_full_exports(viewer)
    viewer.onBeginProcessData += begin_process_data

    if sti_process(viewer):
        return sti_response(viewer)

    report = StiReport()
    apply_license(report)
    report.loadFile("/mrt")
    viewer.report = report  # копирует viewer.license → report.license → JS StiLicense.Key
    if licensed:
        apply_license(viewer.report)
    resp = sti_response(viewer)
    html = resp.get_data(as_text=True)
    if isinstance(html, str) and ("</body>" in html.lower() or "StiViewer" in html):
        html = inject_license_banner(inject_export_ui(html))
        if licensed and "StiLicense.Key" not in html and "StiLicense.loadFromFile" not in html:
            print("WARN: license set in Python but missing in HTML", flush=True)
        resp.set_data(html)
    return resp


def main() -> None:
    print("Stimulsoft Dashboards.PYTHON 2024.2.2 — official Flask wiring", flush=True)
    print(f"MRT: {MRT_PATH.resolve()}", flush=True)
    print(f"Designer: http://{HOST}:{PORT}/designer", flush=True)
    print(f"Viewer:   http://{HOST}:{PORT}/viewer", flush=True)
    if license_key() or license_file():
        print("License: OK (from .env — not committed to git)", flush=True)
    else:
        print(
            "WARN: нет Stimulsoft license в .env — trial watermark в PDF/Excel "
            "и «Не удалось отобразить рисунок» на чартах. "
            "Preview в Designer ок, потому что там логин Stimulsoft. "
            ".env в .gitignore — ключ не из git. "
            "Нужен ключ именно для Dashboards.PYTHON / JS 2024.2.x "
            "(STIMULSOFT_DASHBOARDS_LICENSE_KEY или _LICENSE_FILE).",
            flush=True,
        )
    if OPEN_BROWSER:
        try:
            webbrowser.open(f"http://{HOST}:{PORT}/viewer")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
