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
    return /ComboBox|ListBox|DatePicker|TreeViewBox|NumberBox/i.test(attrType(el));
  }

  function readFilterLabel(host) {
    var label = "";
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
    if (!label) {
      try {
        var inp = host.querySelector(
          "input:not([type=hidden]), textarea, [contenteditable='true']"
        );
        if (inp) {
          label = (inp.value != null && String(inp.value)) || inp.textContent || "";
        }
      } catch (e) {}
    }
    // Stimulsoft DropDownList: .text / .key на контроле
    if (!label) {
      try {
        var nodes = host.querySelectorAll("*");
        for (var i = 0; i < nodes.length; i++) {
          var n = nodes[i];
          if (n.text != null && String(n.text).trim()) {
            label = String(n.text).trim();
            break;
          }
          if (n.key != null && n.textBox && n.textBox.value) {
            label = String(n.textBox.value).trim();
            break;
          }
        }
      } catch (e) {}
    }
    if (!label) {
      try {
        var ea = host.elementAttributes;
        if (ea && ea.contentAttributes) {
          var ca = ea.contentAttributes;
          if (ca.selectedValues && ca.selectedValues.length)
            label = String(ca.selectedValues[0]);
          else if (ca.text) label = String(ca.text);
          else if (ca.value) label = String(ca.value);
        }
      } catch (e) {}
    }
    return String(label || "").trim();
  }

  /**
   * html2canvas часто не рисует value у ComboBox/select/input.
   * Печём видимый текст выбранного фильтра поверх контрола.
   */
  function bakeFilterValues(root) {
    var restorers = [];
    var hosts = [];
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      if (isFilterHost(nodes[i])) hosts.push(nodes[i]);
    }
    // нативные select вне hosts
    var selects = root.querySelectorAll("select");
    console.log("[dash-shot] filter hosts", hosts.length, "selects", selects.length);

    function placeLabel(target, text, host) {
      if (!text) return;
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
        "background:" + (cs.backgroundColor && cs.backgroundColor !== "rgba(0, 0, 0, 0)"
          ? cs.backgroundColor
          : "#ffffff"),
        "color:" + (cs.color || "#111"),
        "font:" + cs.font,
        "border-radius:" + (cs.borderRadius || "0"),
        "z-index:2",
        "pointer-events:none",
      ].join(";");
      host.appendChild(div);
      restorers.push({ div: div });
      // прячем исходный контрол на время снимка (иначе пустой поверх/под)
      restorers.push({ el: target, visibility: target.style.visibility });
      target.style.visibility = "hidden";
    }

    for (var h = 0; h < hosts.length; h++) {
      var host = hosts[h];
      var label = readFilterLabel(host);
      console.log(
        "[dash-shot] filter",
        attrName(host) || attrType(host),
        JSON.stringify(label)
      );
      if (!label) continue;
      var paintTarget =
        host.querySelector("select, input:not([type=hidden]), textarea") ||
        host.contentPanel ||
        host;
      // не затирать titlePanel
      if (paintTarget === host && host.contentPanel) paintTarget = host.contentPanel;
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
    var root = captureRoot();
    if (!root) {
      setStatus("панель не найдена");
      return;
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
        var res = await fetch("/api/screenshot", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name, data_b64: b64 }),
        });
        var body = await res.json().catch(function () {
          return {};
        });
        if (res.ok) setStatus("saved " + (body.name || name));
        else setStatus("download ok; server: " + (body.error || res.status));
      } catch (err) {
        setStatus("download ok");
        console.warn(err);
      }
    } catch (err) {
      console.error(err);
      setStatus(String((err && err.message) || err));
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

  function mount() {
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }

  window.__dashTakeScreenshot = takeScreenshot;
})();
