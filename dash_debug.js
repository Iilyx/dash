/* Interaction dump + кнопка «page» → POST /api/page → GET /api/page */
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
      '<strong style="color:#86efac">dash</strong>' +
      '<span id="dash-interaction-action" style="color:#93c5fd"></span>' +
      '<span style="margin-left:auto"></span>' +
      '<button type="button" id="dash-page-btn" style="background:#1f2937;color:#86efac;border:1px solid #475569;padding:2px 8px;cursor:pointer">page</button>' +
      '<button type="button" id="dash-interaction-clear" style="background:#1f2937;color:#e2e8f0;border:1px solid #475569;padding:2px 8px;cursor:pointer">clear</button>' +
      '<button type="button" id="dash-interaction-hide" style="background:#1f2937;color:#e2e8f0;border:1px solid #475569;padding:2px 8px;cursor:pointer">hide</button>';
    var pre = document.createElement("pre");
    pre.id = "dash-interaction-pre";
    pre.style.cssText =
      "margin:0;padding:10px;overflow:auto;flex:1;white-space:pre-wrap;word-break:break-word;color:#bbf7d0;";
    pre.textContent =
      "Кнопка page → снимок экрана на сервер.\n" +
      "Потом: curl /api/page\n";
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
    document.getElementById("dash-page-btn").onclick = function () {
      window.dashPushPage().then(function (res) {
        if (pre) pre.textContent = JSON.stringify(res, null, 2);
      });
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

  function safeStr(v) {
    if (v == null) return null;
    try {
      return String(v);
    } catch (e) {
      return null;
    }
  }

  function componentList(container) {
    if (!container) return [];
    try {
      if (container.list) return container.list;
      if (typeof container.toList === "function") return container.toList();
      if (container.count != null && container.getByIndex) {
        var arr = [];
        for (var i = 0; i < container.count; i++) arr.push(container.getByIndex(i));
        return arr;
      }
    } catch (e) {}
    return [];
  }

  function walkComponents(comp, out, depth) {
    if (!comp || depth > 12) return;
    var ident = "";
    try {
      ident =
        comp.ident ||
        (comp.GetType && String(comp.GetType())) ||
        (comp.constructor && comp.constructor.name) ||
        "";
    } catch (e) {
      ident = "";
    }
    var name = safeStr(comp.name) || safeStr(comp.alias) || "";
    var item = { name: name, type: safeStr(ident) || "unknown" };
    try {
      if (comp.text != null) item.text = safeStr(comp.text);
      if (comp.textValue != null) item.textValue = safeStr(comp.textValue);
      if (comp.title != null) {
        var t = comp.title;
        item.title =
          typeof t === "object" && t && t.text != null
            ? safeStr(t.text)
            : safeStr(t);
      }
      if (comp.tooltip != null) item.tooltip = safeStr(comp.tooltip);
    } catch (e2) {}

    var typeStr = String(item.type || "");
    if (/Button/i.test(typeStr) || /Button/i.test(name)) {
      item.role = "button";
      out.buttons.push(item);
    }
    if (
      (/Text/i.test(typeStr) || item.text != null || item.textValue != null) &&
      (item.text || item.textValue || item.title)
    ) {
      item.role = item.role || "text";
      out.texts.push(item);
    }
    out.elements.push(item);

    var nested = componentList(comp.components);
    for (var i = 0; i < nested.length; i++) {
      walkComponents(nested[i], out, depth + 1);
    }
  }

  function collectElements(report) {
    var out = { elements: [], texts: [], buttons: [] };
    if (!report || !report.pages) return out;
    var pages = componentList(report.pages);
    for (var p = 0; p < pages.length; p++) {
      var page = pages[p];
      if (!page) continue;
      walkComponents(page, out, 0);
      var comps = componentList(page.components);
      for (var c = 0; c < comps.length; c++) {
        walkComponents(comps[c], out, 0);
      }
    }
    return out;
  }

  function cellValue(row, colIndex) {
    try {
      if (row && typeof row.getValue === "function") return row.getValue(colIndex);
      if (row && row.values) return row.values[colIndex];
      if (Array.isArray(row)) return row[colIndex];
    } catch (e) {}
    return null;
  }

  function collectDatasets(rowLimit) {
    rowLimit = rowLimit || 2000;
    var report = getReport();
    var out = {};
    if (!report || !report.dictionary || !report.dictionary.dataSources) return out;
    var list = componentList(report.dictionary.dataSources);
    list.forEach(function (ds) {
      if (!ds) return;
      var name = ds.name || ds.alias || "source";
      var columns = [];
      try {
        var cols = componentList(ds.columns);
        for (var c = 0; c < cols.length; c++) {
          columns.push(cols[c].name || cols[c].alias || String(c));
        }
      } catch (e) {}
      try {
        var count =
          ds.count != null
            ? ds.count
            : ds.dataTable && ds.dataTable.rows
              ? ds.dataTable.rows.count
              : 0;
        var n = Math.min(count, rowLimit);
        var rows = [];
        for (var r = 0; r < n; r++) {
          var rowObj = {};
          var row =
            typeof ds.getDataRow === "function"
              ? ds.getDataRow(r)
              : ds.dataTable &&
                  ds.dataTable.rows &&
                  ds.dataTable.rows.getByIndex
                ? ds.dataTable.rows.getByIndex(r)
                : null;
          for (var ci = 0; ci < columns.length; ci++) {
            rowObj[columns[ci]] = cellValue(row, ci);
          }
          rows.push(rowObj);
        }
        out[name] = { columns: columns, rowcount: count, rows: rows };
      } catch (e2) {
        out[name] = { columns: columns, error: String(e2) };
      }
    });
    return out;
  }

  function scrapeDomTexts() {
    var root =
      document.getElementById("viewerContent") ||
      document.querySelector("[id$='Content']");
    if (!root) return [];
    var nodes = root.querySelectorAll(
      "span, div, p, label, td, th, button, a, h1, h2, h3, h4"
    );
    var seen = {};
    var out = [];
    for (var i = 0; i < nodes.length && out.length < 400; i++) {
      var el = nodes[i];
      if (!el || el.children.length > 3) continue;
      var t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (!t || t.length < 1 || t.length > 200) continue;
      if (seen[t]) continue;
      seen[t] = 1;
      out.push({
        text: t,
        tag: el.tagName.toLowerCase(),
        className: el.className ? String(el.className).slice(0, 80) : "",
      });
    }
    return out;
  }

  window.dashCollectPage = function () {
    var report = getReport();
    var parts = collectElements(report);
    var snap = {
      params: {},
      elements: parts.elements,
      texts: parts.texts,
      buttons: parts.buttons,
      datasets: collectDatasets(2000),
      dom_texts: scrapeDomTexts(),
      page: null,
      pages_count: null,
    };
    try {
      if (report && report.pages) {
        snap.pages_count = report.pages.count;
        snap.page = report.currentPage != null ? report.currentPage : 0;
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
        if (p && p.params) snap.params = p.params;
        try {
          if (report && report.dictionary && report.dictionary.variables) {
            componentList(report.dictionary.variables).forEach(function (v) {
              if (v && v.name != null) {
                snap.params[v.name] =
                  v.value != null ? v.value : snap.params[v.name];
              }
            });
          }
        } catch (e2) {}
        return snap;
      });
  };

  window.dashPushPage = function () {
    return window.dashCollectPage().then(function (snap) {
      return fetch("/api/page", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snap),
      }).then(function (r) {
        return r.json().then(function (data) {
          if (window.console && console.log) console.log("[dash page]", data);
          return data;
        });
      });
    });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensurePanel);
  } else {
    ensurePanel();
  }
})();
