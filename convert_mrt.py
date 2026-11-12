#!/usr/bin/env python3
"""Приводит выражения .mrt к виду JS-парсера Stimulsoft.

Format(значение, \"N2\")  ->  Format(\"{0:N2}\", значение)

Исходный файл не меняется — рядом пишется _converted.mrt

Запуск:
  .venv/bin/python convert_mrt.py test.mrt
  MRT_PATH=dashboard.mrt .venv/bin/python convert_mrt.py
  # потом: MRT_PATH=_converted.mrt .venv/bin/python view_dashboard.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_STRING_LIT = re.compile(r'^"(?:[^"\\]|\\.)*"$|^\'(?:[^\'\\]|\\.)*\'$')
_FORMAT_CALL = re.compile(r"\bFormat\s*\(")

changes = 0
samples: list[str] = []


def skip_string(s: str, i: int) -> int:
    q = s[i]
    i += 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == q:
            return i
        i += 1
    return len(s) - 1


def parse_args(s: str, open_idx: int) -> tuple[list[str], int] | None:
    """Аргументы Format(...); open_idx — индекс '('."""
    args: list[str] = []
    depth = 0
    start = open_idx + 1
    i = open_idx + 1
    while i < len(s):
        c = s[i]
        if c in "\"'":
            i = skip_string(s, i)
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                if c != ")":
                    return None
                args.append(s[start:i])
                return args, i
            depth -= 1
        elif c == "," and depth == 0:
            args.append(s[start:i])
            start = i + 1
        i += 1
    return None


def is_string_literal(a: str) -> bool:
    return bool(_STRING_LIT.match(a))


def convert(s: str) -> str:
    global changes
    out: list[str] = []
    pos = 0
    for m in _FORMAT_CALL.finditer(s):
        if m.start() < pos:
            continue
        open_idx = m.end() - 1
        parsed = parse_args(s, open_idx)
        if not parsed:
            continue
        args_raw, end = parsed
        args = [convert(a) for a in args_raw]
        repl: str | None = None
        if len(args) == 2:
            a0, a1 = args[0].strip(), args[1].strip()
            if not is_string_literal(a0) and is_string_literal(a1) and "{" not in a1:
                fmt = a1[1:-1]
                repl = f'Format("{{0:{fmt}}}", {a0})'
        if repl is None:
            repl = s[m.start() : open_idx + 1] + ",".join(args) + ")"
        before = s[m.start() : end + 1]
        if repl != before:
            changes += 1
            if len(samples) < 8:
                samples.append(f"{before}\n   -> {repl}")
        out.append(s[pos : m.start()])
        out.append(repl)
        pos = end + 1
    out.append(s[pos:])
    return "".join(out)


def fix(v: str) -> str:
    if len(v) < 100_000 and "Format" in v:
        return convert(v)
    return v


def walk(node: object) -> None:
    if isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                node[i] = fix(v)
            else:
                walk(v)
        return
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, str):
                node[k] = v if k == "Image" else fix(v)
            else:
                walk(v)


def resolve_input() -> Path:
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        p = Path(sys.argv[1])
    else:
        raw = os.environ.get("MRT_PATH") or os.environ.get("REPORT_FILE") or "test.mrt"
        p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def main() -> None:
    inp = resolve_input()
    if not inp.is_file():
        raise SystemExit(f"Нет файла: {inp}")

    if len(sys.argv) > 2:
        out = Path(sys.argv[2])
        out = out if out.is_absolute() else ROOT / out
    else:
        out = inp.with_name("_converted.mrt")

    mrt = json.loads(inp.read_text(encoding="utf-8-sig"))
    walk(mrt)
    out.write_text(json.dumps(mrt, ensure_ascii=False), encoding="utf-8")
    print(f"Заменено вызовов Format: {changes}")
    for s in samples:
        print(" ", s)
    print(f"\nГотово: {out.resolve()}")


if __name__ == "__main__":
    main()
