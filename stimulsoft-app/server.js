require('dotenv').config();
const path = require('path');
const fs = require('fs');
const express = require('express');
const adapter = require('stimulsoft-data-adapter');

const PORT = Number(process.env.PORT || 8040);
const REPORT_FILE = path.resolve(process.env.REPORT_FILE || './reports/Report.mrt');
const LICENSE = process.env.STIMULSOFT_LICENSE_KEY || '';
const PG = process.env.PG_CONNECTION_STRING || '';
const PG_SSL = String(process.env.PG_SSL || '').toLowerCase() === 'true';

const app = express();

const nm = (p) => path.join(__dirname, 'node_modules', p);
app.use('/stimulsoft/scripts', express.static(nm('stimulsoft-dashboards-js/Scripts')));
app.use('/stimulsoft/css', express.static(nm('stimulsoft-dashboards-js/Css')));

function pgConnectionString() {
  if (!PG) return '';
  if (!PG_SSL) return PG;
  return PG + (PG.includes('?') ? '&' : '?') + 'ssl=true';
}

// Data adapter: same-origin /handler (Stimulsoft.Base.StiOptions.WebServer.url)
function handleAdapter(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept');
  res.setHeader('Access-Control-Allow-Methods', 'POST, GET, OPTIONS, DELETE, PUT');
  res.setHeader('Cache-Control', 'no-cache');

  if (req.method === 'OPTIONS') {
    res.status(204).end();
    return;
  }

  const raw = typeof req.body === 'string' ? req.body : (req.body ? JSON.stringify(req.body) : '');
  if (!raw) {
    // probe / empty → supported adapters list
    return res.send(
      adapter.getResponse({
        success: true,
        types: ['PostgreSQL', 'MySQL', 'MS SQL', 'Firebird', 'Oracle', 'MongoDB'],
      })
    );
  }

  let command;
  try {
    command = adapter.getCommand(raw);
  } catch (e) {
    console.error('[adapter] bad command', e.message);
    return res.status(400).send('Bad command: ' + e.message);
  }

  const cs = pgConnectionString();
  if (cs) {
    // MRT с Windows часто без database=PostgreSQL — форсим
    if (!command.database || /postgres/i.test(String(command.database)) || command.database === 'PostgreSQL') {
      command.database = 'PostgreSQL';
      command.connectionString = cs;
    }
  }

  console.log('[adapter]', command.command, command.database || '-', (command.queryString || '').slice(0, 80));
  adapter.process(command, (result) => {
    if (result && result.success === false) console.error('[adapter]', result.notice);
    res.send(adapter.getResponse(result));
  });
}

app.options('/handler', handleAdapter);
app.post('/handler', express.text({ type: '*/*', limit: '20mb' }), handleAdapter);
app.get('/handler', express.text({ type: '*/*', limit: '20mb' }), handleAdapter);

app.get('/mrt', (req, res) => {
  if (!fs.existsSync(REPORT_FILE)) {
    return res.status(404).send('Report file not found: ' + REPORT_FILE);
  }
  res.setHeader('Cache-Control', 'no-cache');
  res.sendFile(REPORT_FILE);
});

app.post('/mrt', express.text({ type: '*/*', limit: '50mb' }), (req, res) => {
  fs.writeFileSync(REPORT_FILE, req.body);
  res.json({ ok: true });
});

