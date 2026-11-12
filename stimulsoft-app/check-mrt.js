// Проверка: какие типы элементов (Ident) из mrt НЕ известны установленной версии Stimulsoft.
// Запуск: node check-mrt.js ./reports/Report.mrt
const fs = require('fs');
const path = require('path');

const file = process.argv[2] || process.env.REPORT_FILE || './reports/Report.mrt';
const dir = path.join(__dirname, 'node_modules', 'stimulsoft-dashboards-js');
const pkg = require(path.join(dir, 'package.json'));

const engine =
  fs.readFileSync(path.join(dir, 'Scripts', 'stimulsoft.reports.engine.js'), 'utf8') +
  fs.readFileSync(path.join(dir, 'Scripts', 'stimulsoft.dashboards.js'), 'utf8');

const mrt = JSON.parse(fs.readFileSync(file, 'utf8'));
console.log('Установлено:       stimulsoft-dashboards-js', pkg.version);
console.log('ReportVersion mrt: ', mrt.ReportVersion);
console.log('EngineVersion mrt: ', mrt.EngineVersion);

const found = new Map(); // ident -> [names]
(function walk(node) {
  if (Array.isArray(node)) return node.forEach(walk);
  if (node && typeof node === 'object') {
    // только реальные элементы страниц (у них есть ClientRectangle); базы и служебные объекты не проверяем
    const isElement = 'ClientRectangle' in node;
    if (isElement && typeof node.Ident === 'string' && node.Ident) {
      if (!found.has(node.Ident)) found.set(node.Ident, []);
      if (node.Name) found.get(node.Ident).push(node.Name);
    }
    Object.values(node).forEach(walk);
  }
})(mrt);

const unknown = [...found.keys()].filter((id) => !engine.includes(`"${id}"`) && !engine.includes(`'${id}'`));
console.log(`\nВсего типов в mrt: ${found.size}`);
if (!unknown.length) {
  console.log('Неизвестных этой версии типов нет — причина в другом (данные/SQL/параметры).');
} else {
  console.log('\n!!! Неизвестны установленной версии (из-за них превью может быть пустым):');
  unknown.forEach((id) => console.log(`  ${id}  x${found.get(id).length}  например: ${found.get(id).slice(0, 3).join(', ')}`));
}