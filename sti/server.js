"use strict";

const fs = require("fs");
const path = require("path");
const express = require("express");
const adapter = require("stimulsoft-data-adapter");

const ROOT = __dirname;
const SCRIPTS = path.join(ROOT, "node_modules", "stimulsoft-dashboards-js", "Scripts");
const PORT = Number(process.env.PORT || process.env.VIEWER_PORT || 8040);

function resolveMrtPath() {
  const raw = (process.env.MRT_PATH || "test.mrt").trim();
  if (!raw) throw new Error("MRT_PATH is empty");
  return path.isAbsolute(raw) ? raw : path.resolve("/workspace", raw);
}

function buildPgConn() {
  const fromUrl = (process.env.DATABASE_URL || "").trim();
  if (fromUrl) {
    try {
      const u = new URL(fromUrl);
      const db = decodeURIComponent(u.pathname.replace(/^\//, "").split("?")[0] || "");
      return (
        `Host=${u.hostname}; Port=${u.port || 5432}; Database=${db}; ` +
        `User Id=${decodeURIComponent(u.username || "")}; ` +
        `Password=${decodeURIComponent(u.password || "")};`
      );
    } catch {
      /* fall through */
    }
  }
  const user = (process.env.POSTGRES_USER || "").trim();
  const password = (process.env.POSTGRES_PASSWORD || "").trim();
  const db = (process.env.POSTGRES_DB || "").trim();
  const host = (process.env.POSTGRES_HOST || "postgres").trim() || "postgres";
  const port = (process.env.POSTGRES_PORT || "5432").trim() || "5432";
  if (!user || !password || !db) {
    throw new Error("Set DATABASE_URL or POSTGRES_USER/PASSWORD/DB");
  }
  return `Host=${host}; Port=${port}; Database=${db}; User Id=${user}; Password=${password};`;
}

const MRT_PATH = resolveMrtPath();
const PG_CONN = buildPgConn();
const LICENSE_KEY =
  (process.env.STIMULSOFT_DASHBOARDS_LICENSE_KEY || "").trim() ||
  (process.env.STIMULSOFT_REPORTS_LICENSE_KEY || "").trim();

const app = express();
app.use(express.text({ type: "*/*", limit: "50mb" }));

app.get("/favicon.ico", (_req, res) => res.status(204).end());

app.get("/config.js", (_req, res) => {
  const body =
    "window.__STI__=" +
    JSON.stringify({
      licenseKey: LICENSE_KEY,
      pgConn: PG_CONN,
      dataAdapterUrl: "/data",
    }) +
    ";\n";
  res.type("application/javascript").set("Cache-Control", "no-store").send(body);
});

app.get("/mrt", (_req, res) => {
  if (!fs.existsSync(MRT_PATH)) {
    return res.status(404).type("text/plain").send(`MRT not found: ${MRT_PATH}`);
  }
  const raw = fs.readFileSync(MRT_PATH, "utf8");
  res.type("application/json").set("Cache-Control", "no-store").send(raw);
});

app.post("/mrt", (req, res) => {
  try {
    const body = typeof req.body === "string" ? req.body : JSON.stringify(req.body);
    const parsed = JSON.parse(body);
    fs.writeFileSync(MRT_PATH, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    res.json({ success: true });
  } catch (err) {
    res.status(400).json({ success: false, notice: String(err && err.message ? err.message : err) });
  }
});

function forcePg(command) {
  if (!command || typeof command !== "object") return command;
  const db = String(command.database || "").toLowerCase();
  if (db.includes("postgres") || db.includes("npgsql") || db === "postgresql") {
    command.connectionString = PG_CONN;
  }
  return command;
}

app.options("/data", (_req, res) => {
  res.set({
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Origin, X-Requested-With, Content-Type, Accept",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Cache-Control": "no-cache",
  });
  res.status(204).end();
});

app.post("/data", (req, res) => {
  res.set({
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Origin, X-Requested-With, Content-Type, Accept",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Cache-Control": "no-cache",
  });
  const raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
  try {
    const command = forcePg(adapter.getCommand(raw));
    adapter.process(command, (result) => {
      res.type("application/json").send(adapter.getResponse(result));
    });
  } catch (err) {
    res
      .status(500)
      .json({ success: false, notice: String(err && err.message ? err.message : err) });
  }
});

const scriptFiles = [
  "stimulsoft.reports.js",
  "stimulsoft.dashboards.js",
  "stimulsoft.viewer.js",
  "stimulsoft.designer.js",
  "stimulsoft.blockly.editor.js",
];

for (const name of scriptFiles) {
  app.get("/" + name, (_req, res) => {
    res.sendFile(path.join(SCRIPTS, name));
  });
}

app.use(express.static(path.join(ROOT, "public")));

app.get(["/", "/designer"], (_req, res) => {
  res.sendFile(path.join(ROOT, "public", "designer.html"));
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Stimulsoft Dashboards.JS 2024.3.3 designer`);
  console.log(`MRT: ${MRT_PATH}`);
  console.log(`URL: http://0.0.0.0:${PORT}/`);
  console.log(`Data adapter: /data`);
});
