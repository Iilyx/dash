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

// Адаптер понимает только схему postgres:// (postgresql:// разбирает неверно) — нормализуем
function pgUrl() {
  let url = PG.replace(/^postgresql:\/\//i, 'postgres://');
  if (PG_SSL && !/[?&]ssl=/.test(url)) url += (url.includes('?') ? '&' : '?') + 'ssl=true';
  return url;
}

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
    command.connectionString = pgUrl();
  }

  const t0 = Date.now();
  adapter.process(command, (result) => {
    const ms = Date.now() - t0;
    if (command.command !== 'GetSupportedAdapters') {
      const sql = String(command.queryString || '').replace(/\s+/g, ' ').slice(0, 160);
      const params = (command.parameters || []).map((p) => p.name + '=' + p.value).join(', ');
      if (result && result.success === false) {
        console.error(`[adapter] ERROR (${ms}ms) ${command.database} ${command.command}: ${result.notice}\n          SQL: ${sql}\n          params: ${params}`);
      } else if (process.env.DEBUG_ADAPTER !== 'false') {
        const rows = result && result.rows ? result.rows.length : '-';
        console.log(`[adapter] ok (${ms}ms) ${command.command} rows=${rows} SQL: ${sql} ${params ? '| ' + params : ''}`);
      }
    }
    res.send(adapter.getResponse(result));
  });
});

// Проверка подключения к Postgres из .env: http://127.0.0.1:8040/dbcheck
app.get('/dbcheck', async (req, res) => {
  if (!PG) return res.status(500).send('PG_CONNECTION_STRING не задан в .env');
  const { Client } = require('pg');
  const client = new Client({ connectionString: pgUrl(), connectionTimeoutMillis: 5000 });
  try {
    await client.connect();
    const r = await client.query('select version() as v, current_database() as db, current_user as u');
    res.json({ ok: true, ...r.rows[0] });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  } finally {
    client.end().catch(() => {});
  }
});

// ---------- Отчёт ----------
// ?file=_page0.mrt — отдать другой файл из той же папки (нужно для split-mrt.js)
const pickFile = (req) =>
  req.query.file ? path.join(path.dirname(REPORT_FILE), path.basename(String(req.query.file))) : REPORT_FILE;

