/* Кнопка «Скрин».
 *
 * RegionMap = SVG. html2canvas SVG часто глотает / даёт белое.
 * Перед снимком: SVG → canvas (serialize + drawImage) прямо в DOM, потом restore.
 * OnlineMap iframe: Stimulsoft Image export → <img> вместо iframe.
 * Пустые/белые оверлеи НЕ кладём (из‑за них «стало хуже»).
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
    var arr = [];
    for (var i = 0; i < kids.count; i++) arr.push(kids.getByIndex(i));
    for (var j = 0; j < arr.length; j++) walkComponents(arr[j], fn);
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
    try {
      var keys = Object.keys(window);
      for (var i = 0; i < keys.length; i++) {
        var v = window[keys[i]];
        if (v && v.report && v.jsObject && v.options) return v.report;
      }
    } catch (e) {}
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
    try {
      var M =
        Stimulsoft.Dashboard &&
        Stimulsoft.Dashboard.Components &&
        Stimulsoft.Dashboard.Components.OnlineMap &&
        Stimulsoft.Dashboard.Components.OnlineMap.StiOnlineMapElement;
      if (M && ((c.is && c.is(M)) || c instanceof M)) return true;
    } catch (e) {}
    return /OnlineMap/i.test(typeName(c));
  }

  function attrType(el) {
    var a = el && el.elementAttributes;
    return a && a.type ? String(a.type) : "";
  }

  function attrName(el) {
    var a = el && el.elementAttributes;
    return a && a.name ? String(a.name) : "";
  }

  /** SVG с инлайном fill/stroke из computed style — иначе serialize теряет цвета. */
  function cloneSvgInlined(svg) {
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
      var cs = window.getComputedStyle(srcNodes[i]);
      for (var p = 0; p < props.length; p++) {
        var val = cs.getPropertyValue(props[p]);
        if (val && val !== "none") {
          try {
            dstNodes[i].setAttribute(props[p], val);
          } catch (e) {}
        }
      }
    }
    var box = svg.getBoundingClientRect();
    var w = Math.max(1, Math.round(box.width));
    var h = Math.max(1, Math.round(box.height));
    clone.setAttribute("width", String(w));
    clone.setAttribute("height", String(h));
    if (!clone.getAttribute("viewBox")) {
      try {
        var vb = svg.viewBox && svg.viewBox.baseVal;
        if (vb && vb.width > 0) {
          clone.setAttribute(
            "viewBox",
            vb.x + " " + vb.y + " " + vb.width + " " + vb.height
          );
        } else {
          clone.setAttribute("viewBox", "0 0 " + w + " " + h);
        }
      } catch (e) {
        clone.setAttribute("viewBox", "0 0 " + w + " " + h);
      }
    }
    clone.style.position = "";
    clone.style.left = "";
    clone.style.top = "";
    clone.style.transform = "";
    return { clone: clone, w: w, h: h };
  }

  function svgToCanvas(svg) {
    var prepared = cloneSvgInlined(svg);
    var xml = new XMLSerializer().serializeToString(prepared.clone);
    if (xml.indexOf("xmlns") < 0) {
      xml = xml.replace(
        "<svg",
        '<svg xmlns="http://www.w3.org/2000/svg"'
      );
    }
    var url =
      "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
    return loadImage(url).then(function (img) {
      var canvas = document.createElement("canvas");
      canvas.width = Math.max(1, prepared.w * SCALE);
      canvas.height = Math.max(1, prepared.h * SCALE);
      canvas.style.width = prepared.w + "px";
      canvas.style.height = prepared.h + "px";
      canvas.style.display = "block";
      canvas.setAttribute("data-sti-map-bake", "1");
      var ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      if (!canvasHasInk(canvas)) {
        throw new Error("svg canvas blank");
      }
      return canvas;
    });
  }

  /** Есть ли не-белые пиксели (иначе оверлей только портит). */
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
          var r = data[i];
          var g = data[i + 1];
          var b = data[i + 2];
          var a = data[i + 3];
          samples++;
          if (a > 8 && (r < 250 || g < 250 || b < 250)) colored++;
        }
      }
      var ok = colored >= Math.max(3, samples * 0.01);
      console.log(
        "[dash-shot] ink check",
        colored + "/" + samples,
        ok ? "ok" : "blank"
      );
      return ok;
    } catch (e) {
      console.warn("[dash-shot] ink check failed", e);
      return true;
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
        var finish = function (data) {
          resolve(data);
        };
        if (typeof Tools.exportAsync === "function") {
          Tools.exportAsync(finish, mapEl, s);
        } else {
          Promise.resolve(Tools.export(mapEl, s)).then(finish).catch(reject);
        }
      } catch (e) {
        reject(e);
      }
    });
  }

  function findOnlineMapReport(report, name) {
    var maps = collectAll(report).filter(isOnlineMapElement);
    if (!maps.length) return null;
    if (name) {
      for (var i = 0; i < maps.length; i++) {
        if (maps[i].name === name) return maps[i];
      }
    }
    return maps[0];
  }

  /**
   * Подменяет SVG карт на canvas в DOM (html2canvas canvas видит).
   * Возвращает restore().
   */
  async function bakeRegionMapSvgs(root) {
    var restorers = [];
    var svgs = root.querySelectorAll("svg");
    var candidates = [];
    for (var i = 0; i < svgs.length; i++) {
      var svg = svgs[i];
      var box = svg.getBoundingClientRect();
      var paths = svg.querySelectorAll("path, polygon, polyline, circle").length;
      if (paths >= 5 && box.width >= 60 && box.height >= 60) {
        candidates.push(svg);
      }
    }
    // также SVG внутри RegionMap host
    var nodes = root.querySelectorAll("*");
    for (var n = 0; n < nodes.length; n++) {
      var t = attrType(nodes[n]);
      if (/RegionMap/i.test(t)) {
        var s2 = nodes[n].querySelector("svg");
        if (s2 && candidates.indexOf(s2) < 0) candidates.push(s2);
      }
    }
    console.log("[dash-shot] region svg candidates", candidates.length);
    setStatus("SVG карт " + candidates.length);

    for (var c = 0; c < candidates.length; c++) {
      var svgEl = candidates[c];
      var parent = svgEl.parentNode;
      if (!parent) continue;
      try {
        var canvas = await svgToCanvas(svgEl);
        // сохранить layout
        var cs = window.getComputedStyle(svgEl);
        canvas.style.position = cs.position === "static" ? "relative" : cs.position;
        if (cs.position === "absolute" || cs.position === "fixed") {
          canvas.style.left = svgEl.style.left || cs.left;
          canvas.style.top = svgEl.style.top || cs.top;
        }
        parent.replaceChild(canvas, svgEl);
        restorers.push({ parent: parent, svg: svgEl, canvas: canvas });
        console.log("[dash-shot] baked SVG→canvas", c + 1);
      } catch (e) {
        console.warn("[dash-shot] SVG bake failed", e);
      }
    }

    return function restore() {
      restorers.forEach(function (x) {
        try {
          if (x.canvas && x.canvas.parentNode === x.parent) {
            x.parent.replaceChild(x.svg, x.canvas);
          } else if (x.canvas && x.canvas.parentNode) {
            x.canvas.parentNode.removeChild(x.canvas);
            if (x.svg && !x.svg.parentNode) x.parent.appendChild(x.svg);
          }
        } catch (e) {}
      });
    };
  }

  /**
   * OnlineMap iframe → PNG img поверх.
   */
  async function bakeOnlineMaps(root, report) {
    var restorers = [];
    if (!report) return function () {};

    var hosts = [];
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      var t = attrType(nodes[i]);
      if (/OnlineMap/i.test(t)) hosts.push(nodes[i]);
    }
    var iframes = root.querySelectorAll("iframe");
    for (var j = 0; j < iframes.length; j++) {
      var host = iframes[j].parentElement;
      while (host && host !== root) {
        if (host.elementAttributes) {
          if (hosts.indexOf(host) < 0) hosts.push(host);
          break;
        }
        host = host.parentElement;
      }
    }
    console.log("[dash-shot] online hosts", hosts.length);
    if (!hosts.length) return function () {};

    setStatus("OnlineMap " + hosts.length);
    for (var h = 0; h < hosts.length; h++) {
      var dom = hosts[h];
      // если уже есть SVG-bake — не трогаем
      if (dom.querySelector('canvas[data-sti-map-bake="1"]')) continue;
      var name = attrName(dom);
      var mapEl = findOnlineMapReport(report, name);
      if (!mapEl) {
        var all = collectAll(report).filter(isOnlineMapElement);
        mapEl = all[h] || all[0];
      }
      if (!mapEl) continue;
      var rect = dom.getBoundingClientRect();
      try {
        var data = await exportOnlineMapPng(mapEl, rect.width, rect.height);
        var url = bytesToPngDataUrl(data);
        if (!url || url.length < 200) throw new Error("empty sti map png");
        var img = await loadImage(url);
        // проверка ink через временный canvas
        var probe = document.createElement("canvas");
        probe.width = Math.max(1, Math.round(rect.width));
        probe.height = Math.max(1, Math.round(rect.height));
        probe.getContext("2d").drawImage(img, 0, 0, probe.width, probe.height);
        if (!canvasHasInk(probe)) throw new Error("sti map blank");

        var iframe = dom.frame || dom.querySelector("iframe");
        var bakeImg = document.createElement("img");
        bakeImg.setAttribute("data-sti-map-bake", "1");
        bakeImg.src = url;
        bakeImg.alt = "map";
        bakeImg.style.cssText =
          "position:absolute;left:0;top:0;width:100%;height:100%;" +
          "object-fit:fill;border:none;z-index:2;pointer-events:none";
        if (getComputedStyle(dom).position === "static") {
          restorers.push({ el: dom, position: dom.style.position });
          dom.style.position = "relative";
        }
        if (iframe) {
          restorers.push({ iframe: iframe, display: iframe.style.display });
          iframe.style.display = "none";
        }
        restorers.push({ img: bakeImg });
        dom.appendChild(bakeImg);
        console.log("[dash-shot] baked OnlineMap", name || mapEl.name);
      } catch (e) {
        console.warn("[dash-shot] OnlineMap bake skipped", name, e);
      }
    }

    return function restore() {
      restorers.forEach(function (x) {
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

  async function takeScreenshot() {
    var root = captureRoot();
    if (!root) {
      setStatus("панель не найдена");
      return;
    }
    var btn = document.getElementById("dashShotBtn");
    if (btn) btn.disabled = true;
    var restoreSvg = function () {};
    var restoreOnline = function () {};
    setStatus("снимок…");
    try {
      await loadScript(HTML2CANVAS);
      var report = findReport();
      if (!report) console.warn("[dash-shot] report not found");

      setStatus("печём SVG…");
      restoreSvg = (await bakeRegionMapSvgs(root)) || function () {};

      setStatus("печём OnlineMap…");
      restoreOnline = (await bakeOnlineMaps(root, report)) || function () {};

      await new Promise(function (res) {
        requestAnimationFrame(function () {
          setTimeout(res, 60);
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
      });

      restoreSvg();
      restoreOnline();
      restoreSvg = function () {};
      restoreOnline = function () {};

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
        restoreSvg();
      } catch (e) {}
      try {
        restoreOnline();
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
