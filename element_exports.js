/* Per-element PDF/Excel for dashboard widgets that have Save.
 * Expects window.viewer (set from view_dashboard.py inject).
 */
(function () {
  var FILTER_RE =
    /Button|ComboBox|ListBox|TreeView|DatePicker|NumberBox|Filter/i;

  function $(id) {
    return document.getElementById(id);
  }

  function getViewer() {
    try {
      if (window.viewer) return window.viewer;
    } catch (e) {}
    return null;
  }

  function getReport() {
    var v = getViewer();
    return v && v.report ? v.report : null;
  }

  function collectionItems(col) {
    if (!col) return [];
    if (Array.isArray(col)) return col.slice();
    try {
      if (typeof col.toList === "function") {
        var list = col.toList();
        if (list && Array.isArray(list.list)) return list.list.slice();
        if (Array.isArray(list)) return list.slice();
      }
    } catch (e) {}
    try {
      if (typeof col.toArray === "function") return col.toArray();
    } catch (e2) {}
    var out = [];
    try {
      var n =
        typeof col.getCount === "function"
          ? col.getCount()
          : col.count != null
            ? col.count
            : col.length;
      if (typeof n === "number") {
        for (var i = 0; i < n; i++) {
          var item =
            typeof col.getByIndex === "function"
              ? col.getByIndex(i)
              : typeof col.get === "function"
                ? col.get(i)
                : col[i];
          if (item != null) out.push(item);
        }
      }
    } catch (e3) {}
    return out;
  }

  function typeName(el) {
    try {
      if (el.typeName) return String(el.typeName);
    } catch (e) {}
    try {
      if (el.ident) return String(el.ident);
    } catch (e2) {}
    try {
      return (el.constructor && el.constructor.name) || "";
    } catch (e3) {
      return "";
    }
  }

  function hasSave(el) {
    try {
      var di = el.dashboardInteraction;
      if (!di) return false;
      return di.showSaveButton !== false;
    } catch (e) {
      return false;
    }
  }

  function enableSaves(report) {
    if (!report) return 0;
    var n = 0;
    collectionItems(report.pages).forEach(function (page) {
      collectionItems(
        page.getElements ? page.getElements(false, null) : page.components
      ).forEach(function (el) {
        try {
          var di = el.dashboardInteraction;
          if (!di) return;
          if (FILTER_RE.test(typeName(el) + " " + (el.name || ""))) return;
          di.showSaveButton = true;
          n++;
        } catch (e) {}
      });
    });
    return n;
  }

  function listSaveElements(report) {
    var out = [];
    collectionItems(report.pages).forEach(function (page, pageIndex) {
      var isDash = false;
      try {
        isDash = !!page.isDashboard;
      } catch (e) {}
      if (!isDash) return;
      collectionItems(
        page.getElements ? page.getElements(false, null) : null
      ).forEach(function (el) {
        if (!hasSave(el)) return;
        var tn = typeName(el);
        if (FILTER_RE.test(tn + " " + (el.name || ""))) return;
        out.push({
          pageIndex: pageIndex,
          pageName: page.name || String(pageIndex),
          name: el.name || "element",
          alias: el.alias || el.name || "element",
          typeName: tn,
          element: el,
        });
      });
    });
    return out;
  }

  function makeSettings(kind) {
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
          : ns.StiExcelDashboardExportSettings;
      if (!C) return null;
      var s = new C();
      if ("imageQuality" in s) s.imageQuality = 400;
      return s;
    } catch (e) {
      console.warn("[element-exports] settings", e);
      return null;
    }
  }

  function exportToStream(el, settings) {
    var Tools =
      Stimulsoft.Dashboard &&
      Stimulsoft.Dashboard.Export &&
      Stimulsoft.Dashboard.Export.StiDashboardExportTools;
    if (!Tools || typeof Tools.exportToStream !== "function") {
      return Promise.reject(new Error("StiDashboardExportTools.exportToStream unavailable"));
    }
    return Promise.resolve(Tools.exportToStream(el, settings));
  }

  function toUint8(data) {
    if (data == null) return null;
    if (data instanceof Uint8Array) return data;
    if (data instanceof ArrayBuffer) return new Uint8Array(data);
    if (Array.isArray(data)) return new Uint8Array(data);
    if (typeof data === "string") {
      var enc = new TextEncoder();
      return enc.encode(data);
    }
    try {
      if (typeof data.length === "number") return new Uint8Array(data);
    } catch (e) {}
    return null;
  }

  function toBase64(u8) {
    var chunk = 0x8000;
    var parts = [];
    for (var i = 0; i < u8.length; i += chunk) {
      parts.push(String.fromCharCode.apply(null, u8.subarray(i, i + chunk)));
    }
    return btoa(parts.join(""));
  }

  function safeName(s) {
    return String(s || "element")
      .replace(/[^\w.\-]+/g, "_")
      .replace(/^[._]+|[._]+$/g, "")
      .slice(0, 80) || "element";
  }

  function downloadFile(filename, u8, mime) {
    try {
      if (
        Stimulsoft &&
        Stimulsoft.System &&
        Stimulsoft.System.StiObject &&
        typeof Stimulsoft.System.StiObject.saveAs === "function"
      ) {
        Stimulsoft.System.StiObject.saveAs(u8, filename, mime);
        return;
      }
    } catch (e) {
      console.warn("[element-exports] saveAs", e);
    }
    var blob = new Blob([u8], { type: mime || "application/octet-stream" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(a.href);
      a.remove();
    }, 1000);
  }

  function postFile(filename, u8) {
    return fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: filename,
        data: toBase64(u8),
      }),
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || !j.ok) throw new Error((j && j.error) || r.statusText);
        return j;
      });
    });
  }

  function saveFile(filename, u8, mime) {
    downloadFile(filename, u8, mime);
    return postFile(filename, u8).catch(function (e) {
      console.warn("[element-exports] server save skipped", e);
    });
  }

  function setStatus(text) {
    var el = $("sti-element-export-status");
    if (el) el.textContent = text || "";
  }

  async function exportAll() {
    var report = getReport();
    if (!report) {
      alert("Дашборд ещё не загружен");
      return;
    }
    enableSaves(report);
    var items = listSaveElements(report);
    if (!items.length) {
      alert("Нет элементов с Save");
      return;
    }

    var btn = $("sti-element-export-run");
    if (btn) btn.disabled = true;

    var stamp = new Date()
      .toISOString()
      .replace(/[-:TZ.]/g, "")
      .slice(0, 14);
    var ok = 0;
    var fail = 0;

    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var base =
        safeName(it.pageName) + "__" + safeName(it.name) + "_" + stamp;
      setStatus(
        i + 1 + "/" + items.length + " " + it.name + " PDF…"
      );
      try {
        var pdfSettings = makeSettings("pdf");
        var pdfData = toUint8(await exportToStream(it.element, pdfSettings));
        if (!pdfData || !pdfData.length) throw new Error("empty pdf");
        await saveFile(base + ".pdf", pdfData, "application/pdf");
        ok++;
      } catch (e) {
        fail++;
        console.warn("[element-exports] pdf", it.name, e);
      }

      setStatus(
        i + 1 + "/" + items.length + " " + it.name + " Excel…"
      );
      try {
        var xlsSettings = makeSettings("xlsx");
        var xlsData = toUint8(await exportToStream(it.element, xlsSettings));
        if (!xlsData || !xlsData.length) throw new Error("empty xlsx");
        await saveFile(
          base + ".xlsx",
          xlsData,
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        );
        ok++;
      } catch (e2) {
        fail++;
        console.warn("[element-exports] xlsx", it.name, e2);
      }
    }

    setStatus("готово: ok=" + ok + " fail=" + fail + " → exports/");
    if (btn) btn.disabled = false;
  }

  function mountBar() {
    if ($("sti-element-export-bar")) return;
    var style = document.createElement("style");
    style.textContent =
      "#sti-element-export-bar{position:fixed!important;z-index:2000001!important;" +
      "top:12px!important;right:12px!important;display:flex!important;gap:8px!important;" +
      "align-items:center!important;font:13px/1.2 system-ui,sans-serif!important}" +
      "#sti-element-export-bar button{cursor:pointer;padding:8px 12px;border:1px solid #222;" +
      "background:#111;color:#fff;border-radius:6px}" +
      "#sti-element-export-bar button:disabled{opacity:.5;cursor:wait}" +
      "#sti-element-export-status{color:#111;background:rgba(255,255,255,.85);" +
      "padding:4px 8px;border-radius:4px;max-width:360px}";
    document.head.appendChild(style);

    var bar = document.createElement("div");
    bar.id = "sti-element-export-bar";
    bar.innerHTML =
      '<button type="button" id="sti-element-export-run">Скачать Save → PDF+Excel</button>' +
      '<span id="sti-element-export-status"></span>';
    document.body.appendChild(bar);
    $("sti-element-export-run").onclick = function () {
      exportAll().catch(function (e) {
        console.error(e);
        setStatus(String(e && e.message ? e.message : e));
        var b = $("sti-element-export-run");
        if (b) b.disabled = false;
      });
    };
  }

  function boot() {
    mountBar();
    var tries = 0;
    var timer = setInterval(function () {
      tries++;
      var report = getReport();
      if (report) {
        enableSaves(report);
        clearInterval(timer);
        setStatus("элементов с Save: " + listSaveElements(report).length);
      } else if (tries > 40) {
        clearInterval(timer);
      }
    }, 500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