app.get('/mrt', (req, res) => {
  const file = pickFile(req);
  if (!fs.existsSync(file)) return res.status(404).send('Report file not found: ' + file);
  res.setHeader('Cache-Control', 'no-cache');
  res.sendFile(file);
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
  // Включаем внутренние сообщения движка (по умолчанию он молча оставляет пустой экран)
  Stimulsoft.System.StiError.consoleErrorLog = true;
  Stimulsoft.System.StiError.consoleWarningLog = true;
  Stimulsoft.System.StiError.consoleMessageLog = true;
  ['error', 'warn'].forEach(function (lvl) {
    var orig = console[lvl];
    console[lvl] = function () {
      var msg = Array.prototype.slice.call(arguments).join(' ');
      if (!/trial version|Failed to load resource/i.test(msg)) showErr('[' + lvl + '] ' + msg);
      orig.apply(console, arguments);
    };
  });
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
  report.loadFile('/mrt' + location.search);
  viewer.report = report;
  viewer.renderHtml('viewer');
</script></body></html>`);
});

app.get('/designer', (req, res) => {
  res.send(`${head(['stimulsoft.reports.pack.js', 'stimulsoft.dashboards.pack.js', 'stimulsoft.viewer.pack.js', 'stimulsoft.designer.pack.js'])}
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

// Проверка всех выражений из mrt парсером установленной версии: http://127.0.0.1:8040/expr-check
app.get('/expr-check', (req, res) => {
  res.send(`${head(['stimulsoft.reports.pack.js', 'stimulsoft.dashboards.pack.js'])}
<pre id="out" style="padding:12px;font:13px monospace;white-space:pre-wrap">Проверяю...</pre>
<script>
(async function () {
  var out = document.getElementById('out');
  try {
    var text = await (await fetch('/mrt' + location.search)).text();
    var json = JSON.parse(text);
    var report = new Stimulsoft.Report.StiReport();
    report.load(text);
    var comp = report.pages.getByIndex(0);
    var Parser = Stimulsoft.Report.Engine.StiParser.StiParser;

    // верхнеуровневые блоки {...} с учётом строковых литералов (внутри "{0:N2}" скобки не считаются)
    function extract(s) {
      var res = [], i = 0;
      while (i < s.length) {
        if (s[i] === '{') {
          var depth = 0, j = i, q = null;
          for (; j < s.length; j++) {
            var c = s[j];
            if (q) { if (c === '\\\\') j++; else if (c === q) q = null; }
            else if (c === '"' || c === "'") q = c;
            else if (c === '{') depth++;
            else if (c === '}') { depth--; if (depth === 0) break; }
          }
          if (j < s.length) { res.push(s.slice(i + 1, j)); i = j + 1; continue; }
        }
        i++;
      }
      return res;
    }
    // собираем выражения: ключи Expression и блоки {...} в строках
    var items = [];
    (function walk(n, path) {
      if (Array.isArray(n)) return n.forEach(function (v, i) { walk(v, path + '[' + i + ']'); });
      if (n && typeof n === 'object') {
        Object.keys(n).forEach(function (k) {
          var v = n[k], p = path + '/' + k;
          if (typeof v === 'string') {
            if (k === 'Expression' && v.trim()) items.push({ path: p, expr: v });
            else if (k !== 'Image' && v.length < 5000) {
              extract(v).forEach(function (x) { items.push({ path: p, expr: x }); });
            }
          } else walk(v, p);
        });
      }
    })(json, '');

    var bad = [], seen = {};
    items.forEach(function (it) {
      var key = it.expr;
      if (seen[key] !== undefined) { if (seen[key]) bad.push(Object.assign({ err: seen[key] }, it)); return; }
      var err = null;
      try { Parser.parseTextValue('{' + it.expr + '}', comp); }
      catch (e) { err = String(e && e.message || e); }
      seen[key] = err;
      if (err) bad.push(Object.assign({ err: err }, it));
    });

    var isName = function (b) { return /does not exist in the current context/i.test(b.err); };
    var syntax = bad.filter(function (b) { return !isName(b); });
    var names = bad.filter(isName);
    out.textContent = 'Проверено выражений: ' + items.length + ' (уникальных ' + Object.keys(seen).length + ')\\n' +
      'Синтаксических/прочих ошибок парсера: ' + syntax.length + '\\n' +
      'Ошибок вида "имя не найдено" (могут быть просто колонки данных, не загруженные в проверке): ' + names.length + '\\n\\n' +
      (syntax.length ? '=== ОШИБКИ ПАРСЕРА ===\\n' + syntax.map(function (b) {
        return b.err + '\\n  ' + b.path + '\\n  {' + b.expr + '}\\n';
      }).join('\\n') : 'Ошибок парсера не найдено.') +
      (names.length ? '\\n=== ИМЯ НЕ НАЙДЕНО (первые 15) ===\\n' + names.slice(0, 15).map(function (b) {
        return b.err + '  <- {' + b.expr + '}';
      }).join('\\n') : '');
  } catch (e) { out.textContent = 'Сбой проверки: ' + (e && e.stack || e); }
})();
</script></body></html>`);
});

app.get('/', (req, res) => res.redirect('/viewer'));

app.listen(PORT, () => {
  console.log(`http://127.0.0.1:${PORT}/viewer   |   http://127.0.0.1:${PORT}/designer`);
  if (!LICENSE) console.warn('! STIMULSOFT_LICENSE_KEY не задан');
  if (!PG) console.warn('! PG_CONNECTION_STRING не задан — будет использована строка из mrt');
  if (!fs.existsSync(REPORT_FILE)) console.warn('! Нет файла отчёта: ' + REPORT_FILE);
});
