/* Кнопка «Скрин» — WYSIWYG снимок. Online Map в iframe html2canvas не видит:
 * перед снимком экспортируем карту в PNG через Stimulsoft и временно кладём <img>. */
(function () {
  var HTML2CANVAS =
    "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js";

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

  function isRegionMapElement(c) {
    if (!c) return false;
    try {
      var I =
        Stimulsoft.Report &&
        Stimulsoft.Report.Dashboard &&
        Stimulsoft.Report.Dashboard.IStiRegionMapElement;
      if (I && c.is && c.is(I)) return true;
    } catch (e) {}
    try {
      var M =
        Stimulsoft.Dashboard &&
        Stimulsoft.Dashboard.Components &&
        Stimulsoft.Dashboard.Components.RegionMap &&
        Stimulsoft.Dashboard.Components.RegionMap.StiRegionMapElement;
      if (M && ((c.is && c.is(M)) || c instanceof M)) return true;
    } catch (e) {}
    var tn = typeName(c);
    if (tn === "StiRegionMapElement" || /RegionMap/i.test(tn)) return true;
    try {
      if (c.mapIdent != null || (c.mapType != null && c.keyMeter != null))
        return true;
    } catch (e) {}
    return false;
  }

  function findDomByElementName(name) {
    var root = captureRoot();
    if (!root) return null;
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var a = el.elementAttributes;
      if (!a) continue;
      if (name && a.name === name) return el;
      if (
        !name &&
        (a.type === "StiOnlineMapElement" || a.type === "StiRegionMapElement")
      )
        return el;
    }
    return null;
  }

  function findOnlineMapDomHosts() {
    var root = captureRoot();
    var out = [];
    var seen = [];
    if (!root) return out;
    function add(el) {
      if (!el || seen.indexOf(el) >= 0) return;
      seen.push(el);
      out.push(el);
    }
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      var a = nodes[i].elementAttributes;
      if (a && a.type === "StiOnlineMapElement") add(nodes[i]);
    }
    var iframes = root.querySelectorAll("iframe");
    for (var j = 0; j < iframes.length; j++) {
      var host = iframes[j].parentElement;
      while (host && host !== root) {
        if (host.elementAttributes) {
          add(host);
          break;
        }
        host = host.parentElement;
      }
      if (!host || host === root) add(iframes[j].parentElement || iframes[j]);
    }
    return out;
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
    var isPng =
      data.length > 4 &&
      data[0] === 0x89 &&
      data[1] === 0x50 &&
      data[2] === 0x4e &&
      data[3] === 0x47;
    var isJpg = data.length > 2 && data[0] === 0xff && data[1] === 0xd8;
    var mime = isJpg ? "image/jpeg" : "image/png";
    if (!isPng && !isJpg) {
      // Stimulsoft иногда отдаёт уже base64-like / сырой PNG без проверки — всё равно пробуем
      mime = "image/png";
    }
    var bin = "";
    var step = 0x8000;
    for (var i = 0; i < data.length; i += step) {
      bin += String.fromCharCode.apply(
        null,
        data.subarray(i, Math.min(i + step, data.length))
      );
    }
    return "data:" + mime + ";base64," + btoa(bin);
  }

  function exportMapPng(mapEl) {
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
        s.imageQuality = 400;
        var w = Number(mapEl.width) || 0;
        var h = Number(mapEl.height) || 0;
        if (w > 0 && h > 0) {
          s.width = w;
          s.height = h;
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

  function placeBakeImg(dom, url) {
    var iframe = dom.frame || dom.querySelector("iframe");
    var img = document.createElement("img");
    img.setAttribute("data-sti-map-bake", "1");
    img.src = url;
    img.alt = "map";
    img.style.cssText =
      "position:absolute;left:0;top:0;width:100%;height:100%;" +
      "object-fit:fill;border:none;z-index:2;pointer-events:none";
    var restorers = [];
    if (iframe) {
      restorers.push({ iframe: iframe, display: iframe.style.display });
      iframe.style.display = "none";
    }
    // на всякий случай прячем leaflet/canvas-host если есть
    var canvases = dom.querySelectorAll("canvas");
    for (var i = 0; i < canvases.length; i++) {
      restorers.push({ el: canvases[i], display: canvases[i].style.display });
      canvases[i].style.display = "none";
    }
    if (getComputedStyle(dom).position === "static") {
      restorers.push({ el: dom, position: dom.style.position });
      dom.style.position = "relative";
    }
    restorers.push({ img: img });
    dom.appendChild(img);
    return restorers;
  }

  function bakeMapsIntoDom(report) {
    var restorers = [];
    var maps = collectAll(report).filter(function (c) {
      return isOnlineMapElement(c) || isRegionMapElement(c);
    });
    setStatus("карты " + maps.length + "…");
    var chain = Promise.resolve();

    // Online/Region из report API
    maps.forEach(function (mapEl) {
      chain = chain.then(function () {
        var dom = findDomByElementName(mapEl.name);
        if (!dom) {
          console.warn("[dash-shot] DOM map not found", mapEl.name);
          return;
        }
        // RegionMap часто уже SVG в DOM — html2canvas его видит; печём только если есть iframe
        var hasIframe = !!(dom.frame || dom.querySelector("iframe"));
        if (isRegionMapElement(mapEl) && !hasIframe && !isOnlineMapElement(mapEl)) {
          return;
        }
        return exportMapPng(mapEl)
          .then(function (data) {
            var url = bytesToPngDataUrl(data);
            if (!url) {
              console.warn("[dash-shot] empty map png", mapEl.name);
              return;
            }
            restorers = restorers.concat(placeBakeImg(dom, url));
            console.log("[dash-shot] baked map", mapEl.name);
          })
          .catch(function (e) {
            console.warn("[dash-shot] map bake failed", mapEl.name, e);
          });
      });
    });

    // fallback: iframe-хосты без найденного component
    chain = chain.then(function () {
      var hosts = findOnlineMapDomHosts();
      var next = Promise.resolve();
      hosts.forEach(function (dom) {
        next = next.then(function () {
          if (dom.querySelector('img[data-sti-map-bake="1"]')) return;
          var name =
            (dom.elementAttributes && dom.elementAttributes.name) || null;
          var mapEl = null;
          if (name) {
            var all = collectAll(report);
            for (var i = 0; i < all.length; i++) {
              if (all[i].name === name) {
                mapEl = all[i];
                break;
              }
            }
          }
          if (!mapEl) {
            var online = collectAll(report).filter(isOnlineMapElement);
            if (online.length === 1) mapEl = online[0];
          }
          if (!mapEl) return;
          return exportMapPng(mapEl)
            .then(function (data) {
              var url = bytesToPngDataUrl(data);
              if (!url) return;
              restorers = restorers.concat(placeBakeImg(dom, url));
              console.log("[dash-shot] baked iframe host", name || mapEl.name);
            })
            .catch(function (e) {
              console.warn("[dash-shot] iframe bake failed", e);
            });
        });
      });
      return next;
    });

    return chain.then(function () {
      return function restoreMaps() {
        restorers.forEach(function (x) {
          try {
            if (x.img && x.img.parentNode) x.img.parentNode.removeChild(x.img);
          } catch (e) {}
          try {
            if (x.iframe) x.iframe.style.display = x.display || "";
          } catch (e) {}
          try {
            if (x.el) {
              if (x.display != null) x.el.style.display = x.display;
              if (x.position != null) x.el.style.position = x.position;
            }
          } catch (e) {}
        });
      };
    });
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
    setStatus("снимок…");
    try {
      await loadScript(HTML2CANVAS);
      var report = findReport();
      if (report) {
        restoreMaps = (await bakeMapsIntoDom(report)) || function () {};
        await new Promise(function (res) {
          requestAnimationFrame(function () {
            setTimeout(res, 100);
          });
        });
      } else {
        console.warn("[dash-shot] report not found — карта может пропасть");
        setStatus("без report, карта?");
      }
      setStatus("рендер…");
      var canvas = await window.html2canvas(root, {
        scale: 2,
        useCORS: true,
        allowTaint: true,
        backgroundColor: "#ffffff",
        logging: false,
        ignoreElements: function (el) {
          return !!(el && el.id === "dashShotBar");
        },
      });
      restoreMaps();
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
      "max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.9;";
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
