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

  var data = "";
  request.on("data", function (buffer) {
    data += buffer;
  });

  request.on("end", function () {
    var command = adapter.getCommand(data);
    adapter.process(command, function (result) {
      var responseData = adapter.getResponse(result);
      response.end(responseData);
    });
  });
}

console.log("The DataAdapter run on port 9615");
console.log("To use, on the client side, you need to specify the URL of this host that handles requests:");
console.log('StiOptions.WebServer.url = "http://localhost:9615"');
http.createServer(accept).listen(9615);
