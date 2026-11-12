// Приводит выражения mrt к виду, который понимает JS-парсер Stimulsoft 2024.x.
// Сейчас: Format(значение, "N2")  ->  Format("{0:N2}", значение)
// Исходный файл НЕ меняется, результат пишется рядом: _converted.mrt
//
// Запуск:  node convert-mrt.js ./reports/Report.mrt
// Потом:   http://127.0.0.1:8040/expr-check?file=_converted.mrt
//          http://127.0.0.1:8040/viewer?file=_converted.mrt
const fs = require('fs');
const path = require('path');

const input = process.argv[2] || process.env.REPORT_FILE || './reports/Report.mrt';
const output = process.argv[3] || path.join(path.dirname(input), '_converted.mrt');

// индекс закрывающей кавычки строкового литерала, начиная с открывающей
function skipString(s, i) {
  const q = s[i];
  for (i++; i < s.length; i++) {
    if (s[i] === '\\') i++;
    else if (s[i] === q) return i;
  }
  return s.length - 1;
}

// делит аргументы на верхнем уровне; возвращает { args, end } (end — индекс закрывающей ')')
function parseArgs(s, open) {
  const args = [];
  let depth = 0, start = open + 1;
  for (let i = open + 1; i < s.length; i++) {
    const c = s[i];
    if (c === '"' || c === "'") i = skipString(s, i);
    else if (c === '(' || c === '[' || c === '{') depth++;
    else if (c === ')' || c === ']' || c === '}') {
      if (depth === 0) {
        if (c !== ')') return null;
        args.push(s.slice(start, i));
        return { args, end: i };
      }
      depth--;
    } else if (c === ',' && depth === 0) {
      args.push(s.slice(start, i));
      start = i + 1;
    }
  }
  return null; // скобки не сошлись — не трогаем
}

const isStringLiteral = (a) => /^"(?:[^"\\]|\\.)*"$/.test(a) || /^'(?:[^'\\]|\\.)*'$/.test(a);
let changes = 0;
const samples = [];

function convert(s) {
  let out = '';
  let pos = 0;
  const re = /\bFormat\s*\(/g;
  let m;
  while ((m = re.exec(s))) {
    const open = m.index + m[0].length - 1;
    const parsed = parseArgs(s, open);
    if (!parsed) continue;
    const args = parsed.args.map((a) => convert(a)); // вложенные Format
    let repl;
    if (args.length === 2) {
      const a0 = args[0].trim();
      const a1 = args[1].trim();
      if (!isStringLiteral(a0) && isStringLiteral(a1) && !a1.includes('{')) {
        const fmt = a1.slice(1, -1);
        repl = `Format("{0:${fmt}}", ${a0})`;
      }
    }
    if (!repl) repl = s.slice(m.index, open + 1) + args.join(',') + ')';
    const before = s.slice(m.index, parsed.end + 1);
    if (repl !== before) {
      changes++;
      if (samples.length < 8) samples.push(`${before}\n   -> ${repl}`);
    }
    out += s.slice(pos, m.index) + repl;
    pos = parsed.end + 1;
    re.lastIndex = pos;
  }
  return out + s.slice(pos);
}

const mrt = JSON.parse(fs.readFileSync(input, 'utf8'));
(function walk(node) {
  if (Array.isArray(node)) return node.forEach((v, i) => { if (typeof v === 'string') node[i] = fix(v); else walk(v); });
  if (node && typeof node === 'object') {
    for (const k of Object.keys(node)) {
      const v = node[k];
      if (typeof v === 'string') node[k] = k === 'Image' ? v : fix(v);
      else walk(v);
    }
  }
})(mrt);

function fix(v) {
  return v.length < 100000 && v.includes('Format') ? convert(v) : v;
}

fs.writeFileSync(output, JSON.stringify(mrt));
console.log(`Заменено вызовов Format: ${changes}`);
samples.forEach((s) => console.log(' ', s));
console.log(`\nГотово: ${output}`);