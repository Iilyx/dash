/* Кнопка «Скрин».
 *
 * Stimulsoft кладёт RegionMap HTML в iframe (InsertHTMLContentToDashboardElement + document.write).
 * html2canvas iframe не видит → карта пустая.
 * Фикс: читаем SVG из iframe.contentDocument, печём в <canvas>, прячем iframe, снимаем страницу.
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
      canvas.style.cssText =
        "position:absolute;left:0;top:0;width:100%;height:100%;" +
        "display:block;z-index:3;pointer-events:none;border:none";
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
   * RegionMap: SVG живёт внутри iframe → canvas в host, iframe hide.
   * OnlineMap без SVG: Stimulsoft Image export → <img>.
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
      });

      if (getComputedStyle(host).position === "static") {
        restorers.push({ el: host, position: host.style.position });
        host.style.position = "relative";
      }

      if (svg) {
        try {
          var baked = await svgToCanvas(svg, win);
          restorers.push({ iframe: iframe, display: iframe.style.display });
          iframe.style.display = "none";
          restorers.push({ canvas: baked.canvas, host: host });
          host.appendChild(baked.canvas);
          console.log("[dash-shot] baked iframe SVG", name);
          continue;
        } catch (e) {
          console.warn("[dash-shot] iframe SVG bake failed", name, e);
        }
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
          var rect = host.getBoundingClientRect();
          var data = await exportOnlineMapPng(mapEl, rect.width, rect.height);
          var url = bytesToPngDataUrl(data);
          if (!url || url.length < 200) throw new Error("empty sti png");
          var imgEl = document.createElement("img");
          imgEl.setAttribute("data-sti-map-bake", "1");
          imgEl.src = url;
          imgEl.style.cssText =
            "position:absolute;left:0;top:0;width:100%;height:100%;" +
            "object-fit:fill;border:none;z-index:3;pointer-events:none";
          // ink check
          var probeImg = await loadImage(url);
          var probe = document.createElement("canvas");
          probe.width = Math.max(1, Math.round(rect.width));
          probe.height = Math.max(1, Math.round(rect.height));
          probe.getContext("2d").drawImage(probeImg, 0, 0, probe.width, probe.height);
          if (!canvasHasInk(probe)) throw new Error("sti blank");
          restorers.push({ iframe: iframe, display: iframe.style.display });
          iframe.style.display = "none";
          restorers.push({ img: imgEl });
          host.appendChild(imgEl);
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

  async function takeScreenshot() {
    var root = captureRoot();
    if (!root) {
      setStatus("панель не найдена");
      return;
    }
    var btn = document.getElementById("dashShotBtn");
    if (btn) btn.disabled = true;
    var restore = function () {};
    setStatus("снимок…");
    try {
      await loadScript(HTML2CANVAS);
      var report = findReport();

      setStatus("карты из iframe…");
      restore = (await bakeMaps(root, report)) || function () {};

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
      });

      restore();
      restore = function () {};

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
        restore();
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
