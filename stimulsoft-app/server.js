require('dotenv').config();
const path = require('path');
const fs = require('fs');
const express = require('express');
const adapter = require('stimulsoft-data-adapter');

const PORT = process.env.PORT || 8040;
const REPORT_FILE = path.resolve(process.env.REPORT_FILE || './reports/Report.mrt');
const LICENSE = process.env.STIMULSOFT_LICENSE_KEY || '';
const PG = process.env.PG_CONNECTION_STRING || '';
const PG_SSL = String(process.env.PG_SSL).toLowerCase() === 'true';

const app = express();

// Скрипты и стили Stimulsoft отдаём из node_modules (версии берутся из package.json)
const nm = (p) => path.join(__dirname, 'node_modules', p);
app.use('/stimulsoft/scripts', express.static(nm('stimulsoft-dashboards-js/Scripts')));
app.use('/stimulsoft/css', express.static(nm('stimulsoft-dashboards-js/Css')));

// ---------- Data adapter (Postgres) ----------
app.post('/handler', express.text({ type: '*/*', limit: '20mb' }), (req, res) => {
  res.setHeader('Cache-Control', 'no-cache');
  let command;
  try {
    command = adapter.getCommand(req.body);
  } catch (e) {
    return res.status(400).send('Bad command: ' + e.message);
  }

  // Берём подключение из .env, а не из mrt
  if (command.database === 'PostgreSQL' && PG) {
    command.connectionString = PG;
    if (PG_SSL) command.connectionString += (PG.includes('?') ? '&' : '?') + 'ssl=true';
  }

  adapter.process(command, (result) => {
    if (result && result.success === false) console.error('[adapter]', result.notice);
    res.send(adapter.getResponse(result));
  });
});

// ---------- Отчёт ----------
app.get('/mrt', (req, res) => {
  if (!fs.existsSync(REPORT_FILE)) return res.status(404).send('Report file not found: ' + REPORT_FILE);
  res.setHeader('Cache-Control', 'no-cache');
  res.sendFile(REPORT_FILE);
});
// сохранение из дизайнера обратно в файл
app.post('/mrt', express.text({ type: '*/*', limit: '50mb' }), (req, res) => {
  fs.writeFileSync(REPORT_FILE, req.body);
  res.json({ ok: true });
});

// ---------- Страницы ----------
const head = (scripts) => `<!doctype html>
<html><head><meta charset="utf-8"><title>Stimulsoft</title>
<style>html,body{margin:0;height:100%}#err{position:fixed;top:0;left:0;right:0;background:#fee;color:#900;
font:13px monospace;white-space:pre-wrap;z-index:99999}</style>
${scripts.map((s) => `<script src="/stimulsoft/scripts/${s}"></script>`).join('\n')}
<script>
  // Показываем ошибки прямо на странице, чтобы не было «серого экрана»
  function showErr(m){var d=document.getElementById('err')||document.body.appendChild(Object.assign(document.createElement('div'),{id:'err'}));d.textContent+=m+'\\n';}
  window.addEventListener('error',function(e){showErr('JS error: '+e.message);});
  window.addEventListener('unhandledrejection',function(e){showErr('Promise: '+(e.reason&&e.reason.message||e.reason));});
  Stimulsoft.Base.StiLicense.key = ${JSON.stringify(LICENSE)};
  // В 2024.x настройки доступны как глобальный StiOptions
  (window.StiOptions || Stimulsoft.StiOptions || Stimulsoft.Base.StiOptions).WebServer.url = location.origin + '/handler';
  console.log('Stimulsoft engine', Stimulsoft.System && Stimulsoft.System.StiVersion);
</script>
</head><body>`;

app.get('/viewer', (req, res) => {
  res.send(`${head(['stimulsoft.reports.pack.js', 'stimulsoft.dashboards.pack.js', 'stimulsoft.viewer.pack.js'])}
<link href="/stimulsoft/css/stimulsoft.viewer.office2013.whiteblue.css" rel="stylesheet">
<div id="viewer"></div>
<script>
  var options = new Stimulsoft.Viewer.StiViewerOptions();
  options.appearance.fullScreenMode = true;
  options.toolbar.showSendEmailButton = false;
  var viewer = new Stimulsoft.Viewer.StiViewer(options, 'StiViewer', false);
  var report = new Stimulsoft.Report.StiReport();
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
  var options = new Stimulsoft.Designer.StiDesignerOptions();
  options.appearance.fullScreenMode = true;
  var designer = new Stimulsoft.Designer.StiDesigner(options, 'StiDesigner', false);
  var report = new Stimulsoft.Report.StiReport();
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
  if (!LICENSE) console.warn('! STIMULSOFT_LICENSE_KEY не задан');
  if (!PG) console.warn('! PG_CONNECTION_STRING не задан — будет использована строка из mrt');
  if (!fs.existsSync(REPORT_FILE)) console.warn('! Нет файла отчёта: ' + REPORT_FILE);
});
