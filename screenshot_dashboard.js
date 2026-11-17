/* Кнопка «Скрин».
 *
 * Stimulsoft кладёт RegionMap HTML в iframe (InsertHTMLContentToDashboardElement + document.write).
 * html2canvas iframe не видит → карта пустая.
 * Фикс: SVG из iframe.contentDocument → offscreen canvas, после html2canvas
 * рисуем поверх PNG ровно в бокс iframe (DOM не трогаем — элемент над картой остаётся).
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

  /** SVG из iframe: стили через contentWindow.getComputedStyle */
  function cloneSvgInlined(svg, win) {
    var getStyle = (win && win.getComputedStyle.bind(win)) || window.getComputedStyle.bind(window);
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
    ];
    for (var i = 0; i < srcNodes.length && i < dstNodes.length; i++) {
      try {
        var cs = getStyle(srcNodes[i]);
        for (var p = 0; p < props.length; p++) {
          var val = cs.getPropertyValue(props[p]);
          if (val && val !== "none") dstNodes[i].setAttribute(props[p], val);
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
    clone.removeAttribute("style");
    return { clone: clone, w: w, h: h };
  }

  function svgToCanvas(svg, win) {
    var prepared = cloneSvgInlined(svg, win);
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
      bytes = new Uint8Array(data.buffer, data.byteOffset || 0, data.byteLength || data.length);
    else if (typeof data === "string") return btoa(data);
    else if (Array.isArray(data)) bytes = new Uint8Array(data);
    else throw new Error("unknown export payload");
    var chunk = 0x8000;
    var parts = [];
    for (var i = 0; i < bytes.length; i += chunk) {
      parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + chunk)));
    }
    return btoa(parts.join(""));
  }

  /** Save на Pivot в viewer → Image/Excel/Data; PDF — через export API (все строки). */
  async function exportPivotsToPdf() {
    var report = findReport();
    if (!report) {
      setStatus("report не найден");
      return;
    }
    var Pdf =
      Stimulsoft.Report && Stimulsoft.Report.StiExportFormat
        ? Stimulsoft.Report.StiExportFormat.Pdf
        : null;
    if (Pdf == null) {
      setStatus("StiExportFormat.Pdf нет");
      return;
    }
    var pivots = collectAll(report).filter(isPivotTableElement);
    console.log(
      "[dash-shot] pivots",
      pivots.map(function (p) {
        return p.name || typeName(p);
      })
    );
    if (!pivots.length) {
      setStatus("PivotTable не найден");
      return;
    }
    var btn = document.getElementById("dashPivotPdfBtn");
    if (btn) btn.disabled = true;
    var saved = 0;
    try {
      for (var i = 0; i < pivots.length; i++) {
        var el = pivots[i];
        var base =
          String(el.name || "PivotTable" + (i + 1)).replace(/\s+/g, "_") +
          "_" +
          stamp();
        setStatus("PDF " + (el.name || base) + "…");
        var data = await exportElementAsync(el, Pdf);
        var pdfName = base + ".pdf";
        try {
          Stimulsoft.System.StiObject.saveAs(
            data,
            pdfName,
            "application/pdf"
          );
        } catch (e) {
          console.warn("[dash-shot] saveAs pdf", e);
        }
        try {
          var b64 = stiBytesToBase64(data);
          await postExportFile(pdfName, b64, "pdf");
          saved++;
        } catch (e) {
          console.warn("[dash-shot] pdf upload", e);
          saved++;
        }
      }
      setStatus("pivot pdf: " + saved + " → exports/");
    } catch (err) {
      console.error(err);
      setStatus(String((err && err.message) || err));
    } finally {
      if (btn) btn.disabled = false;
    }
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

  /** Бокс для карты: contentPanel / iframe, ниже titlePanel — title на скрине остаётся. */
  function mapContentBox(host, iframe, root) {
    var rootRect = root.getBoundingClientRect();
    var target =
      (host.contentPanel && host.contentPanel.getBoundingClientRect().height > 2
        ? host.contentPanel
        : null) || iframe;
    var ir = target.getBoundingClientRect();
    var box = {
      x: ir.left - rootRect.left + (root.scrollLeft || 0),
      y: ir.top - rootRect.top + (root.scrollTop || 0),
      w: ir.width,
      h: ir.height,
    };
    var titleEl = host.titlePanel;
    if (titleEl && titleEl.offsetParent !== null) {
      var tr = titleEl.getBoundingClientRect();
      var titleBottom = tr.bottom - rootRect.top + (root.scrollTop || 0);
      if (box.y < titleBottom - 1) {
        var cut = titleBottom - box.y;
        box.y = titleBottom;
        box.h = Math.max(1, box.h - cut);
      }
    }
    return box;
  }

  /**
   * Снимаем карту offscreen (DOM не меняем).
   * paintMapOverlays кладёт её только в content-бокс — titlePanel не затираем.
   */
  async function captureMapOverlays(root, report) {
    var overlays = [];
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

      var box = mapContentBox(host, iframe, root);
      if (box.w < 2 || box.h < 2) continue;

      var doc = iframeDoc(iframe);
      var win = iframeWin(iframe);
      var svg = doc && doc.querySelector("svg");

      console.log("[dash-shot] host", name, {
        hasDoc: !!doc,
        svg: !!svg,
        type: attrType(host),
        box: box,
        titleH: host.titlePanel ? host.titlePanel.offsetHeight : 0,
      });

      if (svg) {
        try {
          var baked = await svgToCanvas(svg, win);
          overlays.push({
            name: name,
            canvas: baked.canvas,
            x: box.x,
            y: box.y,
            w: box.w,
            h: box.h,
          });
          console.log("[dash-shot] overlay SVG ready", name, box.w, "x", box.h);
          continue;
        } catch (e) {
          console.warn("[dash-shot] iframe SVG bake failed", name, e);
        }
      }

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
          var data = await exportOnlineMapPng(mapEl, box.w, box.h);
          var url = bytesToPngDataUrl(data);
          if (!url || url.length < 200) throw new Error("empty sti png");
          var img = await loadImage(url);
          var probe = document.createElement("canvas");
          probe.width = Math.max(1, Math.round(box.w * SCALE));
          probe.height = Math.max(1, Math.round(box.h * SCALE));
          probe.getContext("2d").drawImage(img, 0, 0, probe.width, probe.height);
          if (!canvasHasInk(probe)) throw new Error("sti blank");
          overlays.push({
            name: name,
            canvas: probe,
            x: box.x,
            y: box.y,
            w: box.w,
            h: box.h,
          });
          console.log("[dash-shot] overlay OnlineMap ready", name);
        } catch (e) {
          console.warn("[dash-shot] OnlineMap bake skipped", name, e);
        }
      }
    }

    return overlays;
  }

  function paintMapOverlays(pageCanvas, overlays) {
    if (!overlays || !overlays.length) return 0;
    var ctx = pageCanvas.getContext("2d");
    for (var i = 0; i < overlays.length; i++) {
      var o = overlays[i];
      ctx.drawImage(
        o.canvas,
        Math.round(o.x * SCALE),
        Math.round(o.y * SCALE),
        Math.round(o.w * SCALE),
        Math.round(o.h * SCALE)
      );
      console.log("[dash-shot] painted overlay", o.name);
    }
    return overlays.length;
  }

  function isTableHost(el) {
    var t = attrType(el);
    return (
      t === "StiTableElement" ||
      t === "StiPivotTableElement" ||
      /TableElement/i.test(t)
    );
  }

  function collectTableHosts(root) {
    var out = [];
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      if (isTableHost(nodes[i])) out.push(nodes[i]);
    }
    console.log(
      "[dash-shot] tables",
      out.length,
      out.map(function (el) {
        return { name: attrName(el), type: attrType(el) };
      })
    );
    return out;
  }

  function pushStyleRestore(restorers, el, keys) {
    var saved = { el: el, styles: {} };
    for (var i = 0; i < keys.length; i++) {
      saved.styles[keys[i]] = el.style[keys[i]];
    }
    restorers.push(saved);
  }

  /**
   * Table/Pivot в viewer с overflow+фиксированной высотой → на скрине обрезано.
   * Раскрываем scroll-контейнеры на scrollHeight перед html2canvas.
   */
  function expandTables(root) {
    var restorers = [];
    var tables = collectTableHosts(root);
    setStatus("таблицы " + tables.length);

    tables.forEach(function (host) {
      var nodes = [host];
      if (host.contentPanel) nodes.push(host.contentPanel);
      if (host.dataGrid) nodes.push(host.dataGrid);
      var all = host.querySelectorAll("*");
      for (var i = 0; i < all.length; i++) nodes.push(all[i]);

      // 1) все внутренние scroll/clip → visible + высота по контенту
      nodes.forEach(function (el) {
        if (!el || !el.style) return;
        var cs = window.getComputedStyle(el);
        var oy = cs.overflowY;
        var ox = cs.overflowX;
        var ov = cs.overflow;
        var clips =
          oy === "auto" ||
          oy === "scroll" ||
          oy === "hidden" ||
          ox === "auto" ||
          ox === "scroll" ||
          ov === "auto" ||
          ov === "scroll" ||
          ov === "hidden";
        if (!clips) return;
        var sh = el.scrollHeight;
        var ch = el.clientHeight;
        if (!(sh > ch + 2) && !(el.scrollWidth > el.clientWidth + 2)) return;

        pushStyleRestore(restorers, el, [
          "height",
          "maxHeight",
          "overflow",
          "overflowX",
          "overflowY",
        ]);
        el.style.overflow = "visible";
        el.style.overflowX = "visible";
        el.style.overflowY = "visible";
        el.style.maxHeight = "none";
        if (sh > ch + 2) el.style.height = sh + "px";
        console.log(
          "[dash-shot] expand node",
          el.className || el.tagName,
          ch + "→" + sh
        );
      });

      // 2) сам host (absolute dashboard cell) тоже вырастить
      var needH = Math.max(
        host.scrollHeight,
        host.dataGrid ? host.dataGrid.scrollHeight : 0,
        host.contentPanel ? host.contentPanel.scrollHeight : 0
      );
      // title + padding
      if (host.titlePanel) needH += host.titlePanel.offsetHeight || 0;
      var hostH = host.clientHeight;
      if (needH > hostH + 2) {
        pushStyleRestore(restorers, host, ["height", "maxHeight", "overflow"]);
        host.style.overflow = "visible";
        host.style.maxHeight = "none";
        host.style.height = needH + "px";
        console.log(
          "[dash-shot] expand host",
          attrName(host) || attrType(host),
          hostH + "→" + needH
        );
      }
    });

    return function restore() {
      restorers.forEach(function (x) {
        try {
          Object.keys(x.styles).forEach(function (k) {
            x.el.style[k] = x.styles[k];
          });
        } catch (e) {}
      });
    };
  }

  function cellText(cell) {
    if (cell == null) return "";
    if (typeof cell !== "object") return String(cell);
    if (cell.text != null && String(cell.text) !== "") return String(cell.text);
    if (cell.displayValue != null) return String(cell.displayValue);
    if (cell.value != null) return String(cell.value);
    if (cell.sortLabel != null) return String(cell.sortLabel);
    if (cell.label != null) return String(cell.label);
    return "";
  }

  function headerText(cell, idx) {
    if (cell == null) return "c" + idx;
    if (typeof cell !== "object") return String(cell);
    return (
      cell.sortLabel ||
      cell.label ||
      cell.headerText ||
      cell.text ||
      cell.key ||
      cell.name ||
      "c" + idx
    );
  }

  /** Достаёт все строки таблицы из dataGrid / contentAttributes (не только видимый scroll). */
  function extractTableMatrix(host) {
    var rows = null;
    var src = "";
    try {
      if (host.dataGrid && host.dataGrid.data && host.dataGrid.data.length) {
        rows = host.dataGrid.data;
        src = "dataGrid.data";
      }
    } catch (e) {}
    try {
      var ca =
        host.elementAttributes && host.elementAttributes.contentAttributes;
      if ((!rows || !rows.length) && ca && ca.data && ca.data.length) {
        rows = ca.data;
        src = "contentAttributes.data";
      }
    } catch (e) {}
    if (!rows || !rows.length) {
      console.warn("[dash-shot] extract empty", {
        name: attrName(host),
        type: attrType(host),
        hasGrid: !!(host.dataGrid),
        gridData: host.dataGrid && host.dataGrid.data,
      });
      return null;
    }

    // Stimulsoft: data = [[{key,text,sortLabel,...}, ...], ...] — row0 = header
    var matrix = [];
    var first = rows[0];
    if (Array.isArray(first)) {
      matrix.push(
        first.map(function (cell, idx) {
          return headerText(cell, idx);
        })
      );
      for (var r = 1; r < rows.length; r++) {
        matrix.push(
          rows[r].map(function (cell) {
            return cellText(cell);
          })
        );
      }
      // если в row0 тоже данные (нет отдельных header fields) — уже ок
      if (matrix.length === 1 && rows.length === 1) {
        // only header row present
      }
    } else if (typeof first === "object") {
      var header = Object.keys(first);
      matrix.push(header);
      rows.forEach(function (row) {
        matrix.push(
          header.map(function (k) {
            return cellText(row[k]);
          })
        );
      });
    } else {
      return null;
    }
    console.log("[dash-shot] extract", src, "rows", matrix.length, "cols", matrix[0].length);
    return matrix;
  }

  function toCsv(matrix) {
    return matrix
      .map(function (row) {
        return row
          .map(function (cell) {
            var s = String(cell == null ? "" : cell);
            if (/[",\n\r]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
            return s;
          })
          .join(",");
      })
      .join("\n");
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

  async function saveTableData(root) {
    var tables = collectTableHosts(root);
    var saved = 0;
    var paths = [];
    for (var i = 0; i < tables.length; i++) {
      var host = tables[i];
      var matrix = extractTableMatrix(host);
      if (!matrix || matrix.length < 1) {
        console.warn(
          "[dash-shot] no table data",
          attrName(host) || attrType(host)
        );
        continue;
      }
      var base =
        (attrName(host) || attrType(host) || "table").replace(/\s+/g, "_") +
        "_" +
        stamp();
      // BOM — Excel нормально открывает UTF-8
      var csv = "\ufeff" + toCsv(matrix);
      var csvName = base + ".csv";
      try {
        var b64 = btoa(unescape(encodeURIComponent(csv)));
        var savedMeta = await postExportFile(csvName, b64, "csv");
        paths.push(savedMeta.name || csvName);
        // один браузерный download CSV (Chrome режет пачку download'ов)
        if (saved === 0) {
          downloadBlob(
            new Blob([csv], { type: "text/csv;charset=utf-8" }),
            csvName
          );
        }
      } catch (e) {
        console.warn("[dash-shot] csv upload failed", e);
        downloadBlob(
          new Blob([csv], { type: "text/csv;charset=utf-8" }),
          csvName
        );
        paths.push(csvName + " (download)");
      }
      // PNG таблицы — только на сервер, без второго browser download
      try {
        var tableCanvas = await window.html2canvas(host, {
          scale: SCALE,
          useCORS: true,
          allowTaint: true,
          backgroundColor: "#ffffff",
          logging: false,
          foreignObjectRendering: false,
        });
        var blob = await new Promise(function (resolve, reject) {
          tableCanvas.toBlob(function (b) {
            if (b) resolve(b);
            else reject(new Error("toBlob failed"));
          }, "image/png");
        });
        var pngName = base + ".png";
        var pb64 = await blobToBase64(blob);
        var pngMeta = await postExportFile(pngName, pb64, "png");
        paths.push(pngMeta.name || pngName);
      } catch (e) {
        console.warn("[dash-shot] table png failed", e);
      }
      saved++;
      console.log(
        "[dash-shot] table saved",
        attrName(host),
        "rows",
        matrix.length
      );
    }
    return { saved: saved, paths: paths };
  }

  async function takeScreenshot() {
    var root = captureRoot();
    if (!root) {
      setStatus("панель не найдена");
      return;
    }
    var btn = document.getElementById("dashShotBtn");
    if (btn) btn.disabled = true;
    var restoreTables = function () {};
    setStatus("снимок…");
    try {
      await loadScript(HTML2CANVAS);
      var report = findReport();

      setStatus("раскрываем таблицы…");
      restoreTables = expandTables(root) || function () {};

      await new Promise(function (res) {
        requestAnimationFrame(function () {
          setTimeout(res, 80);
        });
      });

      setStatus("данные таблиц…");
      var tableResult = await saveTableData(root);
      var nTables = tableResult.saved || 0;

      // после expand — актуальные координаты; DOM карты не трогаем
      setStatus("карты из iframe…");
      var mapOverlays = await captureMapOverlays(root, report);

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
      });

      restoreTables();
      restoreTables = function () {};

      var nMaps = paintMapOverlays(canvas, mapOverlays);

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
        var bits = [];
        if (nMaps) bits.push(nMaps + " map");
        if (nTables) bits.push("csv в exports/ (" + nTables + ")");
        else bits.push("csv нет");
        setStatus("saved " + (body.name || name) + " | " + bits.join(" | "));
      } catch (err) {
        setStatus(
          "download ok" +
            (nMaps ? " +" + nMaps + " map" : "") +
            (nTables ? " | csv в exports/ (" + nTables + ")" : "")
        );
        console.warn(err);
      }
    } catch (err) {
      console.error(err);
      setStatus(String((err && err.message) || err));
    } finally {
      try {
        restoreTables();
      } catch (e) {}
      if (btn) btn.disabled = false;
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
    var pdfBtn = document.createElement("button");
    pdfBtn.id = "dashPivotPdfBtn";
    pdfBtn.type = "button";
    pdfBtn.textContent = "Pivot PDF";
    pdfBtn.title =
      "Экспорт PivotTable в PDF (все строки). Кнопка Save на элементе — Image/Excel/Data, не PDF.";
    pdfBtn.style.cssText = btnStyle;
    pdfBtn.onclick = function () {
      exportPivotsToPdf();
    };
    var status = document.createElement("span");
    status.id = "dashShotStatus";
    status.style.cssText =
      "max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.9;";
    bar.appendChild(btn);
    bar.appendChild(pdfBtn);
    bar.appendChild(status);
    document.body.appendChild(bar);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }

  window.__dashTakeScreenshot = takeScreenshot;
  window.__dashExportPivotsPdf = exportPivotsToPdf;
})();
