// Запуск официальной связки: Express (viewer/designer) + SQL Data Adapter :9615
const { spawn } = require("child_process");
const path = require("path");

function run(file) {
  const child = spawn(process.execPath, [path.join(__dirname, file)], {
    stdio: "inherit",
    env: process.env,
  });
  child.on("exit", (code) => {
    console.error(file + " exited with code " + code);
    process.exit(code || 1);
  });
  return child;
}

const adapter = run("adapter.js");
const server = run("server.js");

function shutdown() {
  adapter.kill();
  server.kill();
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
