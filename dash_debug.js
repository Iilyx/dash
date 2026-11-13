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

  function exportPdf() {
    var js = getViewerJs();
    if (!js) {
      alert("Дождитесь загрузки дашборда и нажмите PDF ещё раз");
      return;
    }
    try {
      // 4-й аргумент true = ExportDashboard (не report)
      var settings =
        typeof js.getDefaultExportSettings === "function"
          ? js.getDefaultExportSettings("Pdf")
          : {};
      if (js.options && js.options.exports && js.options.exports.showExportDialog && js.controls && js.controls.forms && js.controls.forms.exportForm) {
        js.controls.forms.exportForm.show(
          "Pdf",
          js.options.actions.exportReport,
          "Adobe PDF"
        );
      } else if (typeof js.postExport === "function") {
        js.postExport("Pdf", settings, null, true);
      } else {
        alert("Stimulsoft export API недоступен");
      }
    } catch (e) {
      if (window.console && console.error) console.error(e);
      alert("Ошибка экспорта PDF: " + (e && e.message ? e.message : e));
    }
  }

  function ensurePdfButton() {
    if (document.getElementById("dash-export-pdf")) return;
    var btn = document.createElement("button");
    btn.id = "dash-export-pdf";
    btn.type = "button";
    btn.textContent = "Скачать PDF";
    btn.style.cssText = [
      "position:fixed",
      "top:12px",
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
    btn.onclick = exportPdf;
    document.body.appendChild(btn);
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
