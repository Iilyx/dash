/* Snapshot готового dashboard во viewer:
 * variables + последний interaction + StiElementDataCache.getOrCreate по элементам с meters.
 * Элементы без данных → skipped.
 */
(function () {
  var MAX_ROWS = 200;
  var MAX_CELL = 400;
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

  function collectionToArray(col) {
    if (!col) return [];
    if (Array.isArray(col)) return col.slice();
    var out = [];
    try {
      if (typeof col.toArray === "function") return col.toArray();
    } catch (e) {}
    try {
      var count =
        typeof col.getCount === "function"
          ? col.getCount()
          : col.count != null
            ? col.count
            : col.length;
      if (typeof count === "number") {
        for (var i = 0; i < count; i++) {
          var item =
            typeof col.getByIndex === "function"
              ? col.getByIndex(i)
              : typeof col.get === "function"
                ? col.get(i)
                : col[i];
          if (item != null) out.push(item);
        }
        return out;
      }
    } catch (e2) {}
    try {
      Object.keys(col).forEach(function (k) {
        if (/^\d+$/.test(k) && col[k] != null) out.push(col[k]);
      });
    } catch (e3) {}
    return out;
  }

  function cellValue(v) {
    if (v == null) return null;
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
      if (typeof v === "string" && v.length > MAX_CELL) return v.slice(0, MAX_CELL) + "…";
      return v;
    }
    try {
      if (v instanceof Date) return v.toISOString();
    } catch (e) {}
    try {
      if (Array.isArray(v)) return v.map(cellValue);
    } catch (e2) {}
    try {
      if (typeof v.valueOf === "function") {
        var raw = v.valueOf();
        if (raw !== v) return cellValue(raw);
      }
    } catch (e3) {}
    try {
      var s = String(v);
      return s.length > MAX_CELL ? s.slice(0, MAX_CELL) + "…" : s;
    } catch (e4) {
      return null;
    }
  }

  function rememberReport(report) {
    if (report) window.__dashReport = report;
  }

  function getViewer() {
    try {
      if (typeof viewer !== "undefined" && viewer) return viewer;
    } catch (e) {}
    try {
      if (window.viewer) return window.viewer;
    } catch (e2) {}
    return null;
  }

  function getReport() {
    try {
      var v = getViewer();
      if (v && v.report) {
        rememberReport(v.report);
        return v.report;
      }
    } catch (e) {}
    return window.__dashReport || null;
  }

  function readVariables(report) {
    var out = {};
    try {
      var vars = report.dictionary && report.dictionary.variables;
      collectionToArray(vars).forEach(function (v) {
        try {
          var name = v.name || v.Name;
          if (!name) return;
          out[name] =
            v.valueObject != null ? cellValue(v.valueObject) : cellValue(v.value);
        } catch (e) {}
      });
    } catch (e2) {}
    return out;
  }

  function dumpArgs(args) {
    var out = { __keys: [] };
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
    keys.forEach(function (k) {
      if (k === "report") {
        out[k] = "[StiReport]";
        return;
      }
      try {
        out[k] = JSON.parse(JSON.stringify(args[k]));
      } catch (e2) {
        try {
          out[k] = String(args[k]);
        } catch (e3) {
          out[k] = "[unserializable]";
        }
      }
    });
    [
      "event",
      "sender",
      "action",
      "variables",
      "filteringParameters",
      "sortingParameters",
      "collapsingParameters",
      "drillDownParameters",
    ].forEach(function (k) {
      if (!(k in out) && args[k] !== undefined) {
        try {
          out[k] = k === "report" ? "[StiReport]" : JSON.parse(JSON.stringify(args[k]));
        } catch (e4) {
          out[k] = String(args[k]);
        }
        out.__keys.push(k);
      }
    });
    out.__knownActions = ACTIONS;
    return out;
  }

  function collectElements(report) {
    var elements = [];
    var seen = {};
    function add(el) {
      if (!el) return;
      var name = null;
      try {
        name = el.name || el.Name || null;
      } catch (e) {}
      var key = name || "anon:" + elements.length;
      if (seen[key]) return;
      seen[key] = true;
      elements.push(el);
    }
    function walk(comps) {
      collectionToArray(comps).forEach(function (c) {
        add(c);
        try {
          if (c.components) walk(c.components);
        } catch (e) {}
        try {
          if (typeof c.getComponents === "function") walk(c.getComponents());
        } catch (e2) {}
      });
    }
    try {
      if (typeof report.getComponents === "function") walk(report.getComponents());
    } catch (e) {}
    collectionToArray(report.pages).forEach(function (page) {
      try {
        if (page.components) walk(page.components);
      } catch (e2) {}
      try {
        if (typeof page.getComponents === "function") walk(page.getComponents());
      } catch (e3) {}
    });
    return elements;
  }

  function elementTypeName(el) {
    try {
      if (el.componentType) return String(el.componentType);
    } catch (e) {}
    try {
      return (el.constructor && el.constructor.name) || null;
    } catch (e2) {
      return null;
    }
  }

  function hasMeters(el) {
    try {
      if (typeof el.getMeters !== "function") return false;
      var meters = el.getMeters();
      if (!meters) return false;
      if (typeof meters.any === "function") return meters.any();
      return collectionToArray(meters).length > 0;
    } catch (e) {
      return false;
    }
  }

  function serializeDataTable(table) {
    if (!table) return { empty: true };
    var columns = [];
    var rows = [];
    var truncated = false;
    try {
      columns = collectionToArray(table.columns || table.Columns).map(function (c) {
        try {
          return c.name || c.Name || String(c);
        } catch (e) {
          return String(c);
        }
      });
    } catch (e2) {}
    try {
      var rawRows = collectionToArray(table.rows || table.Rows);
      if (rawRows.length > MAX_ROWS) {
        truncated = true;
        rawRows = rawRows.slice(0, MAX_ROWS);
      }
      rows = rawRows.map(function (r) {
        try {
          if (typeof r.getValue === "function") {
            return columns.map(function (_, idx) {
              return cellValue(r.getValue(idx));
            });
          }
        } catch (e3) {}
        try {
          return collectionToArray(r).map(cellValue);
        } catch (e4) {
          return [cellValue(r)];
        }
      });
    } catch (e5) {}
    return {
      empty: columns.length === 0 && rows.length === 0,
      columns: columns,
      rows: rows,
      row_count: rows.length,
      truncated: truncated,
    };
  }

  function ensurePanel() {
    var el = document.getElementById("dash-snapshot-panel");
    if (el) return el;
    el = document.createElement("div");
    el.id = "dash-snapshot-panel";
    el.style.cssText = [
      "position:fixed",
      "right:10px",
      "bottom:10px",
      "z-index:2147483647",
      "display:flex",
      "gap:8px",
      "align-items:center",
      "padding:8px 10px",
      "background:#0f172a",
      "color:#e2e8f0",
      "border:1px solid #334155",
      "border-radius:10px",
      "font:12px/1.3 ui-sans-serif,system-ui,sans-serif",
      "box-shadow:0 8px 24px rgba(0,0,0,.35)",
    ].join(";");
    el.innerHTML =
      '<span style="color:#94a3b8">dashboard</span>' +
      '<button type="button" id="dash-snapshot-btn" style="background:#14532d;color:#bbf7d0;border:1px solid #22c55e;border-radius:6px;padding:4px 10px;cursor:pointer">snapshot</button>' +
      '<span id="dash-snapshot-status" style="color:#86efac;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>';
    document.body.appendChild(el);
    document.getElementById("dash-snapshot-btn").onclick = function () {
      window.dashCaptureSnapshot();
    };
    return el;
  }

  function setStatus(text) {
    ensurePanel();
    var s = document.getElementById("dash-snapshot-status");
    if (s) s.textContent = text || "";
  }

  window.dashCaptureInteraction = function (args) {
    try {
      if (args && args.report) rememberReport(args.report);
      var dump = dumpArgs(args);
      window.__dashLastInteraction = dump;
      fetch("/api/interaction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(dump),
      }).catch(function () {});
      setStatus("interaction: " + (dump.action || "?"));
    } catch (e) {
      if (window.console && console.error) console.error(e);
    }
  };

  window.dashCaptureSnapshot = async function () {
    ensurePanel();
    setStatus("собираю…");
    var report = getReport();
    if (!report) {
      var err = {
        ok: false,
        error: "report not ready — дождись загрузки или кликни по дашборду",
      };
      setStatus(err.error);
      return err;
    }

    var Cache =
      (window.Stimulsoft &&
        Stimulsoft.Report &&
        Stimulsoft.Report.Dashboard &&
        Stimulsoft.Report.Dashboard.StiElementDataCache) ||
      null;
    if (!Cache || typeof Cache.getOrCreate !== "function") {
      var err2 = { ok: false, error: "StiElementDataCache.getOrCreate недоступен" };
      setStatus(err2.error);
      return err2;
    }

    var all = collectElements(report);
    var elements = [];
    var skipped = [];
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      var name = null;
      var alias = null;
      try {
        name = el.name || null;
      } catch (e) {}
      try {
        alias = el.alias || null;
      } catch (e2) {}
      var typ = elementTypeName(el);
      if (!hasMeters(el)) {
        skipped.push({ name: name, alias: alias, type: typ, reason: "no meters" });
        continue;
      }
      var entry = { name: name, alias: alias, type: typ };
      try {
        entry.data = serializeDataTable(await Cache.getOrCreate(el));
      } catch (e3) {
        entry.error = String(e3 && e3.message ? e3.message : e3);
      }
      elements.push(entry);
    }

    var snapshot = {
      ok: true,
      captured_at: new Date().toISOString(),
      variables: readVariables(report),
      interaction: window.__dashLastInteraction || null,
      element_count: elements.length,
      skipped_count: skipped.length,
      elements: elements,
      skipped: skipped,
    };

    setStatus("elements=" + elements.length + " skipped=" + skipped.length);
    if (window.console && console.log) console.log("[dash snapshot]", snapshot);

    try {
      await fetch("/api/snapshot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshot),
      });
    } catch (e4) {}
    return snapshot;
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensurePanel);
  } else {
    ensurePanel();
  }
})();
