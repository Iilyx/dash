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

  function ensurePanel() {
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

  function setStatus(text) {
    ensurePanel();
    var actionEl = document.getElementById("dash-interaction-action");
    if (actionEl) actionEl.textContent = text;
  }

  function cleanParams(params) {
    var out = {};
    Object.keys(params || {}).forEach(function (k) {
      if (k === "__action" || k.indexOf("filteringParameters") === 0) return;
      out[k] = params[k];
    });
    return out;
  }

  function downloadJson(filename, data) {
    var blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json;charset=utf-8",
    });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
  }

  // После Submit ждём onEndProcessData (данные уже в report), потом снимаем datasets.
  window._dashPendingResults = null;
  var _dashExportTimer = null;

  function getViewerReport() {
    try {
      if (typeof viewer !== "undefined" && viewer && viewer.report) return viewer.report;
    } catch (e) {}
    return null;
  }

  function rowValue(row, col, idx) {
    try {
      if (row && typeof row.getValue === "function") return row.getValue(col);
      if (row && typeof row.getByIndex === "function") return row.getByIndex(idx);
      if (row && row.list) return row.list[idx];
      if (Array.isArray(row)) return row[idx];
      if (row && typeof row === "object") return row[col];
    } catch (e) {}
    return null;
  }

  function dumpDataSources(report) {
    var out = {};
    try {
      var sources =
        report &&
        report.dictionary &&
        report.dictionary.dataSources &&
        report.dictionary.dataSources.list
          ? report.dictionary.dataSources.list
          : [];
      sources.forEach(function (src) {
        var name = src.name || "unknown";
        var cols = [];
        try {
          var colList =
            (src.columns && src.columns.list) ||
            (src.dataTable && src.dataTable.columns && src.dataTable.columns.list) ||
            [];
          cols = colList.map(function (c) {
            return c.name || c.columnName || c.caption || "";
          }).filter(Boolean);
        } catch (e) {}
        var rows = [];
        try {
          var table = src.dataTable;
          var list =
            table && table.rows && table.rows.list
              ? table.rows.list
              : [];
          list.forEach(function (row) {
            var obj = {};
            if (cols.length) {
              cols.forEach(function (col, idx) {
                obj[col] = rowValue(row, col, idx);
              });
            } else if (row && row.list) {
              obj = row.list.slice();
            }
            rows.push(obj);
          });
        } catch (e) {
          out[name] = { error: String(e), rows: [] };
          return;
        }
        out[name] = { columns: cols, rowcount: rows.length, rows: rows };
      });
    } catch (e) {
      out._error = String(e);
    }
    return out;
  }

  function dumpVariables(report) {
    var out = {};
    try {
      var list =
        report &&
        report.dictionary &&
        report.dictionary.variables &&
        report.dictionary.variables.list
          ? report.dictionary.variables.list
          : [];
      list.forEach(function (v) {
        if (!v || !v.name) return;
        out[v.name] = v.value;
      });
    } catch (e) {
      out._error = String(e);
    }
    return out;
  }

  function buildComputedPayload(report, params) {
    var payload = {
      kind: "computed",
      ok: true,
      at: new Date().toISOString(),
      params: cleanParams(params || {}),
      variables: dumpVariables(report),
      datasets: dumpDataSources(report),
    };
    try {
      if (report && typeof report.saveDocumentToJsonString === "function") {
        var doc = report.saveDocumentToJsonString();
        if (doc) {
          try {
            payload.document = JSON.parse(doc);
          } catch (e) {
            payload.document_raw = doc;
          }
        }
      }
    } catch (e) {
      payload.document_error = String(e);
    }
    return payload;
  }

  function exportComputedResults(params) {
    setStatus("export…");
    var report = getViewerReport();
    if (!report) {
      setStatus("no report");
      alert("Нет viewer.report — дождись загрузки дашборда и нажми Submit снова");
      return;
    }
    var payload = buildComputedPayload(report, params);
    var stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadJson("dashboard-results-" + stamp + ".json", payload);
    setStatus("downloaded results");
    if (window.console && console.log) {
      console.log("[dash results]", payload);
    }
    fetch("/api/results", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(function () {});
  }

  window.dashOnEndProcessData = function () {
    if (!window._dashPendingResults) return;
    if (_dashExportTimer) clearTimeout(_dashExportTimer);
    // несколько sources → несколько EndProcessData; берём последний с паузой
    _dashExportTimer = setTimeout(function () {
      var p = window._dashPendingResults;
      window._dashPendingResults = null;
      _dashExportTimer = null;
      exportComputedResults(p);
    }, 700);
  };

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
          params: cleanParams(params),
          interaction: dump,
        }),
      }).catch(function () {});

      // Submit → ждём загрузку данных, потом снимаем УЖЕ ПОСЧИТАННЫЕ datasets
      if (args && args.action === "Variables") {
        window._dashPendingResults = cleanParams(params);
        setStatus("waiting data…");
        // запасной таймер, если EndProcessData не придёт
        setTimeout(function () {
          if (window._dashPendingResults) {
            var p = window._dashPendingResults;
            window._dashPendingResults = null;
            exportComputedResults(p);
          }
        }, 5000);
      }
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
