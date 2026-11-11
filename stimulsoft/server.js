"use strict";

const fs = require("fs");
const path = require("path");
const express = require("express");
const adapter = require("stimulsoft-data-adapter");

const ROOT = __dirname;
const SCRIPTS = path.join(ROOT, "node_modules", "stimulsoft-dashboards-js", "Scripts");
const PORT = Number(process.env.PORT || 8040);

function mrtPath() {
  const raw = (process.env.MRT_PATH || "/data/dashboard.mrt").trim();
  return path.isAbsolute(raw) ? raw : path.resolve(ROOT, raw);
}

function pgConn() {
  const user = (process.env.POSTGRES_USER || "").trim();
  const password = (process.env.POSTGRES_PASSWORD || "").trim();
  const db = (process.env.POSTGRES_DB || "").trim();
  const host = (process.env.POSTGRES_HOST || "postgres").trim() || "postgres";
  const port = (process.env.POSTGRES_PORT || "5432").trim() || "5432";
  if (!user || !password || !db) {
    throw new Error("Need POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB");
  }
  return `Host=${host}; Port=${port}; Database=${db}; User Id=${user}; Password=${password};`;
}

const MRT = mrtPath();
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
        JSON.stringify({
          licenseKey: LICENSE,
          pgConn: PG,
          dataAdapterUrl: "/data",
        }) +
        ";\n"
    );
});

app.get("/mrt", (_req, res) => {
  if (!fs.existsSync(MRT)) {
    return res.status(404).type("text/plain").send("MRT not found: " + MRT);
  }
  res.type("application/json").set("Cache-Control", "no-store").send(fs.readFileSync(MRT, "utf8"));
});

app.post("/mrt", (req, res) => {
  try {
    const body = typeof req.body === "string" ? req.body : JSON.stringify(req.body);
    fs.writeFileSync(MRT, JSON.stringify(JSON.parse(body), null, 2) + "\n", "utf8");
    res.json({ success: true });
  } catch (err) {
    res.status(400).json({ success: false, notice: String(err.message || err) });
  }
});

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
  res.status(204).end();
});

app.post("/data", (req, res) => {
  res.set({
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Origin, X-Requested-With, Content-Type, Accept",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Cache-Control": "no-store",
  });
  const raw = typeof req.body === "string" ? req.body : JSON.stringify(req.body || {});
  try {
    const command = forcePg(adapter.getCommand(raw));
    adapter.process(command, (result) => {
      res.type("application/json").send(adapter.getResponse(result));
    });
  } catch (err) {
    res.status(500).json({ success: false, notice: String(err.message || err) });
  }
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

app.use(express.static(path.join(ROOT, "public")));
app.get(["/", "/designer"], (_req, res) => {
  res.sendFile(path.join(ROOT, "public", "designer.html"));
});

app.listen(PORT, "0.0.0.0", () => {
  console.log("Stimulsoft Dashboards.JS 2024.3.3");
  console.log("MRT:", MRT);
  console.log("http://0.0.0.0:" + PORT + "/");
});
