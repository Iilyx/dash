/* Кнопка «Скрин».
 *
 * Stimulsoft кладёт RegionMap HTML в iframe (InsertHTMLContentToDashboardElement + document.write).
 * html2canvas iframe не видит → карта пустая.
 * Фикс: html2canvas всего iframe (карта + labels), размер = CSS-бокс iframe;
 * fallback — SVG с inline fill у text; прячем iframe, снимаем страницу.
 */
(function () {
  var HTML2CANVAS =
    "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js";
  var SCALE = 2;

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      if (src.indexOf("html2canvas") >= 0 && window.html2canvas) {
        resolve();
        return;
      }
      var key = 'script[data-dash-cdn="' + src + '"]';
      var existing = document.querySelector(key);
      if (existing) {
        if (existing.getAttribute("data-loaded") === "1") resolve();
        else
          existing.addEventListener("load", function () {
            resolve();
          });
        return;
      }
      var s = document.createElement("script");
      s.src = src;
      s.async = true;
      s.setAttribute("data-dash-cdn", src);
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
      document.getElementById("viewerContent") ||
      document.body
    );
  }

  function setStatus(text) {
    var el = document.getElementById("dashShotStatus");
    if (el) el.textContent = text || "";
  }

  function stamp() {
    var d = new Date();
    function p(n) {
      return String(n).padStart(2, "0");
    }
    return (
      d.getFullYear() +
      p(d.getMonth() + 1) +
      p(d.getDate()) +
      "_" +
      p(d.getHours()) +
      p(d.getMinutes()) +
      p(d.getSeconds())
    );
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  /** StiJsViewer — postInteraction / ApplyFilters. */
  function getViewerJs() {
    try {
      if (window.__dashViewerJs && window.__dashViewerJs.postInteraction)
        return window.__dashViewerJs;
    } catch (e) {}
    try {
      var v = window.__dashViewer;
      if (v && v.jsObject && v.jsObject.postInteraction) {
        window.__dashViewerJs = v.jsObject;
        return v.jsObject;
      }
    } catch (e) {}
    try {
      var root = document.getElementById("viewerContent") || document.body;
      var all = root.getElementsByTagName("*");
      for (var i = 0; i < all.length; i++) {
        var jo = all[i].jsObject;
        if (jo && typeof jo.postInteraction === "function") {
          window.__dashViewerJs = jo;
          return jo;
        }
      }
    } catch (e) {}
    return null;
  }

  /** Ждём ApplyFilters / перерисовку дашборда после клика. */
  async function waitDashboardIdle(minMs, maxMs) {
    var js = getViewerJs();
    await sleep(minMs != null ? minMs : 700);
    var t0 = Date.now();
    maxMs = maxMs != null ? maxMs : 12000;
    var sawBusy = false;
    while (Date.now() - t0 < maxMs) {
      var busy = false;
      try {
        if (js && js.options) {
          if (js.options.interactionInProgress || js.options.refreshInProgress)
            busy = true;
        }
        var pi = js && js.controls && js.controls.processImage;
        if (pi && pi.style && pi.style.display !== "none" && pi.style.display !== "")
          busy = true;
      } catch (e) {}
      if (busy) sawBusy = true;
      if (sawBusy && !busy) {
        await sleep(600);
        return true;
      }
      // фильтр с debounce 500ms — даём шанс стартовать
      if (!sawBusy && Date.now() - t0 > 3500) {
        await sleep(900);
        return false;
      }
      await sleep(200);
    }
    return false;
  }

  /** Имя файла без пробелов/спецсимволов. */
  function safeSlug(text, maxLen) {
    var s = String(text || "")
      .trim()
      .replace(/\s+/g, "_")
      .replace(/[^\w\-а-яА-ЯёЁ]+/gi, "_")
      .replace(/_+/g, "_")
      .replace(/^_|_$/g, "");
    if (!s) s = "item";
    return s.slice(0, maxLen || 60);
  }

  function downloadBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 2000);
  }

  function blobToBase64(blob) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        var s = String(reader.result || "");
        var i = s.indexOf(",");
        resolve(i >= 0 ? s.slice(i + 1) : s);
      };
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  }

  function loadImage(url) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        resolve(img);
      };
      img.onerror = function () {
        reject(new Error("img load failed"));
      };
      img.src = url;
    });
  }

  function attrType(el) {
    var a = el && el.elementAttributes;
    return a && a.type ? String(a.type) : "";
  }

  function attrName(el) {
    var a = el && el.elementAttributes;
    return a && a.name ? String(a.name) : "";
  }

  function iframeDoc(iframe) {
    try {
      return iframe.contentDocument || (iframe.contentWindow && iframe.contentWindow.document);
    } catch (e) {
      return null;
    }
  }

  function iframeWin(iframe) {
    try {
      return iframe.contentWindow || null;
    } catch (e) {
      return null;
    }
  }

  function canvasHasInk(canvas) {
    try {
      var ctx = canvas.getContext("2d");
      var w = canvas.width;
      var h = canvas.height;
      if (w < 2 || h < 2) return false;
      var stepX = Math.max(1, Math.floor(w / 40));
      var stepY = Math.max(1, Math.floor(h / 40));
      var data = ctx.getImageData(0, 0, w, h).data;
      var colored = 0;
      var samples = 0;
      for (var y = 0; y < h; y += stepY) {
        for (var x = 0; x < w; x += stepX) {
          var i = (y * w + x) * 4;
          samples++;
          if (data[i + 3] > 8 && (data[i] < 250 || data[i + 1] < 250 || data[i + 2] < 250))
            colored++;
        }
      }
      var ok = colored >= Math.max(3, samples * 0.008);
      console.log("[dash-shot] ink", colored + "/" + samples, ok ? "ok" : "blank");
      return ok;
    } catch (e) {
      console.warn("[dash-shot] ink failed", e);
      return true;
    }
  }

  function isTransparentPaint(val) {
    if (!val || val === "none") return true;
    var v = String(val).replace(/\s+/g, "").toLowerCase();
    return (
      v === "transparent" ||
      v === "rgba(0,0,0,0)" ||
      v === "hsla(0,0%,0%,0)"
    );
  }

  /** SVG из iframe: стили + fill у text/label (иначе подписи пропадают). */
  function cloneSvgInlined(svg, win) {
    var getStyle =
      (win && win.getComputedStyle.bind(win)) ||
      window.getComputedStyle.bind(window);
    var clone = svg.cloneNode(true);
    var srcNodes = svg.querySelectorAll("*");
    var dstNodes = clone.querySelectorAll("*");
    var props = [
      "fill",
      "stroke",
      "stroke-width",
      "stroke-opacity",
      "fill-opacity",
      "opacity",
      "font",
      "font-size",
      "font-family",
      "font-weight",
      "font-style",
      "color",
      "visibility",
      "display",
      "text-anchor",
      "dominant-baseline",
      "letter-spacing",
    ];
    for (var i = 0; i < srcNodes.length && i < dstNodes.length; i++) {
      try {
        var src = srcNodes[i];
        var dst = dstNodes[i];
        var cs = getStyle(src);
        var tag = (src.tagName || "").toLowerCase();
        var isText = tag === "text" || tag === "tspan";
        for (var p = 0; p < props.length; p++) {
          var val = cs.getPropertyValue(props[p]);
          if (!val || val === "none") continue;
          if (props[p] === "fill" && isTransparentPaint(val) && !isText) continue;
          dst.setAttribute(props[p], val);
        }
        if (isText) {
          var fill = cs.getPropertyValue("fill");
          var color = cs.getPropertyValue("color") || cs.color;
          if (isTransparentPaint(fill)) {
            dst.setAttribute(
              "fill",
              color && !isTransparentPaint(color) ? color : "#000000"
            );
          }
          if (!dst.getAttribute("font-size")) {
            dst.setAttribute(
              "font-size",
              cs.fontSize || cs.getPropertyValue("font-size") || "10px"
            );
          }
          if (!dst.getAttribute("font-family")) {
            dst.setAttribute(
              "font-family",
              cs.fontFamily ||
                cs.getPropertyValue("font-family") ||
                "Arial, sans-serif"
            );
          }
        }
      } catch (e) {}
    }
    var box = svg.getBoundingClientRect();
    var w = Math.max(1, Math.round(box.width || svg.clientWidth || 0));
    var h = Math.max(1, Math.round(box.height || svg.clientHeight || 0));
    if (w < 2 || h < 2) {
      try {
        var vb = svg.viewBox && svg.viewBox.baseVal;
        if (vb && vb.width > 0) {
          w = Math.round(vb.width);
          h = Math.round(vb.height);
        }
      } catch (e) {}
    }
    w = Math.max(1, w);
    h = Math.max(1, h);
    clone.setAttribute("width", String(w));
    clone.setAttribute("height", String(h));
    if (!clone.getAttribute("xmlns")) {
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    }
    if (!clone.getAttribute("viewBox")) {
      try {
        var vb2 = svg.viewBox && svg.viewBox.baseVal;
        if (vb2 && vb2.width > 0) {
          clone.setAttribute(
            "viewBox",
            vb2.x + " " + vb2.y + " " + vb2.width + " " + vb2.height
          );
        } else {
          clone.setAttribute("viewBox", "0 0 " + w + " " + h);
        }
      } catch (e) {
        clone.setAttribute("viewBox", "0 0 " + w + " " + h);
      }
    }
    return { clone: clone, w: w, h: h };
  }

  function svgToCanvas(svg, win) {
    var prepared = cloneSvgInlined(svg, win);
    var texts = prepared.clone.querySelectorAll("text, tspan");
    console.log("[dash-shot] svg texts", texts.length);
    var xml = new XMLSerializer().serializeToString(prepared.clone);
    if (xml.indexOf("xmlns") < 0) {
      xml = xml.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"');
    }
    var url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
    return loadImage(url).then(function (img) {
      var canvas = document.createElement("canvas");
      canvas.width = Math.max(1, prepared.w * SCALE);
      canvas.height = Math.max(1, prepared.h * SCALE);
      canvas.setAttribute("data-sti-map-bake", "1");
      var ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      if (!canvasHasInk(canvas)) throw new Error("svg canvas blank");
      return { canvas: canvas, w: prepared.w, h: prepared.h };
    });
  }

  /**
   * Весь iframe (SVG + HTML-label’ы) → canvas.
   * Размер = CSS-бокс iframe.
   */
  async function iframeDocToCanvas(iframe) {
    var doc = iframeDoc(iframe);
    if (!doc || !doc.body) throw new Error("no iframe doc");
    var ir = iframe.getBoundingClientRect();
    var w = Math.max(1, Math.round(ir.width || iframe.clientWidth || 0));
    var h = Math.max(1, Math.round(ir.height || iframe.clientHeight || 0));
    var labelNodes = doc.querySelectorAll(
      "text, tspan, [class*='abel'], [class*='Label'], [class*='label']"
    );
    console.log(
      "[dash-shot] iframe labels",
      labelNodes.length,
      "box",
      w + "x" + h
    );
    var target = doc.documentElement || doc.body;
    var canvas = await window.html2canvas(target, {
      scale: SCALE,
      useCORS: true,
      allowTaint: true,
      backgroundColor: "#ffffff",
      logging: false,
      foreignObjectRendering: false,
      width: w,
      height: h,
      windowWidth: Math.max(w, target.scrollWidth || 0),
      windowHeight: Math.max(h, target.scrollHeight || 0),
      x: 0,
      y: 0,
    });
    canvas.setAttribute("data-sti-map-bake", "1");
    if (!canvasHasInk(canvas)) throw new Error("iframe html2canvas blank");
    return { canvas: canvas, w: w, h: h };
  }

  /**
   * Bake ровно в CSS-бокс iframe относительно host.
   * z-index 0 — titlePanel/кнопки поверх остаются видимыми.
   */
  function applyIframeBox(el, iframe, host) {
    var hostEl = host || iframe.parentElement;
    var ir = iframe.getBoundingClientRect();
    var hr = hostEl.getBoundingClientRect();
    el.style.position = "absolute";
    el.style.left = Math.round(ir.left - hr.left) + "px";
    el.style.top = Math.round(ir.top - hr.top) + "px";
    el.style.width = Math.round(ir.width) + "px";
    el.style.height = Math.round(ir.height) + "px";
    el.style.zIndex = "0";
    el.style.margin = "0";
    el.style.border = "none";
    el.style.display = "block";
    el.style.pointerEvents = "none";
    el.style.objectFit = "fill";
  }

  function typeName(c) {
    try {
      if (!c) return "";
      if (c.componentType) return String(c.componentType);
      if (c.constructor && c.constructor.name) return c.constructor.name;
    } catch (e) {}
    return "";
  }

  function walkComponents(root, fn) {
    if (!root) return;
    fn(root);
    var kids = root.components;
    if (!kids || kids.count == null) return;
    for (var i = 0; i < kids.count; i++) walkComponents(kids.getByIndex(i), fn);
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

  function findReport() {
    if (window.__dashReport) return window.__dashReport;
    if (window.__dashViewer && window.__dashViewer.report)
      return window.__dashViewer.report;
    if (window.viewer && window.viewer.report) return window.viewer.report;
    return null;
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
    return /OnlineMap/i.test(typeName(c));
  }

  function isTableElement(c) {
    if (!c) return false;
    try {
      var I =
        Stimulsoft.Report &&
        Stimulsoft.Report.Dashboard &&
        Stimulsoft.Report.Dashboard.IStiTableElement;
      if (I && c.is && c.is(I)) return true;
    } catch (e) {}
    return /StiTableElement/i.test(typeName(c)) && !/Pivot/i.test(typeName(c));
  }

  function isPivotTableElement(c) {
    if (!c) return false;
    try {
      var I =
        Stimulsoft.Report &&
        Stimulsoft.Report.Dashboard &&
        Stimulsoft.Report.Dashboard.IStiPivotTableElement;
      if (I && c.is && c.is(I)) return true;
    } catch (e) {}
    return /PivotTable/i.test(typeName(c));
  }

  function isExportableTable(c) {
    return isTableElement(c) || isPivotTableElement(c);
  }

  function exportElementAsync(element, format) {
    return new Promise(function (resolve, reject) {
      try {
        var Tools =
          Stimulsoft.Dashboard &&
          Stimulsoft.Dashboard.Export &&
          Stimulsoft.Dashboard.Export.StiDashboardExportTools;
        if (!Tools || typeof Tools.exportAsync !== "function") {
          reject(new Error("StiDashboardExportTools.exportAsync нет"));
          return;
        }
        Tools.exportAsync(
          function (data) {
            if (data == null) reject(new Error("empty export"));
            else resolve(data);
          },
          element,
          format
        );
      } catch (e) {
        reject(e);
      }
    });
  }

  function stiBytesToBase64(data) {
    var bytes;
    if (data instanceof ArrayBuffer) bytes = new Uint8Array(data);
    else if (data && data.buffer instanceof ArrayBuffer)
      bytes = new Uint8Array(
        data.buffer,
        data.byteOffset || 0,
        data.byteLength || data.length
      );
    else if (typeof data === "string") return btoa(data);
    else if (Array.isArray(data)) bytes = new Uint8Array(data);
    else {
      data = normalizeExportBytes(data);
      if (!data) throw new Error("unknown export payload");
      if (typeof data === "string") return btoa(unescape(encodeURIComponent(data)));
      bytes = data;
    }
    var chunk = 0x8000;
    var parts = [];
    for (var i = 0; i < bytes.length; i += chunk) {
      parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + chunk)));
    }
    return btoa(parts.join(""));
  }

  async function postExportFile(name, data_b64, ext) {
    var res = await fetch("/api/screenshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, data_b64: data_b64, ext: ext || "" }),
    });
    var body = await res.json().catch(function () {
      return {};
    });
    if (!res.ok) throw new Error(body.error || String(res.status));
    console.log("[dash-shot] server file", body.name || name, body.path || "");
    return body;
  }

  /** Имена Table/Pivot, видимых на текущей панели. */
  function visibleTableNames(root) {
    var names = {};
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      var t = attrType(nodes[i]);
      if (/TableElement|PivotTable/i.test(t)) {
        var n = attrName(nodes[i]);
        if (n) names[n] = true;
      }
    }
    return names;
  }

  /**
   * Save на Table/Pivot → Image/Excel/Data; PDF — через export API (все строки).
   * Скачивает PDF + кладёт в exports/.
   */
  async function exportTablesToPdf(report, root, labelPrefix) {
    if (!report) return { saved: 0, names: [] };
    var Pdf =
      Stimulsoft.Report && Stimulsoft.Report.StiExportFormat
        ? Stimulsoft.Report.StiExportFormat.Pdf
        : null;
    if (Pdf == null) {
      console.warn("[dash-shot] StiExportFormat.Pdf нет");
      return { saved: 0, names: [] };
    }
    var visible = visibleTableNames(root || document);
    var comps = collectAll(report).filter(isExportableTable);
    console.log(
      "[dash-shot] tables for pdf",
      comps.map(function (c) {
        return c.name || typeName(c);
      }),
      "visible",
      Object.keys(visible),
      "prefix",
      labelPrefix || ""
    );
    if (Object.keys(visible).length) {
      var onPage = comps.filter(function (c) {
        return c.name && visible[c.name];
      });
      if (onPage.length) comps = onPage;
    }
    if (!comps.length) {
      console.warn("[dash-shot] Table/Pivot не найден");
      return { saved: 0, names: [] };
    }
    var prefix = labelPrefix ? safeSlug(labelPrefix) + "__" : "";
    var saved = 0;
    var names = [];
    for (var i = 0; i < comps.length; i++) {
      var el = comps[i];
      var base =
        prefix +
        String(el.name || "Table" + (i + 1)).replace(/\s+/g, "_") +
        "_" +
        stamp();
      var pdfName = base + ".pdf";
      setStatus("PDF " + (el.name || base) + "…");
      try {
        var data = await exportElementAsync(el, Pdf);
        try {
          Stimulsoft.System.StiObject.saveAs(
            data,
            pdfName,
            "application/pdf"
          );
        } catch (e) {
          console.warn("[dash-shot] saveAs pdf", e);
          try {
            var b64dl = stiBytesToBase64(data);
            var bin = atob(b64dl);
            var arr = new Uint8Array(bin.length);
            for (var j = 0; j < bin.length; j++) arr[j] = bin.charCodeAt(j);
            downloadBlob(new Blob([arr], { type: "application/pdf" }), pdfName);
          } catch (e2) {
            console.warn("[dash-shot] blob pdf", e2);
          }
        }
        try {
          await postExportFile(pdfName, stiBytesToBase64(data), "pdf");
        } catch (e) {
          console.warn("[dash-shot] pdf upload", e);
        }
        saved++;
        names.push(pdfName);
        console.log("[dash-shot] table pdf", pdfName);
      } catch (err) {
        console.warn("[dash-shot] table pdf failed", el.name, err);
      }
    }
    return { saved: saved, names: names };
  }

  function paintBanner(canvas, text) {
    if (!text || !canvas) return;
    var ctx = canvas.getContext("2d");
    var pad = 10 * SCALE;
    var fontPx = 16 * SCALE;
    ctx.save();
    ctx.font = "bold " + fontPx + "px ui-sans-serif,system-ui,sans-serif";
    var metrics = ctx.measureText(text);
    var barH = fontPx + pad * 2;
    ctx.fillStyle = "rgba(20,20,20,0.88)";
    ctx.fillRect(0, 0, canvas.width, barH);
    ctx.fillStyle = "#ffffff";
    ctx.textBaseline = "middle";
    ctx.fillText(text, pad, barH / 2);
    // тонкая цветная полоска чтобы сразу видно «метка»
    ctx.fillStyle = "#3b82f6";
    ctx.fillRect(0, barH - 3 * SCALE, Math.min(canvas.width, metrics.width + pad * 2), 3 * SCALE);
    ctx.restore();
  }

  function normalizeExportBytes(data) {
    if (!data) return null;
    if (data.data && (data.data.length || data.data.byteLength)) data = data.data;
    if (typeof data === "string") return data;
    if (data instanceof ArrayBuffer) data = new Uint8Array(data);
    if (data instanceof Uint8Array) return data;
    if (Array.isArray(data)) return new Uint8Array(data);
    return null;
  }

  function bytesToPngDataUrl(data) {
    data = normalizeExportBytes(data);
    if (!data) return null;
    if (typeof data === "string") {
      if (data.indexOf("data:") === 0) return data;
      if (data.length > 100) return "data:image/png;base64," + data;
      return null;
    }
    if (!data.length) return null;
    var bin = "";
    var step = 0x8000;
    for (var i = 0; i < data.length; i += step) {
      bin += String.fromCharCode.apply(
        null,
        data.subarray(i, Math.min(i + step, data.length))
      );
    }
    return "data:image/png;base64," + btoa(bin);
  }

  function exportOnlineMapPng(mapEl, cssW, cssH) {
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
        s.imageQuality = 500;
        var w = Number(mapEl.width) || 0;
        var h = Number(mapEl.height) || 0;
        if (w > 0 && h > 0) {
          s.width = w;
          s.height = h;
        } else if (cssW > 0 && cssH > 0) {
          s.width = (cssW / 96) * 100;
          s.height = (cssH / 96) * 100;
        }
        if (typeof Tools.exportAsync === "function") {
          Tools.exportAsync(resolve, mapEl, s);
        } else {
          Promise.resolve(Tools.export(mapEl, s)).then(resolve).catch(reject);
        }
      } catch (e) {
        reject(e);
      }
    });
  }

  /** Все хосты карт: RegionMap / OnlineMap / любой element с .frame iframe */
  function collectMapHosts(root) {
    var out = [];
    var seen = [];
    function add(el) {
      if (!el || seen.indexOf(el) >= 0) return;
      seen.push(el);
      out.push(el);
    }
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var t = attrType(el);
      if (/RegionMap|OnlineMap/i.test(t)) add(el);
      if (el.frame) add(el);
    }
    // fallback: iframe внутри dashboard
    var iframes = root.querySelectorAll("iframe");
    for (var j = 0; j < iframes.length; j++) {
      var host = iframes[j].parentElement;
      while (host && host !== root) {
        if (host.elementAttributes || host.frame) {
          add(host);
          break;
        }
        host = host.parentElement;
      }
    }
    console.log(
      "[dash-shot] map hosts",
      out.length,
      out.map(function (el) {
        var iframe = el.frame || el.querySelector("iframe");
        var doc = iframe ? iframeDoc(iframe) : null;
        var svgCount = doc ? doc.querySelectorAll("svg").length : 0;
        return {
          name: attrName(el),
          type: attrType(el),
          iframe: !!iframe,
          svgInIframe: svgCount,
        };
      })
    );
    return out;
  }

  /**
   * RegionMap: весь iframe (карта + labels) → canvas в host, iframe hide.
   * Fallback: SVG с inline fill у text. OnlineMap → Stimulsoft Image.
   */
  async function bakeMaps(root, report) {
    var restorers = [];
    var hosts = collectMapHosts(root);
    setStatus("карты " + hosts.length);

    for (var i = 0; i < hosts.length; i++) {
      var host = hosts[i];
      var name = attrName(host) || attrType(host) || "map" + i;
      var iframe = host.frame || host.querySelector("iframe");
      if (!iframe) {
        console.warn("[dash-shot] no iframe", name);
        continue;
      }

      var doc = iframeDoc(iframe);
      var win = iframeWin(iframe);
      var svg = doc && doc.querySelector("svg");

      console.log("[dash-shot] host", name, {
        hasDoc: !!doc,
        svg: !!svg,
        type: attrType(host),
        titleH: host.titlePanel ? host.titlePanel.offsetHeight : 0,
        iframeBox: {
          w: iframe.offsetWidth,
          h: iframe.offsetHeight,
        },
      });

      if (getComputedStyle(host).position === "static") {
        restorers.push({ el: host, position: host.style.position });
        host.style.position = "relative";
      }

      var baked = null;

      // 1) весь iframe — подписи (SVG text / HTML labels) сохраняются
      try {
        baked = await iframeDocToCanvas(iframe);
        console.log("[dash-shot] baked iframe doc", name, baked.w, "x", baked.h);
      } catch (e) {
        console.warn("[dash-shot] iframe html2canvas failed", name, e);
      }

      // 2) fallback SVG с явным fill у text
      if (!baked && svg) {
        try {
          baked = await svgToCanvas(svg, win);
          console.log("[dash-shot] baked iframe SVG", name, baked.w, "x", baked.h);
        } catch (e) {
          console.warn("[dash-shot] iframe SVG bake failed", name, e);
        }
      }

      if (baked) {
        applyIframeBox(baked.canvas, iframe, host);
        restorers.push({ iframe: iframe, display: iframe.style.display });
        iframe.style.display = "none";
        restorers.push({ canvas: baked.canvas, host: host });
        if (iframe.nextSibling) host.insertBefore(baked.canvas, iframe.nextSibling);
        else host.appendChild(baked.canvas);
        console.log("[dash-shot] placed bake", name, baked.canvas.style.cssText);
        continue;
      }

      // OnlineMap / нет SVG — пробуем Stimulsoft Image
      if (report && /OnlineMap/i.test(attrType(host))) {
        try {
          var maps = collectAll(report).filter(isOnlineMapElement);
          var mapEl = null;
          for (var m = 0; m < maps.length; m++) {
            if (maps[m].name === attrName(host)) {
              mapEl = maps[m];
              break;
            }
          }
          mapEl = mapEl || maps[0];
          if (!mapEl) throw new Error("no online map el");
          var rect = iframe.getBoundingClientRect();
          var data = await exportOnlineMapPng(mapEl, rect.width, rect.height);
          var url = bytesToPngDataUrl(data);
          if (!url || url.length < 200) throw new Error("empty sti png");
          var imgEl = document.createElement("img");
          imgEl.setAttribute("data-sti-map-bake", "1");
          imgEl.src = url;
          applyIframeBox(imgEl, iframe, host);
          var probeImg = await loadImage(url);
          var probe = document.createElement("canvas");
          probe.width = Math.max(1, Math.round(rect.width));
          probe.height = Math.max(1, Math.round(rect.height));
          probe.getContext("2d").drawImage(probeImg, 0, 0, probe.width, probe.height);
          if (!canvasHasInk(probe)) throw new Error("sti blank");
          restorers.push({ iframe: iframe, display: iframe.style.display });
          iframe.style.display = "none";
          restorers.push({ img: imgEl });
          if (iframe.nextSibling) host.insertBefore(imgEl, iframe.nextSibling);
          else host.appendChild(imgEl);
          console.log("[dash-shot] baked OnlineMap img", name);
        } catch (e) {
          console.warn("[dash-shot] OnlineMap bake skipped", name, e);
        }
      }
    }

    return function restore() {
      restorers.forEach(function (x) {
        try {
          if (x.canvas && x.canvas.parentNode) x.canvas.parentNode.removeChild(x.canvas);
        } catch (e) {}
        try {
          if (x.img && x.img.parentNode) x.img.parentNode.removeChild(x.img);
        } catch (e) {}
        try {
          if (x.iframe) x.iframe.style.display = x.display || "";
        } catch (e) {}
        try {
          if (x.el && x.position != null) x.el.style.position = x.position;
        } catch (e) {}
      });
    };
  }

  function isFilterHost(el) {
    if (!el) return false;
    if (/ComboBox|ListBox|DatePicker|TreeViewBox|NumberBox/i.test(attrType(el)))
      return true;
    try {
      var ca =
        el.elementAttributes && el.elementAttributes.contentAttributes;
      // Stimulsoft filter controls: selectionMode + contentPanel (CreateComboBoxElementContent)
      if (ca && ca.selectionMode != null && el.contentPanel) return true;
    } catch (e) {}
    return false;
  }

  function itemLabel(item) {
    if (item == null) return "";
    if (typeof item !== "object") return String(item);
    return String(
      item.label ||
        item.text ||
        item.displayString ||
        item.display ||
        item.caption ||
        item.value ||
        item.key ||
        ""
    );
  }

  function labelForFilterValue(ca, value) {
    var items = (ca && ca.items) || [];
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (!it) continue;
      var key = it.value != null ? it.value : it.key;
      if (String(key) === String(value)) {
        var lab = itemLabel(it);
        if (lab) return lab;
      }
    }
    return value == null ? "" : String(value);
  }

  /**
   * Stimulsoft ComboBox: значение в contentPanel → SmallButton.caption.innerText
   * (не <select>). Также contentAttributes.filters / items / getFilters().
   */
  function readFilterLabel(host) {
    var label = "";

    // 0) обновить caption из выбранных items (p.update в CreateComboBoxElementContent)
    try {
      var cp0 = host.contentPanel;
      if (cp0) {
        for (var u0 = 0; u0 < cp0.childNodes.length; u0++) {
          var b0 = cp0.childNodes[u0];
          if (b0 && typeof b0.update === "function") {
            try {
              b0.update();
            } catch (e0) {}
          }
        }
      }
    } catch (e) {}

    // 1) caption кнопки в contentPanel (CreateComboBoxElementContent / DatePicker)
    try {
      var cp = host.contentPanel;
      if (cp) {
        for (var i = 0; i < cp.childNodes.length; i++) {
          var btn = cp.childNodes[i];
          if (btn && btn.caption) {
            var cap = (btn.caption.innerText || btn.caption.textContent || "").trim();
            if (cap) return cap;
          }
        }
        var raw = (cp.innerText || cp.textContent || "").trim();
        if (raw && raw.length > 0) label = raw;
      }
    } catch (e) {}

    // 2) getFilters() на DOM-элементе
    if (!label) {
      try {
        if (typeof host.getFilters === "function") {
          var gf = host.getFilters();
          if (gf && gf.length) {
            var ca0 =
              host.elementAttributes && host.elementAttributes.contentAttributes;
            var parts = [];
            for (var g = 0; g < gf.length; g++) {
              var fv = gf[g] && (gf[g].value != null ? gf[g].value : gf[g]);
              var lab = labelForFilterValue(ca0, fv);
              if (lab) parts.push(lab);
            }
            if (parts.length) label = parts.join(", ");
          }
        }
      } catch (e) {}
    }

    // 3) contentAttributes.filters + items
    if (!label) {
      try {
        var ca =
          host.elementAttributes && host.elementAttributes.contentAttributes;
        if (ca) {
          var filters = ca.filters || [];
          if (filters.length) {
            var labs = [];
            for (var f = 0; f < filters.length; f++) {
              var v = filters[f] && filters[f].value;
              var l2 = labelForFilterValue(ca, v);
              if (l2) labs.push(l2);
            }
            if (labs.length) label = labs.join(", ");
          } else if (ca.showAllValue) {
            label = "All";
          }
        }
      } catch (e) {}
    }

    // 4) report component.userFilters по имени
    if (!label) {
      try {
        var report = findReport();
        var name = attrName(host);
        if (report && name) {
          var comps = collectAll(report);
          for (var c = 0; c < comps.length; c++) {
            var el = comps[c];
            if (!el || el.name !== name) continue;
            var uf = el.userFilters;
            if (uf && uf.length) {
              var parts2 = [];
              for (var u = 0; u < uf.length; u++) {
                var uv = uf.get ? uf.get(u) : uf[u];
                if (uv && uv.value != null && String(uv.value) !== "")
                  parts2.push(String(uv.value));
              }
              if (parts2.length) label = parts2.join(", ");
            }
            if (!label && el.initialValue != null && String(el.initialValue) !== "")
              label = String(el.initialValue);
            break;
          }
        }
      } catch (e) {}
    }

    // 5) select / input fallback
    if (!label) {
      try {
        var sel = host.querySelector("select");
        if (sel && sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) {
          label =
            sel.options[sel.selectedIndex].textContent ||
            sel.options[sel.selectedIndex].text ||
            sel.value ||
            "";
        }
      } catch (e) {}
    }
    if (!label) {
      try {
        var inp = host.querySelector(
          "input:not([type=hidden]), textarea, [contenteditable='true']"
        );
        if (inp)
          label =
            (inp.value != null && String(inp.value)) || inp.textContent || "";
      } catch (e) {}
    }

    return String(label || "").trim();
  }

  /**
   * html2canvas плохо рисует Stimulsoft ComboBox caption.
   * Печём текст фильтра поверх contentPanel.
   */
  function bakeFilterValues(root) {
    var restorers = [];
    var hosts = [];
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      if (isFilterHost(nodes[i])) hosts.push(nodes[i]);
    }
    var selects = root.querySelectorAll("select");
    console.log("[dash-shot] filter hosts", hosts.length, "selects", selects.length);

    function placeLabel(target, text, host) {
      if (!text || !target) return;
      var ir = target.getBoundingClientRect();
      var hr = host.getBoundingClientRect();
      if (ir.width < 2 || ir.height < 2) return;
      if (getComputedStyle(host).position === "static") {
        restorers.push({ el: host, position: host.style.position });
        host.style.position = "relative";
      }
      var div = document.createElement("div");
      div.setAttribute("data-sti-filter-bake", "1");
      div.textContent = text;
      var cs = window.getComputedStyle(target);
      var bg = cs.backgroundColor;
      if (!bg || bg === "rgba(0, 0, 0, 0)" || bg === "transparent") bg = "#ffffff";
      var color = cs.color || "#111111";
      // если текст почти белый на белом — форсим тёмный
      if (color === "rgb(255, 255, 255)" || color === "#ffffff") color = "#111111";
      div.style.cssText = [
        "position:absolute",
        "left:" + Math.round(ir.left - hr.left) + "px",
        "top:" + Math.round(ir.top - hr.top) + "px",
        "width:" + Math.round(ir.width) + "px",
        "height:" + Math.round(ir.height) + "px",
        "box-sizing:border-box",
        "display:flex",
        "align-items:center",
        "padding:0 8px",
        "overflow:hidden",
        "white-space:nowrap",
        "background:" + bg,
        "color:" + color,
        "font:" + (cs.font || "12px sans-serif"),
        "border-radius:" + (cs.borderRadius || "0"),
        "z-index:5",
        "pointer-events:none",
      ].join(";");
      host.appendChild(div);
      restorers.push({ div: div });
      // не visibility:hidden на весь contentPanel — только прячем детей-кнопок
      try {
        for (var c = 0; c < target.childNodes.length; c++) {
          var ch = target.childNodes[c];
          if (ch && ch.nodeType === 1 && ch.getAttribute("data-sti-filter-bake") !== "1") {
            restorers.push({ el: ch, visibility: ch.style.visibility });
            ch.style.visibility = "hidden";
          }
        }
      } catch (e) {}
    }

    for (var h = 0; h < hosts.length; h++) {
      var host = hosts[h];
      var label = readFilterLabel(host);
      var ca =
        host.elementAttributes && host.elementAttributes.contentAttributes;
      console.log("[dash-shot] filter", {
        name: attrName(host),
        type: attrType(host),
        label: label,
        selectionMode: ca && ca.selectionMode,
        filters: ca && ca.filters,
        items: ca && ca.items && ca.items.length,
        hasContentPanel: !!host.contentPanel,
        captionProbe: (function () {
          try {
            var cp = host.contentPanel;
            if (!cp || !cp.childNodes.length) return null;
            var b = cp.childNodes[0];
            return b && b.caption
              ? String(b.caption.innerText || "").slice(0, 80)
              : String(cp.innerText || "").slice(0, 80);
          } catch (e) {
            return String(e);
          }
        })(),
      });
      if (!label) continue;
      var paintTarget = host.contentPanel || host;
      // не красить поверх title
      if (host.titlePanel && paintTarget === host) {
        paintTarget = host.contentPanel || host;
      }
      placeLabel(paintTarget, label, host);
    }

    for (var s = 0; s < selects.length; s++) {
      var sel = selects[s];
      if (sel.closest && hosts.some(function (hh) { return hh.contains(sel); }))
        continue;
      var text = "";
      if (sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) {
        text =
          sel.options[sel.selectedIndex].textContent ||
          sel.options[sel.selectedIndex].text ||
          "";
      }
      if (!text) continue;
      var parent = sel.parentElement || root;
      placeLabel(sel, text, parent);
    }

    return function restore() {
      restorers.forEach(function (x) {
        try {
          if (x.div && x.div.parentNode) x.div.parentNode.removeChild(x.div);
        } catch (e) {}
        try {
          if (x.el && x.visibility != null) x.el.style.visibility = x.visibility;
        } catch (e) {}
        try {
          if (x.el && x.position != null) x.el.style.position = x.position;
        } catch (e) {}
      });
    };
  }

  function onCloneFixFormValues(doc) {
    try {
      var selects = doc.querySelectorAll("select");
      for (var i = 0; i < selects.length; i++) {
        var sel = selects[i];
        if (sel.getAttribute("data-sti-filter-bake")) continue;
        var text = "";
        if (sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) {
          text =
            sel.options[sel.selectedIndex].textContent ||
            sel.options[sel.selectedIndex].text ||
            "";
        }
        if (!text) continue;
        var div = doc.createElement("div");
        div.textContent = text;
        div.setAttribute("style", sel.getAttribute("style") || "");
        div.style.display = "flex";
        div.style.alignItems = "center";
        div.style.paddingLeft = "8px";
        div.style.overflow = "hidden";
        div.style.whiteSpace = "nowrap";
        div.style.background = "#fff";
        div.style.color = "#111";
        if (sel.parentNode) sel.parentNode.replaceChild(div, sel);
      }
      var inputs = doc.querySelectorAll("input, textarea");
      for (var j = 0; j < inputs.length; j++) {
        var inp = inputs[j];
        try {
          inp.setAttribute("value", inp.value || "");
          if (inp.tagName === "TEXTAREA") inp.textContent = inp.value || "";
        } catch (e) {}
      }
    } catch (e) {
      console.warn("[dash-shot] onclone filters", e);
    }
  }

  /**
   * @param {object} [opts]
   * @param {string} [opts.label] — slug в имени файла
   * @param {string} [opts.banner] — текст «Нажато: …» на PNG
   * @param {boolean} [opts.quiet] — не трогать disabled на кнопках бара
   */
  async function takeScreenshot(opts) {
    opts = opts || {};
    var label = opts.label ? safeSlug(opts.label) : "";
    var banner = opts.banner || (label ? "Нажато: " + opts.label : "");
    var root = captureRoot();
    if (!root) {
      setStatus("панель не найдена");
      return null;
    }
    var btn = document.getElementById("dashShotBtn");
    var scanBtn = document.getElementById("dashScanBtn");
    if (!opts.quiet) {
      if (btn) btn.disabled = true;
      if (scanBtn) scanBtn.disabled = true;
    }
    var restoreMaps = function () {};
    var restoreFilters = function () {};
    setStatus(banner ? banner + "…" : "снимок…");
    var result = { png: null, pdfs: [], label: opts.label || "", banner: banner };
    try {
      await loadScript(HTML2CANVAS);
      var report = findReport();

      setStatus("карты из iframe…");
      restoreMaps = (await bakeMaps(root, report)) || function () {};

      setStatus("фильтры…");
      restoreFilters = bakeFilterValues(root) || function () {};

      await new Promise(function (res) {
        requestAnimationFrame(function () {
          setTimeout(res, 80);
        });
      });

      setStatus("рендер…");
      var canvas = await window.html2canvas(root, {
        scale: SCALE,
        useCORS: true,
        allowTaint: true,
        backgroundColor: "#ffffff",
        logging: false,
        foreignObjectRendering: false,
        ignoreElements: function (el) {
          return !!(el && el.id === "dashShotBar");
        },
        onclone: function (doc) {
          onCloneFixFormValues(doc);
        },
      });

      restoreFilters();
      restoreMaps();
      restoreFilters = function () {};
      restoreMaps = function () {};

      if (banner) paintBanner(canvas, banner);

      var blob = await new Promise(function (resolve, reject) {
        canvas.toBlob(function (b) {
          if (b) resolve(b);
          else reject(new Error("toBlob failed"));
        }, "image/png");
      });
      var name =
        (label ? label + "__" : "") + "dashboard_" + stamp() + ".png";
      downloadBlob(blob, name);
      result.png = name;
      try {
        var b64 = await blobToBase64(blob);
        var body = await postExportFile(name, b64, "png");
        result.png = body.name || name;
        setStatus("saved " + result.png);
      } catch (err) {
        setStatus("download ok");
        console.warn(err);
      }

      setStatus((banner || "таблица") + " → PDF…");
      try {
        var pdfResult = await exportTablesToPdf(report, root, label);
        result.pdfs = (pdfResult && pdfResult.names) || [];
        var nPdf = (pdfResult && pdfResult.saved) || 0;
        setStatus(
          "saved " +
            result.png +
            (nPdf ? " | +" + nPdf + " pdf" : " | pdf нет") +
            (banner ? " | " + banner : "")
        );
      } catch (err) {
        console.warn("[dash-shot] tables pdf", err);
      }
      return result;
    } catch (err) {
      console.error(err);
      setStatus(String((err && err.message) || err));
      return result;
    } finally {
      try {
        restoreFilters();
      } catch (e) {}
      try {
        restoreMaps();
      } catch (e) {}
      if (!opts.quiet) {
        if (btn) btn.disabled = false;
        if (scanBtn) scanBtn.disabled = false;
      }
    }
  }

  var GEOM_CLICK_CAP = 40; // макс. регионов/точек на одну карту/чарт

  function isButtonHost(el) {
    return /^StiButtonElement$/i.test(attrType(el));
  }

  function isListBoxHost(el) {
    return /StiListBoxElement|StiTreeViewElement/i.test(attrType(el));
  }

  function isComboHost(el) {
    return /StiComboBoxElement|StiTreeViewBoxElement/i.test(attrType(el));
  }

  function isGeomHost(el) {
    return /StiRegionMapElement|StiChartElement/i.test(attrType(el));
  }

  function isInteractionHost(el) {
    var t = attrType(el);
    if (!/StiImageElement|StiTextElement|StiIndicatorElement|StiCardsElement/i.test(t))
      return false;
    try {
      var ca = el.elementAttributes && el.elementAttributes.contentAttributes;
      var ix = ca && ca.interaction;
      return !!(ix && ix.onClick && ix.onClick !== "None");
    } catch (e) {
      return false;
    }
  }

  function isVisibleBox(el) {
    if (!el || !el.getBoundingClientRect) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;
    try {
      var cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0)
        return false;
    } catch (e) {}
    return true;
  }

  function buttonLabel(host) {
    try {
      var ca =
        host.elementAttributes && host.elementAttributes.contentAttributes;
      if (ca && ca.buttonText != null && String(ca.buttonText).trim())
        return String(ca.buttonText).trim();
    } catch (e) {}
    try {
      var texts = host.querySelectorAll("text");
      for (var i = 0; i < texts.length; i++) {
        var t = (texts[i].textContent || "").trim();
        // иконки Stimulsoft часто 1 символ из icon font — берём длиннее
        if (t && t.length > 1) return t;
      }
      for (var j = 0; j < texts.length; j++) {
        var t2 = (texts[j].textContent || "").trim();
        if (t2) return t2;
      }
    } catch (e) {}
    try {
      var raw = (host.innerText || host.textContent || "").trim().replace(/\s+/g, " ");
      if (raw) return raw.slice(0, 80);
    } catch (e) {}
    return attrName(host) || "button";
  }

  function listItemLabel(item) {
    if (!item) return "item";
    if (item.label != null && String(item.label).trim()) return String(item.label).trim();
    if (item.value != null && String(item.value).trim()) return String(item.value).trim();
    try {
      var t = (item.innerText || item.textContent || "").trim().replace(/\s+/g, " ");
      if (t) return t.slice(0, 80);
    } catch (e) {}
    return "item";
  }

  /** Лицо кнопки: contentPanel/svg, не огромная пустая ячейка. */
  function buttonFace(host) {
    if (!host) return host;
    var face =
      host.mainSvg ||
      (host.contentPanel &&
        (host.contentPanel.querySelector("svg") || host.contentPanel)) ||
      host.querySelector("svg") ||
      host;
    if (!isVisibleBox(face)) return host;
    return face;
  }

  /**
   * Пункт ListBox: как живой клик Stimulsoft → ApplyFilters → перерисовка.
   * CheckBox: button.action() сам тоглит isChecked и зовёт item.action/itemAction.
   */
  function activateListItem(item, host) {
    if (!item) return false;
    try {
      if (item.button && typeof item.button.action === "function") {
        item.button.action();
        console.log("[dash-shot] listbox via button.action", listItemLabel(item));
        return true;
      }
    } catch (e) {
      console.warn("[dash-shot] button.action", e);
    }
    try {
      if (typeof item.setChecked === "function") {
        item.setChecked(!item.isChecked);
      }
    } catch (e) {}
    try {
      if (typeof item.action === "function") {
        item.action();
        console.log("[dash-shot] listbox via item.action", listItemLabel(item));
        return true;
      }
    } catch (e) {
      console.warn("[dash-shot] item.action", e);
    }
    try {
      if (typeof item.onclick === "function") {
        item.onclick();
        console.log("[dash-shot] listbox via onclick", listItemLabel(item));
        return true;
      }
    } catch (e) {}

    // прямой ApplyFilters
    try {
      var js = getViewerJs();
      if (js && host && typeof js.ApplyFiltersToDashboardElement === "function") {
        var ca =
          host.elementAttributes && host.elementAttributes.contentAttributes;
        var path = (ca && ca.columnPath) || "";
        var mode = ca && ca.selectionMode;
        var filters;
        if (mode === "Multi") {
          if (typeof host.getFilters === "function") filters = host.getFilters();
          else
            filters = [
              {
                condition: "EqualTo",
                value: item.value,
                path: path,
              },
            ];
        } else {
          filters = item.isAllValue
            ? []
            : [{ condition: "equalTo", value: item.value, path: path }];
        }
        var params =
          typeof host.getFilterParameters === "function"
            ? host.getFilterParameters()
            : null;
        js.ApplyFiltersToDashboardElement(host, filters, null, params);
        console.log("[dash-shot] listbox via ApplyFilters", listItemLabel(item), filters);
        return true;
      }
    } catch (e) {
      console.warn("[dash-shot] ApplyFilters", e);
    }
    return false;
  }

  /** StiButton → postInteraction(DashboardButtonElementApplyEvents) + перерисовка. */
  function activateDashButton(host) {
    if (!host) return false;
    var attrs = host.elementAttributes || {};
    var ca = attrs.contentAttributes || {};
    var name = attrs.name;
    try {
      // как в SubscribeToButtonElementActionEvents
      if (ca.buttonType !== "Button") {
        host.isChecked = !host.isChecked;
        try {
          if (typeof host.repaint === "function") host.repaint();
        } catch (e) {}
      }
    } catch (e) {}

    var js = getViewerJs();
    if (js && name && typeof js.postInteraction === "function") {
      try {
        js.postInteraction({
          action: "DashboardButtonElementApplyEvents",
          dashboardFilteringParameters: {
            elementName: name,
            isChecked: !!host.isChecked,
          },
        });
        console.log("[dash-shot] button postInteraction", name, !!host.isChecked);
        return true;
      } catch (e) {
        console.warn("[dash-shot] postInteraction", e);
      }
    }

    // fallback: нативный click (addEventListener на host)
    try {
      host.dispatchEvent(
        new MouseEvent("click", { bubbles: true, cancelable: true, view: window })
      );
      if (typeof host.click === "function") host.click();
      console.log("[dash-shot] button DOM click fallback", name);
      return true;
    } catch (e) {
      console.warn("[dash-shot] button click fail", e);
      return false;
    }
  }

  /** ComboBox / TreeViewBox: выбрать значение → ApplyFilters. */
  function activateComboValue(host, item) {
    if (!host || !item) return false;
    try {
      if (item.button && typeof item.button.action === "function") {
        item.button.action();
        return true;
      }
      if (typeof item.action === "function") {
        item.action();
        return true;
      }
    } catch (e) {
      console.warn("[dash-shot] combo item.action", e);
    }
    try {
      var js = getViewerJs();
      if (!js || typeof js.ApplyFiltersToDashboardElement !== "function") return false;
      var ca =
        host.elementAttributes && host.elementAttributes.contentAttributes;
      var path = (ca && ca.columnPath) || "";
      var val = item.value != null ? item.value : item;
      var isAll =
        item.isAllValue ||
        val === "StiAllValue" ||
        val === "StiUnCheckAll" ||
        String(item.label || "").toLowerCase() === "all";
      var filters = isAll
        ? []
        : [{ condition: "equalTo", value: val, path: path }];
      var params =
        typeof host.getFilterParameters === "function"
          ? host.getFilterParameters()
          : null;
      js.ApplyFiltersToDashboardElement(host, filters, true, params);
      console.log("[dash-shot] combo ApplyFilters", attrName(host), filters);
      return true;
    } catch (e) {
      console.warn("[dash-shot] combo ApplyFilters", e);
      return false;
    }
  }

  /** RegionMap / Chart geom в iframe → onclick (ApplyFilter / DrillDown). */
  function activateGeom(host, geom) {
    if (!geom) return false;
    try {
      if (typeof geom.onclick === "function") {
        geom.onclick();
        console.log(
          "[dash-shot] geom.onclick",
          geom.getAttribute &&
            (geom.getAttribute("elementargument") ||
              geom.getAttribute("elementvalue") ||
              geom.getAttribute("elementident"))
        );
        return true;
      }
    } catch (e) {
      console.warn("[dash-shot] geom.onclick", e);
    }
    try {
      var js = getViewerJs();
      if (host && typeof host.selectGeom === "function") {
        host.selectGeom(geom, false);
        var filters =
          typeof host.getFilters === "function" ? host.getFilters() : [];
        var params =
          typeof geom.getFilterParameters === "function"
            ? geom.getFilterParameters(
                host.elementAttributes &&
                  host.elementAttributes.contentAttributes &&
                  host.elementAttributes.contentAttributes.interaction &&
                  host.elementAttributes.contentAttributes.interaction
                    .drillDownParameters
              )
            : null;
        if (js && typeof js.ApplyFiltersToDashboardElement === "function") {
          js.ApplyFiltersToDashboardElement(host, filters, null, params);
          return true;
        }
      }
    } catch (e) {
      console.warn("[dash-shot] geom ApplyFilters", e);
    }
    return false;
  }

  /** Image/Text с interaction.onClick. */
  function activateInteractionHost(host) {
    if (!host) return false;
    try {
      if (typeof host.onclick === "function") {
        host.onclick();
        console.log("[dash-shot] interaction onclick", attrName(host));
        return true;
      }
    } catch (e) {}
    try {
      var js = getViewerJs();
      var ca =
        host.elementAttributes && host.elementAttributes.contentAttributes;
      var ix = ca && ca.interaction;
      if (!js || !ix) return false;
      if (ix.onClick === "ShowDashboard" && ix.drillDownPageKey) {
        js.postInteraction({
          action: "DashboardDrillDown",
          drillDownParameters: {
            drillDownPageKey: ix.drillDownPageKey,
            value: "",
            parameters: [],
          },
          pageNumber: 0,
        });
        return true;
      }
      if (ix.onClick === "OpenHyperlink" && ix.hyperlink) {
        // не уводим со страницы — только логируем
        console.log("[dash-shot] skip OpenHyperlink", ix.hyperlink);
        return false;
      }
    } catch (e) {
      console.warn("[dash-shot] interaction", e);
    }
    return false;
  }

  /** Вкладки дашбордов (страницы верхнего уровня). */
  function collectDashboardPages() {
    var js = getViewerJs();
    var panel = js && js.controls && js.controls.dashboardsPanel;
    var buttons = (panel && panel.buttons) || [];
    var pages = [];
    for (var i = 0; i < buttons.length; i++) {
      var b = buttons[i];
      if (!b || b.closeButtonAction || b.isNestedPage) continue;
      if (!b.reportParams && typeof b.action !== "function") continue;
      var label = "";
      try {
        label =
          (b.caption && (b.caption.innerText || b.caption.textContent)) ||
          b.name ||
          "";
      } catch (e) {}
      label = String(label || "").trim() || "page" + (i + 1);
      var idx =
        b.reportParams && b.reportParams.originalPageNumber != null
          ? b.reportParams.originalPageNumber
          : i;
      pages.push({
        kind: "page",
        el: b,
        label: label,
        index: idx,
        activate: function (btn) {
          return function () {
            try {
              if (typeof btn.action === "function") {
                btn.action();
                return true;
              }
            } catch (e) {
              console.warn("[dash-shot] page.action", e);
            }
            return false;
          };
        }(b),
      });
    }
    if (!pages.length) {
      pages.push({
        kind: "page",
        el: null,
        label: "page1",
        index: 0,
        activate: function () {
          return true;
        },
      });
    }
    console.log(
      "[dash-shot] pages",
      pages.map(function (p) {
        return p.index + ":" + p.label;
      })
    );
    return pages;
  }

  /** Верхнеуровневый инвентарь элементов на текущей странице. */
  function inventoryPageElements(root) {
    var counts = {};
    var list = [];
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var t = attrType(el);
      if (!t || !/^Sti\w+Element$/i.test(t)) continue;
      if (!counts[t]) counts[t] = 0;
      counts[t]++;
      var ca =
        el.elementAttributes && el.elementAttributes.contentAttributes;
      var ix = ca && ca.interaction;
      var nItems = (ca && ca.items && ca.items.length) || 0;
      list.push({
        type: t,
        name: attrName(el),
        interaction: ix && ix.onClick ? ix.onClick : null,
        items: nItems,
      });
    }
    return { counts: counts, elements: list };
  }

  function geomLabel(geom) {
    try {
      return String(
        geom.getAttribute("elementargument") ||
          geom.getAttribute("elementvalue") ||
          geom.getAttribute("elementident") ||
          geom.getAttribute("elementseries") ||
          "geom"
      );
    } catch (e) {
      return "geom";
    }
  }

  function collectGeoms(host) {
    var out = [];
    var seen = [];
    function addGeom(g) {
      if (!g || seen.indexOf(g) >= 0) return;
      try {
        if (
          g.getAttribute("elementargument") == null &&
          g.getAttribute("elementvalue") == null &&
          g.getAttribute("elementident") == null &&
          g.getAttribute("elementseries") == null
        )
          return;
      } catch (e) {
        return;
      }
      seen.push(g);
      out.push(g);
    }
    function walk(node) {
      if (!node) return;
      addGeom(node);
      var kids = node.childNodes || [];
      for (var i = 0; i < kids.length; i++) walk(kids[i]);
    }
    try {
      var iframe = host.frame || host.querySelector("iframe");
      var doc = iframe ? iframeDoc(iframe) : null;
      if (doc && doc.body) walk(doc.body);
      else if (host.contentPanel) walk(host.contentPanel);
      else walk(host);
    } catch (e) {
      console.warn("[dash-shot] collectGeoms", e);
    }
    return out;
  }

  /**
   * Цели клика: сначала пункты ListBox/TreeView (часто выглядят как «кнопки»),
   * потом StiButtonElement. У каждой — el (куда рамка/клик) + activate().
   */
  function collectClickTargets(root) {
    var out = [];
    var seen = [];
    function add(t) {
      if (!t || !t.el || seen.indexOf(t.el) >= 0) return;
      if (!isVisibleBox(t.el)) return;
      seen.push(t.el);
      out.push(t);
    }

    var nodes = root.querySelectorAll("*");
    // 1) ListBox / TreeView items
    for (var i = 0; i < nodes.length; i++) {
      var host = nodes[i];
      if (!isListBoxHost(host)) continue;
      var filterName = attrName(host) || "List";
      var panel =
        host.itemsPanel ||
        (host.contentPanel && host.contentPanel.querySelector(".stiJsViewerScrollContainer")) ||
        host.contentPanel;
      var pools = [];
      if (panel) pools.push(panel);
      if (host.contentPanel && host.contentPanel !== panel) pools.push(host.contentPanel);
      for (var p = 0; p < pools.length; p++) {
        var kids = pools[p].childNodes || [];
        for (var k = 0; k < kids.length; k++) {
          var item = kids[k];
          if (!item || item.nodeType !== 1) continue;
          if (item.getAttribute && item.getAttribute("data-dash-shot-hl")) continue;
          var hasAction = typeof item.action === "function";
          var hasVal = item.value != null || item.label != null;
          if (!hasAction && !hasVal) continue;
          (function (it, hst, fname) {
            add({
              kind: "listbox",
              host: hst,
              item: it,
              el: it.button || it,
              label: fname + ": " + listItemLabel(it),
              activate: function () {
                return activateListItem(it, hst);
              },
            });
          })(item, host, filterName);
        }
      }
      // horizontal table cells
      try {
        if (host.contentPanel && host.contentPanel.horTable) {
          var cells = host.contentPanel.horTable.querySelectorAll("td");
          for (var c = 0; c < cells.length; c++) {
            var cell = cells[c];
            var child = cell.firstElementChild || cell;
            if (typeof child.action !== "function" && child.value == null && child.label == null)
              continue;
            (function (it, hst, fname) {
              add({
                kind: "listbox",
                host: hst,
                item: it,
                el: it.button || it,
                label: fname + ": " + listItemLabel(it),
                activate: function () {
                  return activateListItem(it, hst);
                },
              });
            })(child, host, filterName);
          }
        }
      } catch (e) {}
    }

    // 2) ComboBox / TreeViewBox — значения из items / меню
    for (var cbi = 0; cbi < nodes.length; cbi++) {
      var combo = nodes[cbi];
      if (!isComboHost(combo)) continue;
      var cname = attrName(combo) || "Combo";
      var ca =
        combo.elementAttributes && combo.elementAttributes.contentAttributes;
      var items = (ca && ca.items) || [];
      var menuItems = [];
      try {
        if (combo.menu && combo.menu.innerContent) {
          var kids = combo.menu.innerContent.querySelectorAll
            ? combo.menu.innerContent.querySelectorAll("*")
            : combo.menu.innerContent.childNodes;
          for (var mi = 0; mi < kids.length; mi++) {
            var m = kids[mi];
            if (m && (m.value != null || m.label != null || typeof m.action === "function"))
              menuItems.push(m);
          }
        }
      } catch (e) {}
      var useItems = menuItems.length ? menuItems : items;
      if (!useItems.length && ca && ca.showAllValue) {
        useItems = [{ value: null, label: "All", isAllValue: true }];
      }
      for (var ui = 0; ui < useItems.length; ui++) {
        (function (hst, it, fname, idx) {
          var lab = listItemLabel(it);
          add({
            kind: "combo",
            host: hst,
            item: it,
            el: hst,
            label: fname + ": " + lab,
            activate: function () {
              return activateComboValue(hst, it);
            },
          });
        })(combo, useItems[ui], cname, ui);
      }
    }

    // 3) StiButtonElement
    for (var b = 0; b < nodes.length; b++) {
      var btn = nodes[b];
      if (!isButtonHost(btn)) continue;
      (function (h) {
        var face = buttonFace(h);
        add({
          kind: "button",
          host: h,
          el: face || h,
          label: buttonLabel(h),
          activate: function () {
            return activateDashButton(h);
          },
        });
      })(btn);
    }

    // 4) Image / Text / Indicator с interaction.onClick
    for (var ii = 0; ii < nodes.length; ii++) {
      var ihost = nodes[ii];
      if (!isInteractionHost(ihost)) continue;
      (function (h) {
        var ca =
          h.elementAttributes && h.elementAttributes.contentAttributes;
        var onClick =
          ca && ca.interaction && ca.interaction.onClick
            ? ca.interaction.onClick
            : "?";
        add({
          kind: "interaction",
          host: h,
          el: h,
          label: attrName(h) + " [" + onClick + "]",
          activate: function () {
            return activateInteractionHost(h);
          },
        });
      })(ihost);
    }

    // 5) RegionMap / Chart — кликабельные geom в iframe
    for (var gi = 0; gi < nodes.length; gi++) {
      var ghost = nodes[gi];
      if (!isGeomHost(ghost)) continue;
      var gname = attrName(ghost) || attrType(ghost) || "Map";
      var geoms = collectGeoms(ghost);
      var cap = Math.min(geoms.length, GEOM_CLICK_CAP);
      if (geoms.length > GEOM_CLICK_CAP) {
        console.warn(
          "[dash-shot] geoms capped",
          gname,
          geoms.length,
          "→",
          GEOM_CLICK_CAP
        );
      }
      for (var gj = 0; gj < cap; gj++) {
        (function (hst, geom, fname, idx) {
          var gl = geomLabel(geom);
          add({
            kind: "geom",
            host: hst,
            el: hst, // рамка на карте/чарте целиком (geom в iframe)
            label: fname + ": " + gl,
            activate: function () {
              return activateGeom(hst, geom);
            },
          });
        })(ghost, geoms[gj], gname, gj);
      }
    }

    // сверху вниз, слева направо
    out.sort(function (a, b) {
      var ra = a.el.getBoundingClientRect();
      var rb = b.el.getBoundingClientRect();
      if (Math.abs(ra.top - rb.top) > 12) return ra.top - rb.top;
      return ra.left - rb.left;
    });

    console.log(
      "[dash-shot] click targets",
      out.map(function (t) {
        var r = t.el.getBoundingClientRect();
        return {
          kind: t.kind,
          label: t.label,
          box:
            Math.round(r.left) +
            "," +
            Math.round(r.top) +
            " " +
            Math.round(r.width) +
            "x" +
            Math.round(r.height),
        };
      })
    );
    return out;
  }

  /** @deprecated use collectClickTargets */
  function collectDashButtons(root) {
    return collectClickTargets(root).map(function (t) {
      return t.host || t.el;
    });
  }

  /**
   * Яркая рамка + плашка «КЛИК → …» по реальному el (пункт списка / лицо кнопки).
   * Fixed — видно вживую; inset на HTML-родителе — попадает в скрин.
   */
  function highlightClickTarget(el, label, stepText) {
    clearClickHighlight();
    if (!el) return function () {};
    try {
      el.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    } catch (e) {
      try {
        el.scrollIntoView(true);
      } catch (e2) {}
    }
    var r = el.getBoundingClientRect();
    var pad = 6;
    var caption =
      (stepText ? stepText + " " : "") + "КЛИК → " + (label || "?");
    if (!document.getElementById("dashShotPulseStyle")) {
      var st = document.createElement("style");
      st.id = "dashShotPulseStyle";
      st.textContent =
        "@keyframes dashShotPulse{from{opacity:1}to{opacity:.55}}";
      document.head.appendChild(st);
    }

    var ring = document.createElement("div");
    ring.setAttribute("data-dash-shot-hl", "1");
    ring.style.cssText = [
      "position:fixed",
      "left:" + Math.round(r.left - pad) + "px",
      "top:" + Math.round(r.top - pad) + "px",
      "width:" + Math.round(r.width + pad * 2) + "px",
      "height:" + Math.round(r.height + pad * 2) + "px",
      "box-sizing:border-box",
      "border:4px solid #ef4444",
      "border-radius:8px",
      "background:rgba(239,68,68,0.18)",
      "box-shadow:0 0 0 3px #fff,0 0 0 6px #ef4444,0 8px 24px rgba(0,0,0,.35)",
      "z-index:2147483645",
      "pointer-events:none",
      "animation:dashShotPulse 0.7s ease-in-out infinite alternate",
    ].join(";");
    var tag = document.createElement("div");
    tag.setAttribute("data-dash-shot-hl", "1");
    tag.textContent = caption;
    var tagTop = Math.max(8, Math.round(r.top - pad - 36));
    if (r.top < 48) tagTop = Math.round(r.bottom + pad + 8);
    tag.style.cssText = [
      "position:fixed",
      "left:" + Math.round(r.left - pad) + "px",
      "top:" + tagTop + "px",
      "max-width:min(480px,90vw)",
      "padding:8px 12px",
      "background:#ef4444",
      "color:#fff",
      "font:bold 14px/1.2 ui-sans-serif,system-ui,sans-serif",
      "border-radius:6px",
      "box-shadow:0 4px 16px rgba(0,0,0,.35)",
      "z-index:2147483645",
      "pointer-events:none",
      "white-space:nowrap",
      "overflow:hidden",
      "text-overflow:ellipsis",
    ].join(";");
    document.body.appendChild(ring);
    document.body.appendChild(tag);

    // inset только на HTMLElement (не SVG) — иначе html2canvas/DOM ломается
    var mount = el;
    if (!mount.tagName || mount.tagName.toLowerCase() === "svg" || mount.namespaceURI === "http://www.w3.org/2000/svg") {
      mount = el.parentElement || el;
    }
    if (mount && mount.tagName && mount.tagName.toLowerCase() !== "svg") {
      if (getComputedStyle(mount).position === "static") {
        mount.setAttribute("data-dash-shot-pos", mount.style.position || "");
        mount.style.position = "relative";
      }
      var ir = el.getBoundingClientRect();
      var mr = mount.getBoundingClientRect();
      var inset = document.createElement("div");
      inset.setAttribute("data-dash-shot-hl", "1");
      inset.style.cssText = [
        "position:absolute",
        "left:" + Math.round(ir.left - mr.left - 4) + "px",
        "top:" + Math.round(ir.top - mr.top - 4) + "px",
        "width:" + Math.round(ir.width + 8) + "px",
        "height:" + Math.round(ir.height + 8) + "px",
        "box-sizing:border-box",
        "border:4px solid #ef4444",
        "border-radius:6px",
        "background:rgba(239,68,68,0.22)",
        "box-shadow:inset 0 0 0 2px #fff",
        "z-index:9999",
        "pointer-events:none",
      ].join(";");
      var insetTag = document.createElement("div");
      insetTag.setAttribute("data-dash-shot-hl", "1");
      insetTag.textContent = caption;
      insetTag.style.cssText = [
        "position:absolute",
        "left:" + Math.round(ir.left - mr.left) + "px",
        "top:" + Math.round(ir.top - mr.top - 28) + "px",
        "padding:4px 8px",
        "background:#ef4444",
        "color:#fff",
        "font:bold 12px/1.2 ui-sans-serif,system-ui,sans-serif",
        "border-radius:4px",
        "white-space:nowrap",
        "z-index:10000",
        "pointer-events:none",
        "max-width:280px",
        "overflow:hidden",
        "text-overflow:ellipsis",
      ].join(";");
      mount.appendChild(inset);
      mount.appendChild(insetTag);
    }

    return clearClickHighlight;
  }

  function clearClickHighlight() {
    try {
      var nodes = document.querySelectorAll("[data-dash-shot-hl]");
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        if (el && el.parentNode) el.parentNode.removeChild(el);
      }
    } catch (e) {}
    try {
      var marked = document.querySelectorAll("[data-dash-shot-pos]");
      for (var j = 0; j < marked.length; j++) {
        var h = marked[j];
        h.style.position = h.getAttribute("data-dash-shot-pos") || "";
        h.removeAttribute("data-dash-shot-pos");
      }
    } catch (e) {}
  }

  /** Пронумеровать все цели сразу (превью перед сканом). */
  function previewAllTargets(targets) {
    clearClickHighlight();
    if (!document.getElementById("dashShotPulseStyle")) {
      var st = document.createElement("style");
      st.id = "dashShotPulseStyle";
      st.textContent =
        "@keyframes dashShotPulse{from{opacity:1}to{opacity:.55}}";
      document.head.appendChild(st);
    }
    for (var i = 0; i < targets.length; i++) {
      var el = targets[i].el;
      if (!el || !el.getBoundingClientRect) continue;
      var r = el.getBoundingClientRect();
      var ring = document.createElement("div");
      ring.setAttribute("data-dash-shot-hl", "1");
      ring.style.cssText = [
        "position:fixed",
        "left:" + Math.round(r.left) + "px",
        "top:" + Math.round(r.top) + "px",
        "width:" + Math.round(r.width) + "px",
        "height:" + Math.round(r.height) + "px",
        "box-sizing:border-box",
        "border:3px solid #ef4444",
        "border-radius:6px",
        "background:rgba(239,68,68,0.12)",
        "z-index:2147483644",
        "pointer-events:none",
      ].join(";");
      var badge = document.createElement("div");
      badge.setAttribute("data-dash-shot-hl", "1");
      badge.textContent = String(i + 1);
      badge.title = targets[i].label || "";
      badge.style.cssText = [
        "position:fixed",
        "left:" + Math.round(r.left) + "px",
        "top:" + Math.max(0, Math.round(r.top) - 22) + "px",
        "min-width:20px",
        "padding:2px 6px",
        "background:#ef4444",
        "color:#fff",
        "font:bold 12px/1.2 ui-sans-serif,system-ui,sans-serif",
        "border-radius:4px",
        "z-index:2147483645",
        "pointer-events:none",
        "text-align:center",
      ].join(";");
      document.body.appendChild(ring);
      document.body.appendChild(badge);
    }
  }

  function activateClickTarget(target) {
    if (!target) return false;
    console.log("[dash-shot] activate", target.kind, target.label);
    if (typeof target.activate === "function") {
      try {
        return !!target.activate();
      } catch (e) {
        console.warn("[dash-shot] activate threw", e);
      }
    }
    if (target.kind === "button" && target.host)
      return activateDashButton(target.host);
    if (target.kind === "listbox" && target.item)
      return activateListItem(target.item, target.host);
    if (target.kind === "combo" && target.item)
      return activateComboValue(target.host, target.item);
    if (target.kind === "interaction" && target.host)
      return activateInteractionHost(target.host);
    return false;
  }

  /** Клик по каждой цели (ListBox-пункты + StiButton) → скрин+PDF. */
  /**
   * Скан по страницам: инвентарь → baseline → клики
   * (ListBox, ComboBox, Button, Image/Text interaction, RegionMap/Chart geoms).
   */
  async function scanButtons() {
    var btn = document.getElementById("dashShotBtn");
    var scanBtn = document.getElementById("dashScanBtn");
    if (btn) btn.disabled = true;
    if (scanBtn) scanBtn.disabled = true;

    var logLines = [];
    var inventoryLines = [];
    var scanId = stamp();
    var clearHl = function () {};
    var totalClicks = 0;
    try {
      await loadScript(HTML2CANVAS);
      var pages = collectDashboardPages();
      setStatus("страниц: " + pages.length + " — инвентарь…");
      inventoryLines.push("PAGES (" + pages.length + "):");
      pages.forEach(function (p, i) {
        inventoryLines.push(
          "  " + (i + 1) + ") [" + p.index + "] " + p.label
        );
      });

      for (var pi = 0; pi < pages.length; pi++) {
        var page = pages[pi];
        var pageSlug =
          "p" +
          String(page.index).padStart(2, "0") +
          "_" +
          safeSlug(page.label);
        setStatus(
          "стр. " + (pi + 1) + "/" + pages.length + ": " + page.label
        );
        logLines.push("");
        logLines.push("=== PAGE " + page.label + " (index " + page.index + ") ===");

        if (typeof page.activate === "function") {
          try {
            page.activate();
          } catch (e) {
            console.warn("[dash-shot] page switch", e);
          }
          await waitDashboardIdle(1000, 14000);
          await sleep(500);
        }

        var root = captureRoot();
        if (!root) {
          logLines.push("page root missing");
          continue;
        }

        var inv = inventoryPageElements(root);
        inventoryLines.push("");
        inventoryLines.push("PAGE " + page.label + " elements:");
        Object.keys(inv.counts)
          .sort()
          .forEach(function (t) {
            inventoryLines.push("  " + t + ": " + inv.counts[t]);
          });
        inv.elements.forEach(function (e) {
          inventoryLines.push(
            "  - " +
              e.type +
              " / " +
              (e.name || "?") +
              (e.interaction ? " interaction=" + e.interaction : "") +
              (e.items ? " items=" + e.items : "")
          );
        });
        console.log("[dash-shot] inventory", page.label, inv);

        setStatus(page.label + " — baseline…");
        var base = await takeScreenshot({
          label: pageSlug + "__00_baseline",
          banner: "Страница: " + page.label + " (без клика)",
          quiet: true,
        });
        logLines.push(
          "baseline | png=" +
            (base && base.png) +
            " | pdf=" +
            ((base && base.pdfs) || []).join(",")
        );

        var targets = collectClickTargets(root);
        logLines.push("targets: " + targets.length);
        targets.forEach(function (t, idx) {
          logLines.push(
            "  " + (idx + 1) + ") [" + t.kind + "] " + t.label
          );
        });

        if (!targets.length) {
          setStatus(page.label + " — нет кликабельных целей");
          await sleep(600);
          continue;
        }

        setStatus(
          page.label + ": " + targets.length + " целей — номера…"
        );
        previewAllTargets(targets);
        await sleep(2200);
        clearClickHighlight();

        for (var i = 0; i < targets.length; i++) {
          var target = targets[i];
          var label = target.label;
          var step = i + 1 + "/" + targets.length;
          var slug =
            pageSlug +
            "__" +
            String(i + 1).padStart(2, "0") +
            "_" +
            target.kind +
            "_" +
            safeSlug(label);
          setStatus(page.label + " " + step + " → " + label);
          clearHl = highlightClickTarget(target.el, label, step);
          await sleep(700);

          var clicked = false;
          try {
            clicked = activateClickTarget(target);
          } catch (e) {
            console.warn("[dash-shot] click failed", label, e);
            logLines.push(slug + " | CLICK FAIL " + label);
            clearHl();
            clearHl = function () {};
            continue;
          }
          if (!clicked) {
            logLines.push(slug + " | ACTIVATE FALSE " + label);
            console.warn("[dash-shot] activate false", label);
          }

          setStatus(page.label + " " + step + " ждём перерисовку…");
          clearHl();
          clearHl = function () {};
          var redrawn = await waitDashboardIdle(800, 14000);
          console.log(
            "[dash-shot] idle",
            label,
            redrawn ? "redraw" : "no-busy"
          );
          await sleep(350);

          var el = null;
          try {
            root = captureRoot() || root;
            var again = collectClickTargets(root);
            var match = again.filter(function (t) {
              return t.kind === target.kind && t.label === label;
            })[0];
            if (match) {
              target = match;
              el = match.el;
            }
          } catch (e) {}
          if (el && el.isConnected) {
            clearHl = highlightClickTarget(el, label, step + " нажато");
            await sleep(250);
          }

          var shot = await takeScreenshot({
            label: slug,
            banner:
              "Стр. " + page.label + " | " + step + " Нажато: " + label,
            quiet: true,
          });
          clearHl();
          clearHl = function () {};
          totalClicks++;
          logLines.push(
            slug +
              " | Нажато: " +
              label +
              " | kind=" +
              target.kind +
              " | png=" +
              (shot && shot.png) +
              " | pdf=" +
              ((shot && shot.pdfs) || []).join(",")
          );
        }
      }

      var invName = "inventory_" + scanId + ".txt";
      var invBody =
        "Инвентарь дашборда " +
        scanId +
        "\n\n" +
        inventoryLines.join("\n") +
        "\n";
      var logName = "buttons_scan_" + scanId + ".txt";
      var logBody =
        "Скан кликов " +
        scanId +
        "\n" +
        "По страницам: ListBox, ComboBox, Button, Image/Text interaction, RegionMap/Chart geoms\n" +
        "Кликов: " +
        totalClicks +
        "\n\n" +
        logLines.join("\n") +
        "\n";
      try {
        await postExportFile(
          invName,
          btoa(unescape(encodeURIComponent(invBody))),
          "txt"
        );
        downloadBlob(
          new Blob([invBody], { type: "text/plain;charset=utf-8" }),
          invName
        );
        await postExportFile(
          logName,
          btoa(unescape(encodeURIComponent(logBody))),
          "txt"
        );
        downloadBlob(
          new Blob([logBody], { type: "text/plain;charset=utf-8" }),
          logName
        );
      } catch (e) {
        console.warn("[dash-shot] scan log", e);
      }
      setStatus(
        "скан готов: " +
          pages.length +
          " стр., " +
          totalClicks +
          " кликов | " +
          logName
      );
      console.log("[dash-shot] inventory\n" + invBody);
      console.log("[dash-shot] scan log\n" + logBody);
    } catch (err) {
      console.error(err);
      setStatus(String((err && err.message) || err));
    } finally {
      try {
        clearHl();
      } catch (e) {}
      clearClickHighlight();
      if (btn) btn.disabled = false;
      if (scanBtn) scanBtn.disabled = false;
    }
  }

  function mount() {
    if (document.getElementById("dashShotBar")) return;
    var bar = document.createElement("div");
    bar.id = "dashShotBar";
    bar.style.cssText =
      "position:fixed;z-index:2147483646;top:8px;right:8px;display:flex;gap:8px;align-items:center;" +
      "font:12px/1.2 ui-sans-serif,system-ui,sans-serif;background:rgba(20,20,20,.85);color:#fff;" +
      "padding:6px 10px;border-radius:6px;";
    var btnStyle =
      "cursor:pointer;border:0;border-radius:4px;padding:5px 10px;background:#eee;color:#111;font:inherit;";
    var btn = document.createElement("button");
    btn.id = "dashShotBtn";
    btn.type = "button";
    btn.textContent = "Скрин";
    btn.style.cssText = btnStyle;
    btn.onclick = function () {
      takeScreenshot();
    };
    var scanBtn = document.createElement("button");
    scanBtn.id = "dashScanBtn";
    scanBtn.type = "button";
    scanBtn.textContent = "Скан кликов";
    scanBtn.title =
      "По страницам: ListBox, ComboBox, Button, Image/Text interaction, RegionMap/Chart → скрин+PDF";
    scanBtn.style.cssText = btnStyle;
    scanBtn.onclick = function () {
      scanButtons();
    };
    var status = document.createElement("span");
    status.id = "dashShotStatus";
    status.style.cssText =
      "max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.9;";
    bar.appendChild(btn);
    bar.appendChild(scanBtn);
    bar.appendChild(status);
    document.body.appendChild(bar);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }

  window.__dashTakeScreenshot = takeScreenshot;
  window.__dashScanButtons = scanButtons;
  window.__dashExportTablesPdf = function () {
    return exportTablesToPdf(findReport(), captureRoot());
  };
})();
