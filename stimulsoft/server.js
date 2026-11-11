// Official sample:
// https://github.com/stimulsoft/Samples-Dashboards.JS-for-Node.js/tree/master/Using%20with%20Express
const fs = require("fs");
const path = require("path");
const express = require("express");

const ROOT = __dirname;
const REPORTS_SCRIPTS = path.join(ROOT, "node_modules", "stimulsoft-reports-js", "Scripts");
const DASH_SCRIPTS = path.join(ROOT, "node_modules", "stimulsoft-dashboards-js", "Scripts");
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
const ADAPTER_URL = (process.env.DATA_ADAPTER_URL || "http://localhost:9615").trim();

const app = express();

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
          pgConn: pgConn(),
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

app.get("/dashboard", (_req, res) => {
  if (!fs.existsSync(MRT)) {
    res.status(404).type("application/json").send(JSON.stringify({ error: "mrt missing", path: MRT }));
    return;
  }
  res.set("Cache-Control", "no-store");
  res.sendFile(MRT);
});

// снимок из Designer Preview → /viewer?from=preview
app.get("/dashboard-preview", (_req, res) => {
  if (!fs.existsSync(PREVIEW_MRT)) {
    res.status(404).type("application/json").send(JSON.stringify({ error: "no preview snapshot" }));
    return;
  }
  res.set("Cache-Control", "no-store");
  res.type("application/json").send(fs.readFileSync(PREVIEW_MRT, "utf8"));
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
  console.log("stimulsoft-reports-js / stimulsoft-dashboards-js 2024.3.3");
  console.log("Data adapter must be running: npm run adapter  (port 9615)");
});