const head = (scripts) => `<!doctype html>
<html><head><meta charset="utf-8"><title>Stimulsoft</title>
<style>html,body{margin:0;height:100%}#err{position:fixed;top:0;left:0;right:0;background:#fee;color:#900;
font:13px monospace;white-space:pre-wrap;z-index:99999;padding:8px}</style>
${scripts.map((s) => `<script src="/stimulsoft/scripts/${s}"></script>`).join('\n')}
<script>
  function showErr(m){
    var d=document.getElementById('err')||document.body.appendChild(Object.assign(document.createElement('div'),{id:'err'}));
    d.textContent+=m+'\\n';
  }
  window.addEventListener('error',function(e){showErr('JS error: '+e.message);});
  window.addEventListener('unhandledrejection',function(e){showErr('Promise: '+(e.reason&&e.reason.message||e.reason));});

  // URL data adapter — обязательно ДО loadFile / SQL
  function setAdapterUrl() {
    var url = window.location.origin + '/handler';
    if (window.Stimulsoft && Stimulsoft.Base && Stimulsoft.Base.StiOptions && Stimulsoft.Base.StiOptions.WebServer) {
      Stimulsoft.Base.StiOptions.WebServer.url = url;
    }
    // alias из доки Stimulsoft
    if (typeof StiOptions !== 'undefined' && StiOptions.WebServer) {
      StiOptions.WebServer.url = url;
    }
    console.log('data adapter url =', url);
    return url;
  }

  if (${JSON.stringify(LICENSE)}) {
    Stimulsoft.Base.StiLicense.key = ${JSON.stringify(LICENSE)};
  }
  setAdapterUrl();
</script>
</head><body>`;

app.get('/viewer', (req, res) => {
  res.send(`${head(['stimulsoft.reports.pack.js', 'stimulsoft.dashboards.pack.js', 'stimulsoft.viewer.pack.js'])}
<link href="/stimulsoft/css/stimulsoft.viewer.office2013.whiteblue.css" rel="stylesheet">
<div id="viewer"></div>
<script>
  setAdapterUrl();
  var options = new Stimulsoft.Viewer.StiViewerOptions();
  options.appearance.fullScreenMode = true;
  options.toolbar.showSendEmailButton = false;
  var viewer = new Stimulsoft.Viewer.StiViewer(options, 'StiViewer', false);
  var report = Stimulsoft.Report.StiReport.createNewDashboard
    ? Stimulsoft.Report.StiReport.createNewDashboard()
    : new Stimulsoft.Report.StiReport();
  setAdapterUrl();
  report.loadFile('/mrt');
  viewer.report = report;
  viewer.renderHtml('viewer');
</script></body></html>`);
});

app.get('/designer', (req, res) => {
  res.send(`${head(['stimulsoft.reports.pack.js', 'stimulsoft.dashboards.pack.js', 'stimulsoft.designer.pack.js'])}
<link href="/stimulsoft/css/stimulsoft.designer.office2013.whiteblue.css" rel="stylesheet">
<link href="/stimulsoft/css/stimulsoft.viewer.office2013.whiteblue.css" rel="stylesheet">
<div id="designer"></div>
<script>
  setAdapterUrl();
  var options = new Stimulsoft.Designer.StiDesignerOptions();
  options.appearance.fullScreenMode = true;
  var designer = new Stimulsoft.Designer.StiDesigner(options, 'StiDesigner', false);
  var report = Stimulsoft.Report.StiReport.createNewDashboard
    ? Stimulsoft.Report.StiReport.createNewDashboard()
    : new Stimulsoft.Report.StiReport();
  setAdapterUrl();
  report.loadFile('/mrt');
  designer.report = report;
  designer.onSaveReport = function (e) {
    fetch('/mrt', { method: 'POST', body: e.report.saveToJsonString() })
      .then(function () { console.log('saved'); });
  };
  designer.renderHtml('designer');
</script></body></html>`);
});

app.get('/', (req, res) => res.redirect('/viewer'));

app.listen(PORT, () => {
  console.log(`http://127.0.0.1:${PORT}/viewer   |   http://127.0.0.1:${PORT}/designer`);
  console.log(`data adapter: http://127.0.0.1:${PORT}/handler`);
  if (!LICENSE) console.warn('! STIMULSOFT_LICENSE_KEY не задан');
  if (!PG) console.warn('! PG_CONNECTION_STRING не задан — будет использована строка из mrt');
  if (!fs.existsSync(REPORT_FILE)) console.warn('! Нет файла отчёта: ' + REPORT_FILE);
});
