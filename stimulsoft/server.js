"use strict";

// Express + designer (офиц. sample) + stimulsoft-data-adapter (Postgres)
const fs = require("fs");
const path = require("path");
const express = require("express");
const adapter = require("stimulsoft-data-adapter");

const ROOT = __dirname;

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

const SCRIPTS = path.join(ROOT, "node_modules", "stimulsoft-dashboards-js", "Scripts");
const PORT = Number(process.env.PORT || process.env.DESIGNER_PORT || 8040);
const MRT = process.env.MRT_PATH || path.join(ROOT, "dashboard.mrt");

function pgConn() {
  const user = process.env.POSTGRES_USER || "";
  const password = process.env.POSTGRES_PASSWORD || "";
  const db = process.env.POSTGRES_DB || "";
  const host = process.env.POSTGRES_HOST || "127.0.0.1";
  const port = process.env.POSTGRES_PORT || "5432";
  if (!user || !password || !db) {
    throw new Error("Need POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB");
  }
  return `Host=${host}; Port=${port}; Database=${db}; User Id=${user}; Password=${password};`;
}

const PG = pgConn();
const LICENSE =
  (process.env.STIMULSOFT_DASHBOARDS_LICENSE_KEY || "").trim() ||
  (process.env.STIMULSOFT_REPORTS_LICENSE_KEY || "").trim();

const app = express();
app.use(express.text({ type: "*/*", limit: "50mb" }));

app.get("/config.js", (_req, res) => {
  res
    .type("application/javascript")
    .set("Cache-Control", "no-store")
    .send(
      "window.__STI__=" +
        JSON.stringify({ licenseKey: LICENSE, pgConn: PG }) +
        ";\n"
    );
});

app.get("/dashboard", (_req, res) => {
  if (!fs.existsSync(MRT)) {
    return res.status(404).send("MRT not found: " + MRT);
  }
  res.type("application/json").set("Cache-Control", "no-store").send(fs.readFileSync(MRT));
});

app.post("/dashboard", (req, res) => {
  try {
    const body = typeof req.body === "string" ? req.body : JSON.stringify(req.body);
    fs.writeFileSync(MRT, JSON.stringify(JSON.parse(body), null, 2) + "\n");
    res.json({ success: true });
  } catch (err) {
    res.status(400).json({ success: false, notice: String(err.message || err) });
  }
});

// https://www.stimulsoft.com/en/samples/dashboards-js/nodejs/starting-sql-adapters-from-the-http-server
function forcePg(command) {
  if (!command || typeof command !== "object") return command;
  const db = String(command.database || "").toLowerCase();
  if (db.includes("postgres") || db.includes("npgsql")) {
    command.connectionString = PG;
  }
  return command;
}

app.options("/data", (_req, res) => {
  res.set({
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Origin, X-Requested-With, Content-Type, Accept",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
  });
  res.sendStatus(204);
});

app.post("/data", (req, res) => {
  res.set({
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Origin, X-Requested-With, Content-Type, Accept",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Cache-Control": "no-cache",
  });
  const raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
  const command = forcePg(adapter.getCommand(raw));
  adapter.process(command, (result) => {
    res.type("application/json").send(adapter.getResponse(result));
  });
});

for (const name of [
  "stimulsoft.reports.js",
  "stimulsoft.dashboards.js",
  "stimulsoft.viewer.js",
  "stimulsoft.designer.js",
  "stimulsoft.blockly.editor.js",
]) {
  app.get("/" + name, (_req, res) => res.sendFile(path.join(SCRIPTS, name)));
}

app.get(["/", "/designer"], (_req, res) => {
  res.sendFile(path.join(ROOT, "designer.html"));
});

app.listen(PORT, "0.0.0.0", () => {
  console.log("Dashboards.JS 2024.3.3 designer + Postgres adapter");
  console.log("MRT:", MRT);
  console.log("http://0.0.0.0:" + PORT + "/designer");
  console.log('StiOptions.WebServer.url = "' + "http://localhost:" + PORT + '/data"');
});
