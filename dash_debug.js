/* Viewer interaction dump: click → overlay + POST /api/params */
(function () {
  var ACTIONS = [
    "InitVars",
    "Variables",
    "Sorting",
    "DrillDown",
    "Collapsing",
    "DashboardFiltering",
    "DashboardSorting",
    "DashboardResetAllFilters",
    "DashboardElementDrillDown",
    "DashboardElementDrillUp",
  ];

  var PARAM_BAGS = [
    "variables",
    "filteringParameters",
    "sortingParameters",
    "collapsingParameters",
    "drillDownParameters",
  ];

  function safeClone(value) {
    if (value == null) return value;
    if (typeof value !== "object") return value;
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (e) {
      try {
        return String(value);
      } catch (e2) {
        return "[unserializable]";
      }
    }
  }

  function dumpArgs(args) {
    var out = {
      __keys: [],
      __knownActions: ACTIONS,
    };
    if (!args || typeof args !== "object") {
      out.raw = args;
      return out;
    }
    var keys = [];
    try {
      keys = Object.keys(args);
    } catch (e) {
      keys = [];
    }
    out.__keys = keys.slice();
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      if (k === "report") {
        out[k] = "[StiReport]";
        continue;
      }
      out[k] = safeClone(args[k]);
    }
    // на случай non-enumerable / пропущенных полей из доки
    [
      "event",
      "sender",
      "action",
      "preventDefault",
      "async",
      "variables",
      "sortingParameters",
      "collapsingParameters",
      "drillDownParameters",
      "filteringParameters",
      "data",
    ].forEach(function (k) {
      if (!(k in out) && args[k] !== undefined) {
        out[k] = k === "report" ? "[StiReport]" : safeClone(args[k]);
        out.__keys.push(k);
      }
    });
    return out;
  }

  function flattenParams(args) {
    var params = {};
    function put(name, value) {
      if (name == null || name === "") return;
      params[String(name)] = value;
    }
    function walk(src, prefix) {
      if (src == null) return;
      if (Array.isArray(src)) {
        src.forEach(function (item, idx) {
          if (item && typeof item === "object") {
            if (item.name != null) put(item.name, item.value);
            else if (prefix) put(prefix + "[" + idx + "]", item);
            else walk(item, prefix);
          } else if (prefix) {
            put(prefix + "[" + idx + "]", item);
          }
        });
        return;
      }
      if (typeof src !== "object") {
        if (prefix) put(prefix, src);
        return;
      }
      Object.keys(src).forEach(function (k) {
        var v = src[k];
        if (v && typeof v === "object" && "value" in v && !Array.isArray(v)) {
          put(prefix ? prefix + "." + k : k, v.value);
        } else if (v && typeof v === "object" && !Array.isArray(v)) {
          // плоские поля Stimulsoft (ComponentName, DataBand, …)
          var nestedKeys = Object.keys(v);
          var looksNested = nestedKeys.some(function (nk) {
            return v[nk] && typeof v[nk] === "object";
          });
          if (!looksNested && nestedKeys.length && nestedKeys.length <= 12) {
            nestedKeys.forEach(function (nk) {
              put((prefix ? prefix + "." : "") + k + "." + nk, v[nk]);
            });
          } else {
            put(prefix ? prefix + "." + k : k, v);
          }
        } else {
          put(prefix ? prefix + "." + k : k, v);
        }
      });
    }
    PARAM_BAGS.forEach(function (bag) {
      walk(args[bag], bag === "variables" ? "" : bag);
    });
    if (args.action) put("__action", args.action);
    return params;
  }

  function getViewerJs() {
    var viewer = window.__dashViewer;
    if (viewer && viewer.jsObject) return viewer.jsObject;
    return null;
  }

  function getReport() {
    var viewer = window.__dashViewer;
    if (viewer && viewer.report) return viewer.report;
    if (window.__dashReport) return window.__dashReport;
    return null;
  }

  function dashExportSettings(format) {
    var js = getViewerJs();
    // База как у кнопки Save в тулбаре (DashboardPdf / DashboardExcel)
    var settings = null;
    try {
      if (js && typeof js.getDefaultExportSettings === "function") {
        settings = js.getDefaultExportSettings(format, true);
      }
    } catch (e) {
      settings = null;
    }
    if (!settings || typeof settings !== "object") {
      settings = {};
    }
    // Копия — не портим defaultSettings в options
    var out = {};
    Object.keys(settings).forEach(function (k) {
      out[k] = settings[k];
    });
    out.Orientation = out.Orientation || "Landscape";
    out.PaperSize = out.PaperSize || (format === "Pdf" ? "A3" : "A4");
    out.RenderSinglePage = false;
    out.ImageQuality = out.ImageQuality != null ? out.ImageQuality : 200;
    out.OpenAfterExport = false;
    return out;
  }

  function saveBlob(data, name, mime) {
    if (
      window.Stimulsoft &&
      window.Stimulsoft.System &&
      window.Stimulsoft.System.StiObject &&
      window.Stimulsoft.System.StiObject.saveAs
    ) {
      window.Stimulsoft.System.StiObject.saveAs(data, name, mime);
      return;
    }
    var blob = new Blob([data], { type: mime });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
    }, 2000);
  }

  /** Графики в PDF = растр; если экспортировать слишком рано — серые прямоугольники. */
  function waitForChartsReady(timeoutMs) {
    timeoutMs = timeoutMs || 8000;
    var start = Date.now();
    return new Promise(function (resolve) {
      function tick() {
        var root =
          document.getElementById("viewerContent") ||
          document.querySelector("[id$='Content']") ||
          document.body;
        var charts = root
          ? root.querySelectorAll("svg, canvas, img")
          : [];
        var ready = false;
        for (var i = 0; i < charts.length; i++) {
          var el = charts[i];
          if (el.tagName === "IMG") {
            if (el.complete && el.naturalWidth > 2) {
              ready = true;
              break;
            }
          } else if (el.tagName === "CANVAS") {
            if (el.width > 2 && el.height > 2) {
              ready = true;
              break;
            }
          } else if (el.tagName === "SVG") {
            var box = el.getBoundingClientRect();
            if (box.width > 2 && box.height > 2) {
              ready = true;
              break;
            }
          }
        }
        if (ready || Date.now() - start > timeoutMs) {
          // ещё кадр — дорисовать анимации
          requestAnimationFrame(function () {
            setTimeout(resolve, 400);
          });
          return;
        }
        setTimeout(tick, 200);
      }
      tick();
    });
  }

  function exportDashboard(format) {
    var isPdf = format === "Pdf";
    var btnId = isPdf ? "dash-export-pdf" : "dash-export-excel";
    var btn = document.getElementById(btnId);
    var label = isPdf ? "Скачать PDF" : "Скачать Excel";
    var js = getViewerJs();
    if (!js || typeof js.postExport !== "function") {
      alert("Дождитесь загрузки дашборда");
      return;
    }
    if (btn) {
      btn.disabled = true;
      btn.textContent = isPdf ? "ждём графики…" : "Excel…";
    }
    var done = function () {
      if (btn) {
        btn.disabled = false;
        btn.textContent = label;
      }
    };

    // Только штатный ExportDashboard viewer (как Save → PDF).
    // Свой report.exportDocumentAsync2 перерисовывает чарты заново → часто серые плитки.
    waitForChartsReady(10000)
      .then(function () {
        if (btn) btn.textContent = isPdf ? "PDF…" : "Excel…";
        var settings = dashExportSettings(format);
        js.postExport(format, settings, null, true);
        // postExport сам качает файл; кнопку отпускаем с запасом
        setTimeout(done, 2500);
      })
      .catch(function (e) {
        if (window.console && console.error) console.error(e);
        alert("Ошибка экспорта: " + (e && e.message ? e.message : e));
        done();
      });
  }

  function makeExportButton(id, text, topPx, onClick, title) {
    if (document.getElementById(id)) return;
    var btn = document.createElement("button");
    btn.id = id;
    btn.type = "button";
    btn.textContent = text;
    btn.title = title || "";
    btn.style.cssText = [
      "position:fixed",
      "top:" + topPx + "px",
      "right:12px",
      "z-index:2147483647",
      "padding:10px 16px",
      "font:600 13px/1.2 system-ui,sans-serif",
      "color:#0b1220",
      "background:#86efac",
      "border:1px solid #22c55e",
      "border-radius:8px",
      "box-shadow:0 6px 20px rgba(0,0,0,.25)",
      "cursor:pointer",
    ].join(";");
    btn.onclick = onClick;
    document.body.appendChild(btn);
  }

  function ensurePdfButton() {
    makeExportButton(
      "dash-export-pdf",
      "Скачать PDF",
      12,
      function () {
        exportDashboard("Pdf");
      },
      "Все страницы → один PDF (A3). Таблицы в PDF — снимок как на экране, не все строки."
    );
    makeExportButton(
      "dash-export-excel",
      "Скачать Excel",
      56,
      function () {
        exportDashboard("Excel");
      },
      "Excel обычно вытягивает таблицы полнее, чем PDF."
    );
  }

  function ensurePanel() {
    ensurePdfButton();
    var el = document.getElementById("dash-interaction-debug");
    if (el) return el;
    el = document.createElement("div");
    el.id = "dash-interaction-debug";
    el.style.cssText = [
      "position:fixed",
      "right:8px",
      "bottom:8px",
      "z-index:2147483647",
      "width:min(520px,46vw)",
      "max-height:48vh",
      "display:flex",
      "flex-direction:column",
      "background:#0b1220",
      "color:#d7f5d0",
      "border:1px solid #334155",
      "box-shadow:0 8px 28px rgba(0,0,0,.35)",
      "font:12px/1.35 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
      "opacity:.96",
    ].join(";");
    var head = document.createElement("div");
    head.style.cssText =
      "display:flex;align-items:center;gap:8px;padding:8px 10px;background:#111827;border-bottom:1px solid #334155;cursor:move;user-select:none;";
    head.innerHTML =
      '<strong style="color:#86efac">interaction</strong>' +
      '<span id="dash-interaction-action" style="color:#93c5fd"></span>' +
      '<span style="margin-left:auto;color:#94a3b8">click dashboard</span>' +
      '<button type="button" id="dash-interaction-clear" style="background:#1f2937;color:#e2e8f0;border:1px solid #475569;padding:2px 8px;cursor:pointer">clear</button>' +
      '<button type="button" id="dash-interaction-hide" style="background:#1f2937;color:#e2e8f0;border:1px solid #475569;padding:2px 8px;cursor:pointer">hide</button>';
    var pre = document.createElement("pre");
    pre.id = "dash-interaction-pre";
    pre.style.cssText =
      "margin:0;padding:10px;overflow:auto;flex:1;white-space:pre-wrap;word-break:break-word;color:#bbf7d0;";
    pre.textContent =
      "Кликай по элементам дашборда — сюда придёт полный args.\n\n" +
      "actions:\n- " +
      ACTIONS.join("\n- ");
    el.appendChild(head);
    el.appendChild(pre);
    document.body.appendChild(el);
    document.getElementById("dash-interaction-hide").onclick = function () {
      el.style.display = "none";
    };
    document.getElementById("dash-interaction-clear").onclick = function () {
      pre.textContent = "";
      fetch("/api/params", { method: "DELETE" }).catch(function () {});
    };
    return el;
  }

  function showDump(dump) {
    var el = ensurePanel();
    el.style.display = "flex";
    var actionEl = document.getElementById("dash-interaction-action");
    if (actionEl) actionEl.textContent = dump.action || "(no action)";
    var pre = document.getElementById("dash-interaction-pre");
    if (pre) pre.textContent = JSON.stringify(dump, null, 2);
  }

  window.dashCaptureInteraction = function (args) {
    try {
      var dump = dumpArgs(args);
      var params = flattenParams(args);
      showDump(dump);
      if (window.console && console.log) {
        console.log("[dash interaction]", dump.action, dump);
      }
      fetch("/api/params", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          params: params,
          interaction: dump,
        }),
      }).catch(function () {});
    } catch (e) {
      if (window.console && console.error) console.error(e);
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensurePanel);
  } else {
    ensurePanel();
  }
})();
