// Делит mrt на отдельные файлы по страницам, чтобы найти страницу, которая ломает превью.
// Запуск: node split-mrt.js ./reports/Report.mrt
// Потом открывай: http://127.0.0.1:8040/viewer?file=_page0.mrt  (_page1.mrt, ...)
const fs = require('fs');
const path = require('path');

const file = process.argv[2] || process.env.REPORT_FILE || './reports/Report.mrt';
const mrt = JSON.parse(fs.readFileSync(file, 'utf8'));
const keys = Object.keys(mrt.Pages);
console.log(`Страниц: ${keys.length}`);

keys.forEach((k, i) => {
  const part = { ...mrt, Pages: { 0: mrt.Pages[k] } };
  const out = path.join(path.dirname(file), `_page${i}.mrt`);
  fs.writeFileSync(out, JSON.stringify(part));
  const n = Object.keys(mrt.Pages[k].Components || {}).length;
  console.log(`${out}  (элементов верхнего уровня: ${n})  →  /viewer?file=_page${i}.mrt`);
});