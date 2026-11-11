// Official sample:
// https://github.com/stimulsoft/Samples-Dashboards.JS-for-Node.js/tree/master/Using%20with%20Express
const fs = require("fs");
const path = require("path");
const express = require("express");

const ROOT = __dirname;
const SCRIPTS = path.join(ROOT, "node_modules", "stimulsoft-dashboards-js", "Scripts");
const port = Number(process.env.PORT || process.env.DESIGNER_PORT || 8040);

function loadEnv() {
  const p = path.join(ROOT, ".env");
  if (!fs.existsSync(p)) return;
  for (const line of fs.readFileSync(p, "utf8").split("\n")) {
    const s = line.trim();
    if (!s || s.startsWith("#") || !s.includes("=")) continue;
    const i = s.indexOf("=");
    const k = s.slice(0, i).trim();
    const v = s.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
    if (!(k in process.env)) process.env[k] = v;
  }
}
loadEnv();

function mrtPath() {
  const raw = (process.env.MRT_PATH || path.join(ROOT, "dashboard.mrt")).trim();
  return path.isAbsolute(raw) ? raw : path.resolve(ROOT, raw);
}

function pgConn() {
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
const LICENSE =
  (process.env.STIMULSOFT_DASHBOARDS_LICENSE_KEY || "").trim() ||
  (process.env.STIMULSOFT_REPORTS_LICENSE_KEY || "").trim();

const app = express();

app.get("/config.js", (_req, res) => {
  // лицензия + connection string (дока: onBeginProcessData)
  res
    .type("application/javascript")
    .set("Cache-Control", "no-store")
    .send(
      "window.__STI__=" +
        JSON.stringify({
          licenseKey: LICENSE,
          pgConn: pgConn(),
          dataAdapterUrl: "http://localhost:9615",
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
  res.sendFile(MRT);
});

app.get("/stimulsoft.reports.js", (_req, res) => {
  res.sendFile(path.join(SCRIPTS, "stimulsoft.reports.js"));
});

app.get("/stimulsoft.dashboards.js", (_req, res) => {
  res.sendFile(path.join(SCRIPTS, "stimulsoft.dashboards.js"));
});

app.get("/stimulsoft.viewer.js", (_req, res) => {
  res.sendFile(path.join(SCRIPTS, "stimulsoft.viewer.js"));
});

app.get("/stimulsoft.designer.js", (_req, res) => {
  res.sendFile(path.join(SCRIPTS, "stimulsoft.designer.js"));
});

app.get("/stimulsoft.blockly.editor.js", (_req, res) => {
  res.sendFile(path.join(SCRIPTS, "stimulsoft.blockly.editor.js"));
});

app.listen(port, "0.0.0.0", () => {
  console.log("Server running on port " + port);
  console.log("Navigate to http://localhost:" + port + "/viewer to show Viewer");
  console.log("Navigate to http://localhost:" + port + "/designer to show Designer");
  console.log("MRT: " + MRT);
  console.log("Data adapter must be running: npm run adapter  (port 9615)");
});
