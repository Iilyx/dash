// Official sample:
// https://github.com/stimulsoft/Samples-Dashboards.JS-for-Node.js/tree/master/Using%20with%20Express
// + SQL adapter same-origin /data (Postgres)
const fs = require("fs");
const path = require("path");
const express = require("express");
const adapter = require("stimulsoft-data-adapter");

const ROOT = __dirname;
const REPORTS_SCRIPTS = path.join(ROOT, "node_modules", "stimulsoft-reports-js", "Scripts");
const DASH_SCRIPTS = path.join(ROOT, "node_modules", "stimulsoft-dashboards-js", "Scripts");
const DASH_CSS = path.join(ROOT, "node_modules", "stimulsoft-dashboards-js", "Css");
const port = Number(process.env.PORT || process.env.DESIGNER_PORT || 8040);

function loadEnv() {
  for (const candidate of [path.join(ROOT, ".env"), path.join(ROOT, "..", ".env")]) {
    if (!fs.existsSync(candidate)) continue;
    for (const line of fs.readFileSync(candidate, "utf8").split("\n")) {
      const s = line.trim();
      if (!s || s.startsWith("#") || !s.includes("=")) continue;
      const i = s.indexOf("=");
      const k = s.slice(0, i).trim();
      const v = s.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
      if (!(k in process.env)) process.env[k] = v;
    }
  }
}
loadEnv();

function mrtPath() {
  const raw = (process.env.MRT_PATH || path.join(ROOT, "SampleDashboard.mrt")).trim();
  return path.isAbsolute(raw) ? raw : path.resolve(ROOT, raw);
}

function pgConn() {
  const url = (process.env.DATABASE_URL || "").trim();
  if (url) {
    try {
      const u = new URL(url);
      const database = u.pathname.replace(/^\//, "").split("?")[0];
      if (u.hostname && database) {
        return (
          "Host=" + u.hostname +
          "; Port=" + (u.port || "5432") +
          "; Database=" + database +
          "; User Id=" + decodeURIComponent(u.username || "") +
          "; Password=" + decodeURIComponent(u.password || "") + ";"
        );
      }
    } catch (_) {
      /* fall through to POSTGRES_* */
    }
  }
  const user = (process.env.POSTGRES_USER || "").trim();
  const password = (process.env.POSTGRES_PASSWORD || "").trim();
  const db = (process.env.POSTGRES_DB || "").trim();
  const host = (process.env.POSTGRES_HOST || "127.0.0.1").trim();
  const pgPort = (process.env.POSTGRES_PORT || "5432").trim();
  if (!user || !password || !db) return "";
  return (
    "Host=" + host + "; Port=" + pgPort + "; Database=" + db +
    "; User Id=" + user + "; Password=" + password + ";"
  );
}

const MRT = mrtPath();
const PREVIEW_MRT = path.join(ROOT, ".preview.mrt");
const LICENSE =
  (process.env.STIMULSOFT_DASHBOARDS_LICENSE_KEY || "").trim() ||
  (process.env.STIMULSOFT_REPORTS_LICENSE_KEY || "").trim();
// same-origin по умолчанию; :9615 только если явно задали DATA_ADAPTER_URL
const ADAPTER_URL = (process.env.DATA_ADAPTER_URL || "").trim();
const PG = pgConn();

function envFlag(name, def) {
  const v = (process.env[name] != null ? process.env[name] : def);
  return String(v).toLowerCase() in { "1": 1, true: 1, yes: 1 };
}

// как в старом view_dashboard.py / Windows mrt_patch
// RequestParameters=true → белый холст до Submit. Для UX «дашборд + фильтры сверху» — false.
const FORCE_REQUEST_PARAMETERS = envFlag("FORCE_REQUEST_PARAMETERS", "0");
const FORCE_PARAMETERS_PANEL = envFlag("FORCE_PARAMETERS_PANEL", "1");
const FORCE_SHOW_ON_PARAMETERS_PANEL = envFlag("FORCE_SHOW_ON_PARAMETERS_PANEL", "1");
// не перетираем DashboardViewerSettings из .mrt — 671 иногда даёт пустой холст
const FORCE_DASHBOARD_TOOLBAR = envFlag("FORCE_DASHBOARD_TOOLBAR", "0");
const DASHBOARD_VIEWER_SETTINGS = 671;

const app = express();

// темы — без CSS холст часто белый
if (fs.existsSync(DASH_CSS)) {
  app.use("/Css", express.static(DASH_CSS));
}

// stimulsoft-data-adapter сравнивает database строго с "PostgreSQL"
function forcePg(command) {
  if (!command || typeof command !== "object") return command;
  if (PG) {
    command.connectionString = PG;
    command.database = "PostgreSQL";
  }
  return command;
}

function dataCors(res) {
  res.set({
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Origin, X-Requested-With, Content-Type, Accept",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Cache-Control": "no-cache",
  });
}

app.options("/data", (_req, res) => {
  dataCors(res);
  res.sendStatus(204);
});

// raw body — НЕ express.json/text (ломают encrypted payload)
app.post("/data", (req, res) => {
  dataCors(res);
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const raw = Buffer.concat(chunks).toString("utf8");
    let command;
    try {
      command = forcePg(adapter.getCommand(raw || "{}"));
    } catch (err) {
      console.error("[/data] getCommand", err && err.message ? err.message : err);
      res.end(
        adapter.getResponse({
          success: false,
          notice: String(err && err.message ? err.message : err),
          encryptData: false,
        })
      );
      return;
    }
    console.log(
      "[/data]",
      command.command,
      command.database,
      (command.queryString && String(command.queryString).slice(0, 160)) || ""
    );
    adapter.process(command, (result) => {
      if (result && result.success === false) {
        console.error("[/data] fail", result.notice || result);
      } else {
        const rows = (result && result.rows && result.rows.length) || 0;
        console.log("[/data] ok rows=", rows);
      }
      res.type("text/plain").end(adapter.getResponse(result));
    });
  });
});

