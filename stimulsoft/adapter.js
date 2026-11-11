// Official sample:
// https://github.com/stimulsoft/Samples-Dashboards.JS-for-Node.js/tree/master/Starting%20SQL%20adapters%20from%20the%20HTTP%20server
// stimulsoft-data-adapter 2024.3.3 — PostgreSQL через pg
var http = require("http");
var adapter = require("stimulsoft-data-adapter");

function accept(request, response) {
  response.setHeader("Access-Control-Allow-Origin", "*");
  response.setHeader("Access-Control-Allow-Headers", "Origin, X-Requested-With, Content-Type, Accept");
  response.setHeader("Access-Control-Allow-Methods", "POST, GET, OPTIONS, DELETE, PUT");
  response.setHeader("Cache-Control", "no-cache");

  if (request.method === "OPTIONS") {
    response.writeHead(204);
    response.end();
    return;
  }

  // GET / probe (браузер, healthcheck) — пустое тело, getCommand падает
  if (request.method === "GET" || request.method === "HEAD") {
    response.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("Stimulsoft Data Adapter OK");
    return;
  }

  var data = "";
  request.on("data", function (buffer) {
    data += buffer;
  });

  request.on("end", function () {
    if (!data || !String(data).trim()) {
      response.writeHead(400, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ success: false, notice: "Empty adapter command" }));
      return;
    }
    try {
      var command = adapter.getCommand(data);
      adapter.process(command, function (result) {
        var responseData = adapter.getResponse(result);
        response.end(responseData);
      });
    } catch (err) {
      console.error("[adapter]", err && err.message ? err.message : err);
      response.writeHead(400, { "Content-Type": "application/json" });
      response.end(
        JSON.stringify({
          success: false,
          notice: String(err && err.message ? err.message : err),
        })
      );
    }
  });
}

console.log("The DataAdapter run on port 9615");
console.log("To use, on the client side, you need to specify the URL of this host that handles requests:");
console.log('StiOptions.WebServer.url = "http://localhost:9615"');
http.createServer(accept).listen(9615);
