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

  function getReport() {
    if (window.__dashViewer && window.__dashViewer.report) {
      return window.__dashViewer.report;
    }
    return window.__dashReport || null;
  }

  function cellValue(row, colIndex) {
    try {
      if (row && typeof row.getValue === "function") return row.getValue(colIndex);
      if (row && row.values) return row.values[colIndex];
      if (Array.isArray(row)) return row[colIndex];
    } catch (e) {}
    return null;
  }

  /** Уже посчитанные datasets из живого report (таблицы/SQL после рендера). */
  function collectDatasets(rowLimit) {
    rowLimit = rowLimit || 5000;
    var report = getReport();
    var out = {};
    if (!report || !report.dictionary || !report.dictionary.dataSources) {
      return out;
    }
    var sources = report.dictionary.dataSources;
    var list = [];
    try {
      if (typeof sources.list !== "undefined") list = sources.list;
      else if (typeof sources.toList === "function") list = sources.toList();
      else if (sources.count != null && sources.getByIndex) {
        for (var i = 0; i < sources.count; i++) list.push(sources.getByIndex(i));
      }
    } catch (e) {
      return out;
    }
    list.forEach(function (ds) {
      if (!ds) return;
      var name = ds.name || ds.alias || "source";
      var columns = [];
      try {
        var cols = ds.columns;
        var clist = cols && cols.list ? cols.list : [];
        for (var c = 0; c < clist.length; c++) {
          columns.push(clist[c].name || clist[c].alias || String(c));
        }
      } catch (e2) {}
      var rows = [];
      try {
        var count =
          ds.count != null
            ? ds.count
            : ds.dataTable && ds.dataTable.rows
              ? ds.dataTable.rows.count
              : 0;
        var n = Math.min(count, rowLimit);
        for (var r = 0; r < n; r++) {
          var rowObj = {};
          var row =
            typeof ds.getDataRow === "function"
              ? ds.getDataRow(r)
              : ds.dataTable && ds.dataTable.rows && ds.dataTable.rows.getByIndex
                ? ds.dataTable.rows.getByIndex(r)
                : null;
          for (var ci = 0; ci < columns.length; ci++) {
            rowObj[columns[ci]] = cellValue(row, ci);
          }
          rows.push(rowObj);
        }
        out[name] = { columns: columns, rowcount: count, rows: rows };
      } catch (e3) {
        out[name] = { columns: columns, error: String(e3) };
      }
    });
    return out;
  }

  function loadHtml2Canvas() {
    return new Promise(function (resolve, reject) {
      if (window.html2canvas) {
        resolve(window.html2canvas);
        return;
      }
      var s = document.createElement("script");
      s.src =
        "https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js";
      s.onload = function () {
        resolve(window.html2canvas);
      };
      s.onerror = function () {
        reject(new Error("html2canvas load failed"));
      };
      document.head.appendChild(s);
    });
  }

  function downloadDataUrl(dataUrl, filename) {
    var a = document.createElement("a");
    a.href = dataUrl;
    a.download = filename;
    a.click();
  }

  function downloadJson(obj, filename) {
    var blob = new Blob([JSON.stringify(obj, null, 2)], {
      type: "application/json;charset=utf-8",
    });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
    }, 2000);
  }

  /**
   * Полный снимок того, что видно: PNG страницы + datasets + params.
   * Вызов: await dashDownloadFilled()  или Ctrl+Shift+S
   * Без зелёных кнопок на экране.
   */
  window.dashGetFilled = function (opts) {
    opts = opts || {};
    var rowLimit = opts.row_limit || 5000;
    var report = getReport();
    var panel = document.getElementById("dash-interaction-debug");
    var prevDisplay = panel ? panel.style.display : "";
    if (panel) panel.style.display = "none";

    var root =
      document.getElementById("viewerContent") ||
      document.querySelector("[id$='Content']") ||
      document.body;

    var meta = {
      params: {},
      datasets: collectDatasets(rowLimit),
      page: null,
      pages_count: null,
    };
    try {
      if (report && report.pages) {
        meta.pages_count = report.pages.count;
        meta.page =
          report.currentPage != null ? report.currentPage : 0;
      }
    } catch (e) {}

    return fetch("/api/params")
      .then(function (r) {
        return r.json();
      })
      .catch(function () {
        return {};
      })
      .then(function (p) {
        if (p && p.params) meta.params = p.params;
        return loadHtml2Canvas();
      })
      .then(function (html2canvas) {
        return html2canvas(root, {
          backgroundColor: "#ffffff",
          useCORS: true,
          allowTaint: true,
          logging: false,
          scale: window.devicePixelRatio || 1,
        });
      })
      .then(function (canvas) {
        if (panel) panel.style.display = prevDisplay;
        meta.png_base64 = canvas.toDataURL("image/png");
        return meta;
      })
      .catch(function (e) {
        if (panel) panel.style.display = prevDisplay;
        throw e;
      });
  };

  window.dashDownloadFilled = function (opts) {
    return window.dashGetFilled(opts).then(function (meta) {
      var ts = new Date().toISOString().replace(/[:.]/g, "-");
      if (meta.png_base64) {
        downloadDataUrl(meta.png_base64, "dashboard-filled-" + ts + ".png");
      }
      downloadJson(
        {
          params: meta.params,
          datasets: meta.datasets,
          page: meta.page,
          pages_count: meta.pages_count,
        },
        "dashboard-filled-" + ts + ".json"
      );
      return fetch("/api/filled", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(meta),
      })
        .then(function (r) {
          return r.json();
        })
        .then(function (res) {
          if (window.console && console.log) {
            console.log("[dash filled]", res);
          }
          return res;
        });
    });
  };

  document.addEventListener("keydown", function (e) {
    if (e.ctrlKey && e.shiftKey && (e.key === "S" || e.key === "s")) {
      e.preventDefault();
      window.dashDownloadFilled().catch(function (err) {
        if (window.console && console.error) console.error(err);
        alert("Снимок не удался: " + (err && err.message ? err.message : err));
      });
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensurePanel);
  } else {
    ensurePanel();
  }
})();
