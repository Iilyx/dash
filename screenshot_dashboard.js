/* Кнопка «Скрин».
 * RegionMap = SVG (общий html2canvas часто «съедает»).
 * OnlineMap = iframe (html2canvas не видит).
 * Делаем общий снимок + отдельные оверлеи карт поверх. */
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

  function isMapReportElement(c) {
    if (!c) return false;
    try {
      var D = Stimulsoft.Report && Stimulsoft.Report.Dashboard;
      if (D) {
        if (D.IStiRegionMapElement && c.is && c.is(D.IStiRegionMapElement))
          return true;
        if (D.IStiOnlineMapElement && c.is && c.is(D.IStiOnlineMapElement))
          return true;
      }
    } catch (e) {}
    try {
      var Comp = Stimulsoft.Dashboard && Stimulsoft.Dashboard.Components;
      var Region =
        Comp && Comp.RegionMap && Comp.RegionMap.StiRegionMapElement;
      var Online =
        Comp && Comp.OnlineMap && Comp.OnlineMap.StiOnlineMapElement;
      if (Region && ((c.is && c.is(Region)) || c instanceof Region)) return true;
      if (Online && ((c.is && c.is(Online)) || c instanceof Online)) return true;
    } catch (e) {}
    var tn = typeName(c);
    if (/RegionMap|OnlineMap|MapElement/i.test(tn)) return true;
    try {
      // RegionMap в .mrt часто с MapIdent (даже пустым) + meters
      if ("mapIdent" in c || "MapIdent" in c) return true;
      if (c.keyMeter != null || c.nameMeter != null || c.valueMeter != null) {
        if (c.mapType != null || c.labels != null) return true;
      }
      if (c.latitude != null || c.longitude != null) return true;
    } catch (e) {}
    return false;
  }

  function attrType(el) {
    var a = el && el.elementAttributes;
    return a && a.type ? String(a.type) : "";
  }

  function attrName(el) {
    var a = el && el.elementAttributes;
    return a && a.name ? String(a.name) : "";
  }

  function isMapAttrType(t) {
    return (
      t === "StiRegionMapElement" ||
      t === "StiOnlineMapElement" ||
      /Map/i.test(t)
    );
  }

  /** Находит DOM-хосты карт без опоры на пустые Name в .mrt */
  function collectMapDomNodes(root) {
    var out = [];
    var seen = [];
    if (!root) return out;
    function add(el) {
      if (!el || seen.indexOf(el) >= 0) return;
      // не берём крошечные узлы
      var r = el.getBoundingClientRect();
      if (r.width < 40 || r.height < 40) return;
      seen.push(el);
      out.push(el);
    }

    var nodes = root.getElementsByTagName("*");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var t = attrType(el);
      if (t && isMapAttrType(t)) add(el);
    }

    // iframe = OnlineMap
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

    // RegionMap часто большой SVG без type в parent — ищем «тяжёлые» svg
    if (!out.length) {
      var svgs = root.querySelectorAll("svg");
      for (var s = 0; s < svgs.length; s++) {
        var svg = svgs[s];
        var paths = svg.querySelectorAll("path, polygon, polyline").length;
        var box = svg.getBoundingClientRect();
        if (paths >= 8 && box.width >= 80 && box.height >= 80) {
          var host2 = svg.parentElement;
          // поднимаемся к elementAttributes если есть
          var climb = host2;
          while (climb && climb !== root) {
            if (climb.elementAttributes) {
              host2 = climb;
              break;
            }
            climb = climb.parentElement;
          }
          add(host2 || svg);
        }
      }
    }

    console.log("[dash-shot] map DOM hosts", out.length, out.map(function (el) {
      return {
        type: attrType(el),
        name: attrName(el),
        tag: el.tagName,
        w: Math.round(el.getBoundingClientRect().width),
        h: Math.round(el.getBoundingClientRect().height),
        svg: !!(el.querySelector && el.querySelector("svg")),
        iframe: !!(el.querySelector && el.querySelector("iframe")),
      };
    }));
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

  function isValidPngDataUrl(url) {
    return !!(url && typeof url === "string" && url.indexOf("data:image/") === 0 && url.length > 200);
  }

  function exportMapPngStimulsoft(mapEl, cssW, cssH) {
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

  function captureDomMapPng(dom) {
    var target = dom;
    if (dom.contentPanel) target = dom.contentPanel;
    else {
      var svg = dom.querySelector && dom.querySelector("svg");
      if (svg && svg.parentElement) target = svg.parentElement;
    }
    return window
      .html2canvas(target, {
        scale: SCALE,
        useCORS: true,
        allowTaint: true,
        backgroundColor: "#ffffff",
        logging: false,
        foreignObjectRendering: false,
      })
      .then(function (canvas) {
        if (!canvas || canvas.width < 2 || canvas.height < 2) {
          throw new Error("пустой canvas карты");
        }
        return canvas.toDataURL("image/png");
      });
  }

  function findReportMap(report, name) {
    var maps = collectAll(report).filter(isMapReportElement);
    if (!maps.length) return null;
    if (name) {
      for (var i = 0; i < maps.length; i++) {
        if (maps[i].name === name) return maps[i];
      }
    }
    return maps.length === 1 ? maps[0] : maps[0];
  }

  function resolveMapPng(dom, report) {
    var name = attrName(dom);
    var t = attrType(dom);
    var rect = dom.getBoundingClientRect();
    var hasIframe = !!(dom.frame || (dom.querySelector && dom.querySelector("iframe")));
    var mapEl = report ? findReportMap(report, name) : null;

    // Online / iframe → сначала Stimulsoft Image
    if (hasIframe || /OnlineMap/i.test(t)) {
      if (mapEl) {
        return exportMapPngStimulsoft(mapEl, rect.width, rect.height)
          .then(function (data) {
            var url = bytesToPngDataUrl(data);
            if (isValidPngDataUrl(url)) return url;
            throw new Error("empty sti map png");
          })
          .catch(function (e) {
            console.warn("[dash-shot] sti map failed, try DOM", name, e);
            return captureDomMapPng(dom);
          });
      }
      return captureDomMapPng(dom);
    }

    // RegionMap SVG → DOM snapshot надёжнее, чем Stimulsoft Image (часто 0 байт)
    return captureDomMapPng(dom)
      .then(function (url) {
        if (isValidPngDataUrl(url)) return url;
        throw new Error("bad dom map png");
      })
      .catch(function (e) {
        console.warn("[dash-shot] DOM map failed", name || t, e);
        if (!mapEl) return null;
        return exportMapPngStimulsoft(mapEl, rect.width, rect.height).then(
          function (data) {
            return bytesToPngDataUrl(data);
          }
        );
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

  async function buildMapOverlays(root, report) {
    var hosts = collectMapDomNodes(root);
    var reportMaps = report ? collectAll(report).filter(isMapReportElement) : [];
    setStatus("карты DOM " + hosts.length + "/" + reportMaps.length);
    console.log(
      "[dash-shot] report maps",
      reportMaps.length,
      reportMaps.map(function (c) {
        return { name: c.name, type: typeName(c), mapIdent: c.mapIdent };
      })
    );

    var rootRect = root.getBoundingClientRect();
    var overlays = [];
    for (var i = 0; i < hosts.length; i++) {
      var dom = hosts[i];
      var rect = dom.getBoundingClientRect();
      if (!(rect.width > 20 && rect.height > 20)) continue;
      setStatus("карта " + (i + 1) + "/" + hosts.length);
      var url = await resolveMapPng(dom, report);
      if (!isValidPngDataUrl(url)) {
        console.warn("[dash-shot] skip empty overlay", attrName(dom), attrType(dom));
        continue;
      }
      overlays.push({
        url: url,
        x: (rect.left - rootRect.left) * SCALE,
        y: (rect.top - rootRect.top) * SCALE,
        w: rect.width * SCALE,
        h: rect.height * SCALE,
        name: attrName(dom) || attrType(dom) || "map",
      });
      console.log("[dash-shot] overlay ready", overlays[overlays.length - 1].name);
    }

    // если DOM пуст, но report знает карты — пробуем Stimulsoft-only (без позиции ≈ skip)
    if (!overlays.length && reportMaps.length) {
      console.warn("[dash-shot] DOM maps empty, report has", reportMaps.length);
    }
    return overlays;
  }

  async function takeScreenshot() {
    var root = captureRoot();
    if (!root) {
      setStatus("панель не найдена");
      return;
    }
    var btn = document.getElementById("dashShotBtn");
    if (btn) btn.disabled = true;
    setStatus("снимок…");
    try {
      await loadScript(HTML2CANVAS);
      var report = findReport();
      if (!report) console.warn("[dash-shot] report not found");

      setStatus("оверлеи карт…");
      var overlays = await buildMapOverlays(root, report);

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

      if (overlays.length) {
        setStatus("наложение " + overlays.length);
        var ctx = canvas.getContext("2d");
        for (var i = 0; i < overlays.length; i++) {
          var o = overlays[i];
          try {
            var img = await loadImage(o.url);
            ctx.drawImage(img, o.x, o.y, o.w, o.h);
          } catch (e) {
            console.warn("[dash-shot] draw overlay failed", o.name, e);
          }
        }
      } else {
        console.warn("[dash-shot] no map overlays");
      }

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
        if (res.ok)
          setStatus(
            "saved " +
              (body.name || name) +
              (overlays.length ? " +" + overlays.length + " map" : " (no map)")
          );
        else setStatus("download ok; server: " + (body.error || res.status));
      } catch (err) {
        setStatus(
          "download ok" + (overlays.length ? " +" + overlays.length + " map" : "")
        );
        console.warn(err);
      }
    } catch (err) {
      console.error(err);
      setStatus(String((err && err.message) || err));
    } finally {
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
