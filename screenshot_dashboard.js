/* Скрин дашборда + Table/Pivot → PDF.
 *
 * UI-кнопка выключена по умолчанию (SHOT_SHOW_BUTTON / ?shot_ui=1).
 * GET /shot не перезагружает дашборд: вкладка /open поллит /api/shot-poll
 * и по сигналу снимает PNG+PDF (download + exports/).
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
    // после drill-down viewer.report может смениться — не кэшируем устаревший __dashReport
    var live =
      (window.__dashViewer && window.__dashViewer.report) ||
      (window.viewer && window.viewer.report) ||
      null;
    if (live) {
      window.__dashReport = live;
      return live;
    }
    if (window.__dashReport) return window.__dashReport;
    return null;
  }

  function currentDashboardPage(report) {
    if (!report) return null;
    try {
      if (report.currentPage) return report.currentPage;
    } catch (e) {}
    try {
      var idx =
        report.currentPageIndex != null
          ? report.currentPageIndex
          : report.pageNumber != null
            ? report.pageNumber
            : null;
      if (
        idx != null &&
        report.pages &&
        idx >= 0 &&
        idx < report.pages.count
      ) {
        return report.pages.getByIndex(idx);
      }
    } catch (e2) {}
    // drill-down часто показывает последнюю добавленную страницу
    try {
      if (report.pages && report.pages.count > 0) {
        return report.pages.getByIndex(report.pages.count - 1);
      }
    } catch (e3) {}
    return null;
  }

  function isDomReallyVisible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    try {
      var r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) return false;
      var st = window.getComputedStyle(el);
      if (
        st.display === "none" ||
        st.visibility === "hidden" ||
        Number(st.opacity) === 0
      ) {
        return false;
      }
      // скрытые страницы после drill-down часто уезжают за экран / в zero-size panel
      var panel = el.closest
        ? el.closest(
            ".stiJsViewerDashboardBackPanel, .stiJsViewerPageView, .stiJsViewerReportPanel"
          )
        : null;
      if (panel) {
        var pr = panel.getBoundingClientRect();
        if (pr.width < 4 || pr.height < 4) return false;
      }
      return true;
    } catch (e) {
      return false;
    }
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

  /** PDF settings как у Save на элементе: A2 Landscape. */
  function dashboardPdfSettings() {
    var Exp = Stimulsoft.Dashboard && Stimulsoft.Dashboard.Export;
    if (!Exp) return null;
    var Settings = Exp.Settings || Exp;
    var Ctor =
      (Settings && Settings.StiPdfDashboardExportSettings) ||
      Exp.StiPdfDashboardExportSettings;
    if (!Ctor) return null;
    try {
      var s = new Ctor();
      var PaperKind =
        Stimulsoft.System &&
        Stimulsoft.System.Drawing &&
        Stimulsoft.System.Drawing.Printing &&
        Stimulsoft.System.Drawing.Printing.PaperKind;
      if (PaperKind && PaperKind.A2 != null) s.paperSize = PaperKind.A2;
      var Orient =
        Stimulsoft.Report &&
        Stimulsoft.Report.Components &&
        Stimulsoft.Report.Components.StiPageOrientation;
      if (Orient && Orient.Landscape != null) s.orientation = Orient.Landscape;
      if ("imageQuality" in s) s.imageQuality = 200;
      if ("openAfterExport" in s) s.openAfterExport = false;
      return s;
    } catch (e) {
      console.warn("[dash-shot] pdf settings", e);
      return null;
    }
  }

  function exportElementAsync(element, settingsOrFormat) {
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
          settingsOrFormat
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

  /** Table/Pivot в DOM, которые реально видны (после drill-down родительские скрыты). */
  function visibleTableInfos(root) {
    var infos = [];
    var nodes = (root || document).querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var t = attrType(el);
      if (!/TableElement|PivotTable/i.test(t)) continue;
      if (!isDomReallyVisible(el)) continue;
      var n = attrName(el);
      if (!n) continue;
      infos.push({ name: n, el: el });
    }
    return infos;
  }

  function collectTablesOnPage(page) {
    var list = [];
    if (!page) return list;
    walkComponents(page, function (c) {
      if (isExportableTable(c)) list.push(c);
    });
    return list;
  }

  /**
   * Таблицы для экспорта: только текущая страница + реально видимые в DOM.
   * Иначе после drill-down уходит PDF родительского уровня.
   */
  function tablesForExport(report, root) {
    var visible = visibleTableInfos(root || document);
    var page = currentDashboardPage(report);
    var comps = collectTablesOnPage(page);
    if (!comps.length) {
      // fallback: все, но потом сузим по visible
      comps = collectAll(report).filter(isExportableTable);
    }
    var visibleNames = {};
    for (var i = 0; i < visible.length; i++) visibleNames[visible[i].name] = true;

    console.log(
      "[dash-shot] tables page",
      page && (page.name || page.pageName || "?"),
      "comps",
      comps.map(function (c) {
        return c.name || typeName(c);
      }),
      "visibleDom",
      Object.keys(visibleNames),
      "drillDownFilters",
      comps.map(function (c) {
        try {
          return {
            name: c.name,
            level: c.drillDownCurrentLevel,
            filters: c.drillDownFilters && c.drillDownFilters.length,
          };
        } catch (e) {
          return { name: c.name };
        }
      })
    );

    if (Object.keys(visibleNames).length) {
      var matched = comps.filter(function (c) {
        return c.name && visibleNames[c.name];
      });
      if (matched.length) return matched;
      // имена в DOM не совпали с page comps — возьмём visible из всего report
      var all = collectAll(report).filter(isExportableTable);
      matched = all.filter(function (c) {
        return c.name && visibleNames[c.name];
      });
      if (matched.length) return matched;
    }
    // нет DOM-метки: только comps текущей страницы (не все страницы!)
    return comps;
  }

  function queryFlag(name) {
    try {
      var q = new URLSearchParams(window.location.search || "");
      var v = (q.get(name) || "").trim().toLowerCase();
      return v === "1" || v === "true" || v === "yes" || v === "on";
    } catch (e) {
      return false;
    }
  }

  function autoShotEnabled() {
    return !!(window.__dashAutoShot || queryFlag("autoscreenshot"));
  }

  function shotButtonEnabled() {
    return !!(window.__dashShowShotButton || queryFlag("shot_ui"));
  }

  /**
   * Save на Table/Pivot → PDF A2 (как hover Save), с учётом drill-down.
   * Скачивает PDF + кладёт в exports/.
   * @returns {Promise<Array<{name:string,path?:string}>>}
   */
  async function exportTablesToPdf(report, root) {
    var files = [];
    if (!report) return files;
    var settings = dashboardPdfSettings();
    if (!settings) {
      // fallback: Report.StiExportFormat.Pdf → helper соберёт settings (A4)
      settings =
        Stimulsoft.Report && Stimulsoft.Report.StiExportFormat
          ? Stimulsoft.Report.StiExportFormat.Pdf
          : null;
    }
    if (settings == null) {
      console.warn("[dash-shot] PDF settings нет");
      return files;
    }
    try {
      console.log(
        "[dash-shot] pdf paper",
        settings.paperSize,
        "orient",
        settings.orientation
      );
    } catch (e) {}
    var comps = tablesForExport(report, root || document);
    if (!comps.length) {
      console.warn("[dash-shot] Table/Pivot не найден на текущем уровне");
      return files;
    }
    for (var i = 0; i < comps.length; i++) {
      var el = comps[i];
      var base =
        String(el.name || "Table" + (i + 1)).replace(/\s+/g, "_") +
        "_" +
        stamp();
      var pdfName = base + ".pdf";
      setStatus("PDF A2 " + (el.name || base) + "…");
      try {
        var data = await exportElementAsync(el, settings);
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
          var body = await postExportFile(pdfName, stiBytesToBase64(data), "pdf");
          files.push({
            name: (body && body.name) || pdfName,
            path: body && body.path,
            kind: "pdf",
          });
        } catch (e) {
          console.warn("[dash-shot] pdf upload", e);
        }
        console.log("[dash-shot] table pdf", pdfName);
      } catch (err) {
        console.warn("[dash-shot] table pdf failed", el.name, err);
      }
    }
    return files;
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

  async function takeScreenshot() {
    var result = { ok: false, png: null, pdfs: [], files: [], error: null };
    var root = captureRoot();
    if (!root) {
      result.error = "панель не найдена";
      setStatus(result.error);
      return result;
    }
    var btn = document.getElementById("dashShotBtn");
    if (btn) btn.disabled = true;
    var restoreMaps = function () {};
    var restoreFilters = function () {};
    setStatus("снимок…");
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

      var blob = await new Promise(function (resolve, reject) {
        canvas.toBlob(function (b) {
          if (b) resolve(b);
          else reject(new Error("toBlob failed"));
        }, "image/png");
      });
      var name = "dashboard_" + stamp() + ".png";
      downloadBlob(blob, name);
      try {
        var b64 = await blobToBase64(blob);
        var body = await postExportFile(name, b64, "png");
        result.png = {
          name: (body && body.name) || name,
          path: body && body.path,
          kind: "png",
        };
        result.files.push(result.png);
        setStatus("saved " + result.png.name);
      } catch (err) {
        setStatus("download ok");
        console.warn(err);
        if (!result.png) result.error = String((err && err.message) || err);
      }

      // дополнительно: Table/Pivot → PDF (все строки, как Save→но PDF)
      setStatus("таблица → PDF…");
      try {
        result.pdfs = await exportTablesToPdf(report, root);
        for (var pi = 0; pi < result.pdfs.length; pi++) {
          result.files.push(result.pdfs[pi]);
        }
      } catch (err) {
        console.warn("[dash-shot] tables pdf", err);
      }
      result.ok = !!(result.png || result.pdfs.length);
      if (result.ok) result.error = null;
      setStatus(
        (result.png ? "saved " + result.png.name : "png нет") +
          (result.pdfs.length
            ? " | +" + result.pdfs.length + " pdf"
            : " | pdf нет")
      );
      return result;
    } catch (err) {
      console.error(err);
      result.error = String((err && err.message) || err);
      setStatus(result.error);
      return result;
    } finally {
      try {
        restoreFilters();
      } catch (e) {}
      try {
        restoreMaps();
      } catch (e) {}
      if (btn) btn.disabled = false;
    }
  }

  function dashboardLooksReady() {
    var report = findReport();
    var root = captureRoot();
    if (!report || !root) return false;
    try {
      var r = root.getBoundingClientRect();
      if (!r || r.width < 80 || r.height < 80) return false;
    } catch (e) {
      return false;
    }
    return true;
  }

  function waitDashboardReady(maxMs) {
    maxMs = maxMs || 60000;
    return new Promise(function (resolve) {
      var started = Date.now();
      var t = setInterval(function () {
        if (dashboardLooksReady() || Date.now() - started > maxMs) {
          clearInterval(t);
          // карты/Variables дорисовываются чуть позже
          setTimeout(resolve, dashboardLooksReady() ? 1800 : 0);
        }
      }, 400);
    });
  }

  async function reportShotDone(result) {
    try {
      await fetch("/api/shot-done", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(result || { ok: false, error: "empty" }),
      });
    } catch (e) {
      console.warn("[dash-shot] shot-done", e);
    }
  }

  async function runAutoShot(opts) {
    opts = opts || {};
    if (window.__dashShotBusy) return;
    window.__dashShotBusy = true;
    window.__dashShotDone = null;
    try {
      // по сигналу /shot дашборд уже на экране — не ждать 70с
      if (opts.skipWait) {
        await new Promise(function (r) {
          setTimeout(r, 50);
        });
      } else {
        await waitDashboardReady(70000);
      }
      var result = await takeScreenshot();
      window.__dashShotDone = result || {
        ok: false,
        error: "empty result",
        files: [],
        png: null,
        pdfs: [],
      };
    } catch (err) {
      window.__dashShotDone = {
        ok: false,
        error: String((err && err.message) || err),
        files: [],
        png: null,
        pdfs: [],
      };
    } finally {
      window.__dashShotBusy = false;
    }
    console.log("[dash-shot] done", window.__dashShotDone);
    await reportShotDone(window.__dashShotDone);
  }

  function startShotPoller() {
    if (window.__dashShotPoller) return;
    window.__dashShotPoller = true;
    function loop() {
      if (window.__dashShotBusy) {
        setTimeout(loop, 400);
        return;
      }
      fetch("/api/shot-poll", { cache: "no-store" })
        .then(function (r) {
          return r.json();
        })
        .then(function (j) {
          if (j && j.shoot) {
            console.log("[dash-shot] signal → shoot");
            return runAutoShot({ skipWait: true }).then(function () {
              loop();
            });
          }
          loop();
        })
        .catch(function () {
          setTimeout(loop, 2000);
        });
    }
    loop();
  }

  function mount() {
    if (shotButtonEnabled()) {
      if (document.getElementById("dashShotBar")) return;
      var bar = document.createElement("div");
      bar.id = "dashShotBar";
      bar.style.cssText =
        "position:fixed;z-index:2147483646;top:8px;right:8px;display:flex;gap:8px;align-items:center;" +
        "font:12px/1.2 ui-sans-serif,system-ui,sans-serif;background:rgba(20,20,20,.85);color:#fff;" +
        "padding:6px 10px;border-radius:6px;";
      var btn = document.createElement("button");
      btn.id = "dashShotBtn";
      btn.type = "button";
      btn.textContent = "Скрин";
      btn.style.cssText =
        "cursor:pointer;border:0;border-radius:4px;padding:5px 10px;background:#eee;color:#111;font:inherit;";
      btn.onclick = function () {
        takeScreenshot();
      };
      var status = document.createElement("span");
      status.id = "dashShotStatus";
      status.style.cssText =
        "max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.9;";
      bar.appendChild(btn);
      bar.appendChild(status);
      document.body.appendChild(bar);
    }
    // всегда слушаем GET /shot (без reload дашборда)
    startShotPoller();
    if (autoShotEnabled()) {
      setTimeout(runAutoShot, 0);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }

  window.__dashTakeScreenshot = takeScreenshot;
  window.__dashExportTablesPdf = function () {
    return exportTablesToPdf(findReport(), captureRoot());
  };
})();