// raw body for preview snapshot (не express.json — большие .mrt)
app.post(
  "/preview-mrt",
  express.text({ type: "*/*", limit: "50mb" }),
  (req, res) => {
    const body = typeof req.body === "string" ? req.body : "";
    if (!body.trim()) {
      res.status(400).json({ ok: false, error: "empty preview body" });
      return;
    }
    fs.writeFileSync(PREVIEW_MRT, body, "utf8");
    res.json({ ok: true });
  }
);

app.get("/config.js", (_req, res) => {
  res
    .type("application/javascript")
    .set("Cache-Control", "no-store")
    .send(
      "window.__STI__=" +
        JSON.stringify({
          licenseKey: LICENSE,
          pgConn: PG,
          // пусто → клиент возьмёт location.origin + "/data"
          dataAdapterUrl: ADAPTER_URL,
        }) +
        ";\n"
    );
});

app.get("/viewer", (_req, res) => {
  res.sendFile(path.join(ROOT, "viewer.html"));
});

app.get("/designer", (_req, res) => {
  res.sendFile(path.join(ROOT, "designer.html"));
});

app.get("/", (_req, res) => {
  res.redirect("/viewer");
});

function flagTrue(v) {
  return v === true || v === "true" || v === "True" || v === 1 || v === "1";
}
function flagFalse(v) {
  return v === false || v === "false" || v === "False" || v === 0 || v === "0";
}

function numbered(obj) {
  if (!obj) return [];
  if (Array.isArray(obj)) return obj;
  if (typeof obj !== "object") return [];
  return Object.keys(obj)
    .filter((k) => /^\d+$/.test(k))
    .sort((a, b) => Number(a) - Number(b))
    .map((k) => obj[k]);
}

function repairMrtJson(raw) {
  return String(raw).replace(/(:\s*)""\.([0-9]+)/g, "$1$2");
}

function varName(variable) {
  const n = variable && variable.Name != null ? String(variable.Name).trim() : "";
  return n;
}

function isPanelCandidate(variable) {
  if (!variable || typeof variable !== "object") return false;
  // безымянная переменная ломает Parameters panel
  if (!varName(variable)) return false;
  if (flagFalse(variable.RequestFromUser) && flagFalse(variable.ShowOnParametersPanel)) {
    return false;
  }
  if (flagTrue(variable.RequestFromUser) || flagTrue(variable.ShowOnParametersPanel)) {
    return true;
  }
  // обезличка: true→"" но ключ остаётся
  if ("RequestFromUser" in variable && !flagFalse(variable.RequestFromUser)) return true;
  if ("ShowOnParametersPanel" in variable && !flagFalse(variable.ShowOnParametersPanel)) {
    return true;
  }
  return false;
}

/**
 * Как Windows /debug mrt_patch:
 * RequestParameters + Panel + RFU/ShowOnParametersPanel + ScriptLanguage=JavaScript
 */
