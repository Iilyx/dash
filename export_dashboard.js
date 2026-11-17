/* Whole-dashboard export via official Stimulsoft JS API (not per-element).
 * Docs: exportDocumentAsync / exportDocumentAsync2 → Pdf | Excel2007 | Html
 */
(function () {
  var FORMATS = {
    Pdf: { ext: "pdf", mime: "application/pdf" },
    Excel2007: { ext: "xlsx", mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
    Html: { ext: "html", mime: "text/html;charset=utf-8" },
  };

  function bytesToBase64(bytes) {
    var chunk = 0x8000;
    var parts = [];
    for (var i = 0; i < bytes.length; i += chunk) {
      parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + chunk)));
    }
    return btoa(parts.join(""));
  }

  function toBase64(data) {
    if (typeof data === "string") {
      return btoa(unescape(encodeURIComponent(data)));
    }
    if (data instanceof ArrayBuffer) {
      return bytesToBase64(new Uint8Array(data));
    }
    if (data && data.buffer instanceof ArrayBuffer) {
      return bytesToBase64(new Uint8Array(data.buffer, data.byteOffset || 0, data.byteLength || data.length));
    }
    throw new Error("unsupported export payload type: " + typeof data);
  }

  function setStatus(text) {
    var el = document.getElementById("dashExportStatus");
    if (el) el.textContent = text || "";
  }

  async function exportWhole(formatName) {
    var report = window.__dashReport;
    if (!report) {
      setStatus("report not ready");
      return;
    }
    var meta = FORMATS[formatName];
    if (!meta) {
      setStatus("unknown format");
      return;
    }
    var Fmt = Stimulsoft.Report.StiExportFormat;
    var format = Fmt[formatName];
    if (format == null) {
      setStatus("format missing in Stimulsoft");
      return;
    }

    setStatus("exporting " + formatName + "…");
    try {
      var data = await report.exportDocumentAsync2(format);
      var b64 = toBase64(data);
      var res = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          format: formatName,
          ext: meta.ext,
          data_b64: b64,
        }),
      });
      var body = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        setStatus(body.error || "export failed");
        return;
      }
      setStatus("saved " + (body.name || meta.ext));
    } catch (err) {
      console.error(err);
      setStatus(String((err && err.message) || err));
    }
  }

  function mount() {
    if (document.getElementById("dashExportBar")) return;
    var bar = document.createElement("div");
    bar.id = "dashExportBar";
    bar.style.cssText =
      "position:fixed;z-index:2147483646;top:8px;right:8px;display:flex;gap:6px;align-items:center;" +
      "font:12px/1.2 ui-sans-serif,system-ui,sans-serif;background:rgba(20,20,20,.82);color:#fff;" +
      "padding:6px 8px;border-radius:6px;";
    ["Pdf", "Excel2007", "Html"].forEach(function (name) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = name === "Excel2007" ? "Excel" : name;
      btn.style.cssText =
        "cursor:pointer;border:0;border-radius:4px;padding:4px 8px;background:#eee;color:#111;";
      btn.onclick = function () {
        exportWhole(name);
      };
      bar.appendChild(btn);
    });
    var status = document.createElement("span");
    status.id = "dashExportStatus";
    status.style.cssText = "margin-left:4px;opacity:.9;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
    bar.appendChild(status);
    document.body.appendChild(bar);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }

  window.__dashExportWhole = exportWhole;
})();