function patchMrt(raw) {
  const meta = {
    patched: false,
    script_language: null,
    request_parameters: false,
    parameters_display_mode: null,
    panel_candidates: 0,
    rfu_forced: 0,
    show_on_panel_forced: 0,
    dashboard_viewer_settings: null,
  };
  if (!raw) return { body: raw, meta };

  let text = String(raw).replace(/^\uFEFF/, "");

  // XML sample — только ScriptLanguage
  if (text.trimStart().startsWith("<")) {
    text = text.replace(
      /<ScriptLanguage>\s*(CSharp|VB|C#)\s*<\/ScriptLanguage>/gi,
      "<ScriptLanguage>JavaScript</ScriptLanguage>"
    );
    meta.script_language = "JavaScript";
    meta.patched = true;
    return { body: text, meta };
  }

  let report;
  try {
    report = JSON.parse(text);
  } catch (_) {
    try {
      text = repairMrtJson(text);
      report = JSON.parse(text);
      meta.json_repaired = true;
    } catch (exc) {
      meta.error = String(exc && exc.message ? exc.message : exc);
      return { body: text, meta };
    }
  }
  if (!report || typeof report !== "object") {
    return { body: text, meta };
  }

  let changed = false;

  const sl = report.ScriptLanguage;
  if (sl === "CSharp" || sl === "VB" || sl === "C#") {
    report.ScriptLanguage = "JavaScript";
    changed = true;
  }
  meta.script_language = report.ScriptLanguage || null;

  if (FORCE_REQUEST_PARAMETERS) {
    if (!flagTrue(report.RequestParameters)) {
      report.RequestParameters = true;
      changed = true;
    }
  } else if (flagTrue(report.RequestParameters) || report.RequestParameters === "") {
    // сразу рисуем дашборд с default-значениями переменных; Submit потом подставит новые
    report.RequestParameters = false;
    changed = true;
  }
  meta.request_parameters = flagTrue(report.RequestParameters);

  if (FORCE_PARAMETERS_PANEL) {
    const mode = report.ParametersDisplayMode;
    if (mode !== "Panel" && mode !== 1 && mode !== "1") {
      report.ParametersDisplayMode = "Panel";
      changed = true;
    }
  }
  meta.parameters_display_mode = report.ParametersDisplayMode || null;

  if (FORCE_DASHBOARD_TOOLBAR) {
    const prev = report.DashboardViewerSettings;
    let prevI = 0;
    try {
      prevI = prev != null && prev !== "" ? Number(prev) : 0;
    } catch (_) {
      prevI = 0;
    }
    if (prevI !== DASHBOARD_VIEWER_SETTINGS) {
      report.DashboardViewerSettings = DASHBOARD_VIEWER_SETTINGS;
      changed = true;
    }
  }
  meta.dashboard_viewer_settings = report.DashboardViewerSettings;

  const dictionary =
    report.Dictionary && typeof report.Dictionary === "object" ? report.Dictionary : {};
  let rfuForced = 0;
  let showForced = 0;
  let candidates = 0;
  let skippedEmptyName = 0;
  for (const variable of numbered(dictionary.Variables)) {
    if (!variable || typeof variable !== "object") continue;
    // RFU без Name — убрать с панели, иначе Stimulsoft рисует пусто
    if (!varName(variable) && (flagTrue(variable.RequestFromUser) || "RequestFromUser" in variable)) {
      if (!flagFalse(variable.RequestFromUser) || !flagFalse(variable.ShowOnParametersPanel)) {
        variable.RequestFromUser = false;
        variable.ShowOnParametersPanel = false;
        skippedEmptyName += 1;
        changed = true;
      }
      continue;
    }
    if (!isPanelCandidate(variable)) continue;
    candidates += 1;
    if (!flagTrue(variable.RequestFromUser)) {
      variable.RequestFromUser = true;
      rfuForced += 1;
      changed = true;
    }
    if (FORCE_SHOW_ON_PARAMETERS_PANEL && !flagTrue(variable.ShowOnParametersPanel)) {
      variable.ShowOnParametersPanel = true;
      showForced += 1;
      changed = true;
    }
  }
  meta.panel_candidates = candidates;
  meta.rfu_forced = rfuForced;
  meta.show_on_panel_forced = showForced;
  meta.skipped_empty_name = skippedEmptyName;
  meta.patched = changed;

  if (!changed) return { body: text, meta };
  return { body: JSON.stringify(report, null, 2) + "\n", meta };
}

function sendMrtFile(filePath, res) {
  if (!fs.existsSync(filePath)) {
    res.status(404).type("application/json").send(JSON.stringify({ error: "mrt missing", path: filePath }));
    return;
  }
  const raw = fs.readFileSync(filePath, "utf8");
  const { body, meta } = patchMrt(raw);
  console.log("[mrt]", path.basename(filePath), meta);
  res.set("Cache-Control", "no-store");
  res.type("application/json; charset=utf-8").send(body);
}

app.get("/dashboard", (_req, res) => {
  sendMrtFile(MRT, res);
});

// снимок из Designer Preview → /viewer?from=preview
app.get("/dashboard-preview", (_req, res) => {
  sendMrtFile(PREVIEW_MRT, res);
});

app.get("/debug", (_req, res) => {
  if (!fs.existsSync(MRT)) {
    res.status(404).json({ ok: false, error: "mrt missing", path: MRT });
    return;
  }
  const raw = fs.readFileSync(MRT, "utf8");
  const { body, meta } = patchMrt(raw);
  let report = null;
  let patchedReport = null;
  try {
    report = JSON.parse(raw.replace(/^\uFEFF/, ""));
  } catch (_) {
    try {
      report = JSON.parse(repairMrtJson(raw.replace(/^\uFEFF/, "")));
    } catch (e) {
      res.json({ ok: false, path: MRT, error: String(e.message || e), mrt_patch: meta });
      return;
    }
  }
  try {
    patchedReport = JSON.parse(body);
  } catch (_) {
    patchedReport = report;
  }
  const vars = numbered(
    report && report.Dictionary && report.Dictionary.Variables
  ).filter((v) => v && typeof v === "object");
  const patchedVars = numbered(
    patchedReport && patchedReport.Dictionary && patchedReport.Dictionary.Variables
  ).filter((v) => v && typeof v === "object");
  const panel = patchedVars.filter(isPanelCandidate).map((v) => ({
    name: varName(v),
    requestFromUser: v.RequestFromUser,
    showOnParametersPanel: v.ShowOnParametersPanel,
  }));
  const emptyNamedRfu = vars.filter(
    (v) =>
      !varName(v) &&
      (flagTrue(v.RequestFromUser) || ("RequestFromUser" in v && !flagFalse(v.RequestFromUser)))
  ).length;
  res.json({
    ok: true,
    path: MRT,
    reportVersion: report.ReportVersion || "",
    scriptLanguage_file: report.ScriptLanguage || "",
    scriptLanguage_served: patchedReport.ScriptLanguage || meta.script_language,
    requestParameters_served: patchedReport.RequestParameters,
    parametersDisplayMode_served: patchedReport.ParametersDisplayMode,
    all_variables_count: vars.length,
    panel_candidates: panel,
    panel_count: panel.length,
    empty_named_rfu_in_file: emptyNamedRfu,
    force_request_parameters: FORCE_REQUEST_PARAMETERS,
    force_parameters_panel: FORCE_PARAMETERS_PANEL,
    force_show_on_parameters_panel: FORCE_SHOW_ON_PARAMETERS_PANEL,
    mrt_patch: meta,
    note:
      panel.length === 0
        ? "Нет именованных RFU на панели — проверь .mrt."
        : "Сервер отдаёт Panel + " +
          panel.length +
          " фильтров (" +
          panel.map((p) => p.name).join(", ") +
          "). Открой /viewer — сверху параметры, ниже дашборд; Submit подставит значения в SQL.",
  });
});

function sendScript(dir, name) {
  return (_req, res) => {
    const file = path.join(dir, name);
    if (!fs.existsSync(file)) {
      res.status(404).send(name + " not found — run npm install");
      return;
    }
    res.type("application/javascript").sendFile(file);
  };
}

// reports engine из stimulsoft-reports-js
app.get("/stimulsoft.reports.js", sendScript(REPORTS_SCRIPTS, "stimulsoft.reports.js"));
// dashboards / viewer / designer из stimulsoft-dashboards-js
app.get("/stimulsoft.dashboards.js", sendScript(DASH_SCRIPTS, "stimulsoft.dashboards.js"));
app.get("/stimulsoft.viewer.js", sendScript(DASH_SCRIPTS, "stimulsoft.viewer.js"));
app.get("/stimulsoft.designer.js", sendScript(DASH_SCRIPTS, "stimulsoft.designer.js"));
app.get("/stimulsoft.blockly.editor.js", sendScript(DASH_SCRIPTS, "stimulsoft.blockly.editor.js"));

app.listen(port, "0.0.0.0", () => {
  console.log("Server running on port " + port);
  console.log("Navigate to http://localhost:" + port + "/viewer to show Viewer");
  console.log("Navigate to http://localhost:" + port + "/designer to show Designer");
  console.log("MRT: " + MRT);
  console.log("PG conn: " + (PG ? "ok" : "MISSING — set POSTGRES_* or DATABASE_URL"));
  console.log("License: " + (LICENSE ? "set" : "empty (trial)"));
  console.log("stimulsoft-reports-js / stimulsoft-dashboards-js 2024.3.3");
  console.log("SQL adapter: same-origin POST /data (force PostgreSQL)");
  console.log("Css: " + (fs.existsSync(DASH_CSS) ? "ok /Css" : "missing"));
});
