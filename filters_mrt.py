#!/usr/bin/env python3
"""Фильтры до генерации дашборда: Dictionary.Variables с Category «Фильтр(ы)».

Два вида выбора значений:
  - items  — Keys/Values (или Items) заданы в DialogInfo;
  - query  — KeysColumn/ValuesColumn/CheckedColumn → DataSource.SqlCommand
             (+ опционально выполнить SQL).

CheckedColumn Stimulsoft: `Source;Expression`, напр.
  payment_types;payment_types.payment_code==\"0000000\"
→ choices[].checked + value (если Variable.Value пустой).

Публичный ответ /filters: у каждого фильтра только alias, choices, name, value.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from extract_mrt import dialog_items, numbered, query_of, string_list, text

FILTER_CATEGORIES = {"фильтр", "фильтры", "filter", "filters"}


def _flag_true(value) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}:
        return True
    return False


def _is_filter_category(category: str | None, raw: dict) -> bool:
    if category and category.strip().lower() in FILTER_CATEGORIES:
        return True
    # обрезанный .mrt: Category есть, значение пустое — всё равно переменные «Фильтры»
    if "Category" in raw and (
        "RequestFromUser" in raw
        or isinstance(raw.get("DialogInfo"), dict)
        or "AllowUseAsSqlParameter" in raw
    ):
        return True
    return False


def _jsonable(v):
    """datetime/Decimal/UUID → JSON-friendly."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    try:
        from datetime import date, datetime
        from decimal import Decimal
        from uuid import UUID

        if isinstance(v, (datetime, date)):
            return v.isoformat()
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, UUID):
            return str(v)
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", errors="replace")
    except Exception:
        pass
    return str(v)


def _database_url(explicit: str | None = None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    user = os.environ.get("POSTGRES_USER", "").strip()
    password = os.environ.get("POSTGRES_PASSWORD", "").strip()
    db = os.environ.get("POSTGRES_DB", "").strip()
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("POSTGRES_PORT", "5432").strip() or "5432"
    if user and password and db:
        from urllib.parse import quote_plus

        return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    return None


def _datasources_index(dictionary: dict) -> tuple[dict[str, dict], list[dict]]:
    """name/alias/NameInSource → source; плюс список всех с SQL."""
    by_name: dict[str, dict] = {}
    all_src: list[dict] = []
    for idx, item in enumerate(numbered(dictionary.get("DataSources"))):
        if not isinstance(item, dict):
            continue
        name = text(item.get("Name"))
        alias = text(item.get("Alias"))
        name_in = text(item.get("NameInSource"))
        sql = query_of(item)
        cols = [
            text(c.get("Name")) or text(c.get("NameInSource"))
            for c in numbered(item.get("Columns"))
            if isinstance(c, dict)
        ]
        cols = [c for c in cols if c]
        rec = {
            "index": idx,
            "name": name,
            "alias": alias,
            "name_in_source": name_in,
            "sql": sql,
            "columns": cols,
        }
        all_src.append(rec)
        for key in (name, alias, name_in):
            if key:
                by_name[key.lower()] = rec
    return by_name, all_src


def _resolve_source(
    source_name: str | None,
    by_name: dict[str, dict],
    all_src: list[dict],
) -> dict | None:
    if source_name:
        hit = by_name.get(source_name.lower())
        if hit:
            return hit
        # частичное совпадение: source в начале имени / имя в source
        sn = source_name.lower()
        for key, rec in by_name.items():
            if key.startswith(sn) or sn.startswith(key):
                return rec
    # один источник с SQL — единственный кандидат
    with_sql = [s for s in all_src if s.get("sql")]
    if len(with_sql) == 1:
        return with_sql[0]
    return None


def _column_tail(path: str | None) -> str | None:
    if not path:
        return None
    # CheckedColumn вида "src;src.col==\"x\"" — не колонка
    if ";" in path:
        return None
    if "." in path:
        return path.split(".", 1)[1].strip() or None
    return path.strip() or None


def _source_head(path: str | None) -> str | None:
    if not path:
        return None
    # "payment_types;payment_types.payment_code==\"0000000\"" → payment_types
    if ";" in path:
        head = path.split(";", 1)[0].strip()
        return head or None
    if "." in path:
        return path.split(".", 1)[0].strip() or None
    return None


def _parse_checked_column(raw: str | None) -> dict:
    """Stimulsoft CheckedColumn: `Source;Expression` или имя bool-колонки.

    Пример: payment_types;payment_types.payment_code==\"0000000\"
    → source=payment_types, expression=payment_types.payment_code==\"0000000\"
    """
    text_raw = (raw or "").strip()
    if not text_raw:
        return {}
    if ";" in text_raw:
        source, expr = text_raw.split(";", 1)
        source = source.strip()
        expr = expr.strip()
        out: dict = {"raw": text_raw}
        if source:
            out["source"] = source
        if expr:
            out["expression"] = expr
        return out
    # просто колонка (Source.Col или Col)
    out = {"raw": text_raw, "column": _column_tail(text_raw) or text_raw}
    head = _source_head(text_raw)
    if head and "." in text_raw:
        out["source"] = head
    return out


def _norm_cell(value) -> str:
    if value is None:
        return ""
    return str(_jsonable(value)).strip()


def _row_field(row: dict, path: str | None):
    """Достать поле строки по `Source.Col` / `Col` (case-insensitive)."""
    if not path or not row:
        return None
    tail = path.strip()
    if "." in tail:
        tail = tail.split(".", 1)[1].strip()
    if not tail:
        return None
    keymap = {str(k).lower(): k for k in row.keys()}
    key = keymap.get(tail.lower())
    if key is None:
        return None
    return row.get(key)


def _eval_checked_expression(row: dict, expression: str) -> bool:
    """Простые условия Stimulsoft dialog: col==\"x\", col=='x', col==123, col!=..."""
    import re

    expr = (expression or "").strip()
    if not expr:
        return False
    # убрать внешние {}
    if expr.startswith("{") and expr.endswith("}"):
        expr = expr[1:-1].strip()

    m = re.match(
        r"^([\w.]+)\s*(==|=|!=|<>)\s*(.+)$",
        expr,
        flags=re.UNICODE,
    )
    if not m:
        # fallback: имя колонки → truthy
        val = _row_field(row, expr)
        return _truthy_checked(val) if val is not None else False

    left, op, right = m.group(1), m.group(2), m.group(3).strip()
    # литерал в кавычках — только строковое сравнение (иначе "0000000" ↔ 0)
    quoted = (right.startswith('"') and right.endswith('"')) or (
        right.startswith("'") and right.endswith("'")
    )
    want = right[1:-1] if quoted else right

    got = _row_field(row, left)
    if got is None:
        return False
    got_s = _norm_cell(got)
    want_s = str(want).strip()
    if quoted:
        eq = got_s == want_s
    else:
        try:
            got_n = float(got_s.replace(",", ".")) if got_s else None
            want_n = float(want_s.replace(",", ".")) if want_s else None
        except ValueError:
            got_n = want_n = None
        eq = (
            (got_n == want_n)
            if (got_n is not None and want_n is not None)
            else (got_s == want_s)
        )
    if op in {"==", "="}:
        return eq
    if op in {"!=", "<>"}:
        return not eq
    return False


def _row_is_checked(row: dict, checked: dict) -> bool:
    """Строка отмечена по CheckedColumn (expression или bool-колонка)."""
    if not checked:
        return False
    try:
        expr = checked.get("expression")
        if expr:
            return _eval_checked_expression(row, expr)
        col = checked.get("column")
        if col:
            val = _row_field(row, col)
            return _truthy_checked(val)
    except Exception:
        return False
    return False


def _variable_value(raw: dict):
    """Variable.Value: строка / число / список / {0:..,1:..} — не только text()."""
    v = raw.get("Value")
    if v is None:
        return None
    if isinstance(v, str):
        return text(v)
    if isinstance(v, (int, float, bool)):
        return _jsonable(v)
    if isinstance(v, list):
        out = [_jsonable(x) for x in v if x not in (None, "")]
        return out or None
    if isinstance(v, dict):
        # Stimulsoft numbered list: {"0": "a", "1": "b"}
        try:
            parts = [_jsonable(x) for x in numbered(v) if x not in (None, "")]
            if parts:
                return parts if len(parts) > 1 else parts[0]
        except Exception:
            pass
        return _jsonable(v)
    return _jsonable(v)


def _apply_choice_labels(choices: list[dict], items: list[dict] | None = None) -> list[dict]:
    """Вернуть пропавшие подписи: из items по key, иначе key → value."""
    by_key: dict[str, object] = {}
    if items:
        for it in items:
            k = it.get("key")
            if k is None or k == "":
                continue
            lab = it.get("value")
            if lab not in (None, ""):
                by_key[str(k)] = lab
    for c in choices:
        if c.get("value") not in (None, ""):
            continue
        k = c.get("key")
        if k is None or k == "":
            continue
        if str(k) in by_key:
            c["value"] = by_key[str(k)]
        else:
            c["value"] = k
    return choices


def _run_sql(database_url: str, sql: str) -> list[dict]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            if not cur.description:
                return []
            cols = [d.name for d in cur.description]
            rows = []
            for row in cur.fetchall():
                rows.append({cols[i]: row[i] for i in range(len(cols))})
            return rows


def _truthy_checked(value) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y", "t"}:
        return True
    return False


def _checked_states_list(di: dict) -> list[bool | None]:
    """CheckedStates / Checked из DialogInfo → список флагов по индексу."""
    raw = di.get("CheckedStates")
    if raw is None:
        raw = di.get("Checked")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [True if _truthy_checked(x) else False for x in raw]
    if isinstance(raw, dict):
        out: list[bool | None] = []
        for item in numbered(raw):
            out.append(True if _truthy_checked(item) else False)
        return out
    return []


def _choices_from_rows(
    rows: list[dict],
    key_col: str | None,
    value_col: str | None,
    checked: dict | None = None,
) -> list[dict]:
    out: list[dict] = []
    key_l = (key_col or "").lower()
    val_l = (value_col or "").lower()
    for row in rows:
        # case-insensitive column match
        keymap = {str(k).lower(): k for k in row.keys()}
        kk = keymap.get(key_l) if key_l else None
        vk = keymap.get(val_l) if val_l else None
        key = row.get(kk) if kk else None
        value = row.get(vk) if vk else None
        if key is None and value is None:
            # fallback: first two columns
            vals = list(row.values())
            if not vals:
                continue
            key = vals[0]
            value = vals[1] if len(vals) > 1 else vals[0]
        item = {}
        if key is not None and str(key) != "":
            item["key"] = _jsonable(key)
        if value is not None and str(value) != "":
            item["value"] = _jsonable(value)
        elif item.get("key") not in (None, ""):
            # подпись не нашлась по ValuesColumn — не отдаём голый key без value
            item["value"] = item["key"]
        if checked and _row_is_checked(row, checked):
            item["checked"] = True
        if item:
            out.append(item)
    return out


def _default_from_checked_choices(choices: list[dict]) -> object | None:
    """Ключи с checked=true → дефолт переменной (1 ключ или список)."""
    keys = [c.get("key") for c in choices if c.get("checked") and c.get("key") not in (None, "")]
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    return keys


def _dialog_meta(di: dict, *, source_name: str | None = None) -> dict:
    """Поля DialogInfo для ответа API (включая CheckedColumn)."""
    keys_column = text(di.get("KeysColumn"))
    values_column = text(di.get("ValuesColumn"))
    checked_column = text(di.get("CheckedColumn"))
    init_type = text(di.get("ItemsInitializationType"))
    checked = _parse_checked_column(checked_column)
    dialog = {
        "source": source_name,
        "keys_column": keys_column,
        "values_column": values_column,
        "checked_column": checked_column,
        "items_from": init_type,
    }
    if checked.get("expression"):
        dialog["checked_expression"] = checked["expression"]
    if checked.get("source") and not source_name:
        dialog["source"] = checked["source"]
    if "AllowUserValues" in di:
        dialog["allow_user_values"] = _flag_true(di.get("AllowUserValues"))
    # если ключ CheckedColumn есть в .mrt, но значение пустое — всё равно отметим наличие
    if "CheckedColumn" in di and not checked_column:
        dialog["checked_column"] = None
        dialog["has_checked_column"] = True
    return {k: v for k, v in dialog.items() if v not in (None, "")}


def _parse_filter_variable(
    raw: dict,
    index: int,
    sources_by_name: dict[str, dict],
    all_sources: list[dict],
    *,
    run_queries: bool,
    database_url: str | None,
) -> dict | None:
    if not isinstance(raw, dict):
        return None
    category = text(raw.get("Category"))
    if not _is_filter_category(category, raw):
        return None

    di = raw.get("DialogInfo") if isinstance(raw.get("DialogInfo"), dict) else {}
    items = dialog_items(di) if di else []
    # dialog_items ignores empty strings — on stripped mrt Keys exist but empty;
    # still mark as items-mode if Keys/Values keys present
    has_keys_values = "Keys" in di or "Values" in di or "Items" in di
    has_query_shape = (
        "KeysColumn" in di
        or "ValuesColumn" in di
        or "CheckedColumn" in di
        or "ItemsInitializationType" in di
    )
    keys_column = text(di.get("KeysColumn"))
    values_column = text(di.get("ValuesColumn"))
    checked_column = text(di.get("CheckedColumn"))
    init_type = text(di.get("ItemsInitializationType"))
    source_name = text(
        di.get("DataSourceName") or di.get("DataSource") or di.get("NameInSource")
    ) or _source_head(keys_column) or _source_head(values_column) or _source_head(
        checked_column
    )

    item: dict = {
        "index": index,
        "name": text(raw.get("Name")),
        "alias": text(raw.get("Alias")),
        "category": category or "Фильтры",
        "type": text(raw.get("Type")),
        "value": _variable_value(raw),
        "request_from_user": True if _flag_true(raw.get("RequestFromUser")) or "RequestFromUser" in raw else None,
        "sql_parameter": True if _flag_true(raw.get("AllowUseAsSqlParameter")) else None,
    }

    is_query = bool(
        keys_column
        or values_column
        or checked_column
        or (init_type and init_type.lower() in {"columns", "column"})
        or (has_query_shape and not has_keys_values)
    )
    is_items = bool(items) or (has_keys_values and not is_query)

    # DialogInfo всегда (KeysColumn / ValuesColumn / CheckedColumn / …)
    dialog = _dialog_meta(di, source_name=source_name)
    if dialog:
        item["dialog"] = dialog

    if is_items and not is_query:
        item["choices_from"] = "items"
        checks = _checked_states_list(di)
        if items:
            # дополнить checked из CheckedStates, если dialog_items не проставил
            for i, ch in enumerate(items):
                if i < len(checks) and checks[i] and "checked" not in ch:
                    ch["checked"] = True
            item["choices"] = items
            if item.get("value") in (None, ""):
                default = _default_from_checked_choices(items)
                if default is not None:
                    item["value"] = default
                    item["value_from"] = "checked_states"
        else:
            # структура Keys/Values есть, значения в .mrt обрезаны
            keys = string_list(di.get("Keys"))
            values = string_list(di.get("Values"))
            item["choices_count"] = max(len(keys), len(values), len(checks))
            item["choices"] = []
            if checks:
                item["dialog"] = {
                    **(item.get("dialog") or {}),
                    "checked_states_count": len(checks),
                    "checked_count": sum(1 for c in checks if c),
                }
    elif is_query or source_name:
        item["choices_from"] = "query"

        src = _resolve_source(source_name, sources_by_name, all_sources)
        if src and not source_name and src.get("name"):
            item.setdefault("dialog", {})["source"] = src.get("name")
            item["dialog"] = {k: v for k, v in (item.get("dialog") or {}).items() if v not in (None, "")}
        sql = (src or {}).get("sql") if src else None
        if sql:
            item["sql"] = sql
        if src and src.get("columns"):
            item["source_columns"] = src["columns"]

        checked = _parse_checked_column(checked_column)
        if checked.get("source") and not (item.get("dialog") or {}).get("source"):
            item.setdefault("dialog", {})["source"] = checked["source"]
            item["dialog"] = {
                k: v for k, v in (item.get("dialog") or {}).items() if v not in (None, "")
            }

        if run_queries and sql and database_url:
            try:
                rows = _run_sql(database_url, sql)
                key_tail = _column_tail(keys_column)
                val_tail = _column_tail(values_column)
                choices = _choices_from_rows(rows, key_tail, val_tail, checked)
                # подтянуть подписи из Keys/Values, если SQL-колонка не совпала
                choices = _apply_choice_labels(choices, items if items else None)
                item["choices"] = choices
                item["query_ok"] = True
                item["query_rows"] = len(rows)
                checked_n = sum(1 for c in choices if c.get("checked"))
                if checked_n:
                    item["checked_count"] = checked_n
                # Value пустой → дефолт из CheckedColumn expression / bool-колонки
                if item.get("value") in (None, ""):
                    default = _default_from_checked_choices(choices)
                    if default is not None:
                        item["value"] = default
                        item["value_from"] = "checked_column"
            except Exception as exc:
                item["query_ok"] = False
                item["query_error"] = str(exc)
                # не теряем Keys/Values из .mrt, если SQL упал
                if items:
                    item["choices"] = items
                    item["choices_fallback"] = "items"
                else:
                    item["choices"] = []
        else:
            if items:
                item["choices"] = items
                item["choices_fallback"] = "items"
            else:
                item["choices"] = []
            if not sql:
                item["query_ok"] = False
                item["query_error"] = "sql/source не найдены в Dictionary.DataSources"
            elif not database_url:
                item["query_ok"] = False
                item["query_error"] = "нет DATABASE_URL / POSTGRES_*"
            elif not run_queries:
                item["query_ok"] = False
                item["query_error"] = "run_queries выключен"
            # без SQL всё равно отдадим разобранное условие
            if checked.get("expression"):
                item.setdefault("dialog", {})["checked_expression"] = checked["expression"]
            # дефолт из CheckedStates / items, если Value пуст
            if item.get("value") in (None, "") and item.get("choices"):
                default = _default_from_checked_choices(item["choices"])
                if default is not None:
                    item["value"] = default
                    item["value_from"] = "checked_states"
    else:
        # Category Фильтры, но без списка и без колонок — всё равно вернём переменную
        item["choices_from"] = "none"
        item["choices"] = []

    return {k: v for k, v in item.items() if v not in (None, "")}


def extract_filters(
    mrt_path: Path | str,
    *,
    run_queries: bool = True,
    database_url: str | None = None,
) -> dict:
    mrt_path = Path(mrt_path)
    data = json.loads(mrt_path.read_text(encoding="utf-8-sig"))
    dictionary = data.get("Dictionary") or {}
    sources_by_name, all_sources = _datasources_index(
        dictionary if isinstance(dictionary, dict) else {}
    )
    db_url = _database_url(database_url)

    filters: list[dict] = []
    nameless = 0
    raw_vars = dictionary.get("Variables") if isinstance(dictionary, dict) else None
    for idx, raw in enumerate(numbered(raw_vars)):
        parsed = _parse_filter_variable(
            raw,
            idx,
            sources_by_name,
            all_sources,
            run_queries=run_queries,
            database_url=db_url,
        )
        if not parsed:
            continue
        # без Name фильтр бесполезен для API — пропускаем
        if not parsed.get("name"):
            nameless += 1
            continue
        filters.append(_public_filter(parsed))

    out: dict = {
        "mrt": mrt_path.name,
        "count": len(filters),
        "filters": filters,
    }
    if nameless and not filters:
        out["mrt_stripped"] = True
        out["error"] = (
            f"В `{mrt_path.name}` у переменных Category «Фильтры» пустые Name/Keys/SQL "
            f"({nameless} шт.). Нужен полный .mrt — задай MRT_PATH на файл с именами."
        )
    return out


def _public_choice(ch: dict) -> dict:
    """Один пункт choices: key + value (+ checked если есть)."""
    out: dict = {}
    if "key" in ch and ch["key"] not in (None, ""):
        out["key"] = ch["key"]
    if "value" in ch and ch["value"] not in (None, ""):
        out["value"] = ch["value"]
    elif "key" in out:
        out["value"] = out["key"]
    if ch.get("checked"):
        out["checked"] = True
    return out


def _public_filter(parsed: dict) -> dict:
    """Только alias, choices, name, value."""
    choices = parsed.get("choices") or []
    return {
        "alias": parsed.get("alias") or parsed.get("name"),
        "choices": [_public_choice(c) for c in choices if isinstance(c, dict)],
        "name": parsed.get("name"),
        "value": parsed.get("value"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract Category=Фильтры variables from .mrt")
    ap.add_argument("mrt", type=Path, nargs="?", default=None)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument(
        "--no-run-queries",
        action="store_true",
        help="Не выполнять SQL (по умолчанию SQL считается)",
    )
    args = ap.parse_args()
    mrt = args.mrt or Path(os.environ.get("MRT_PATH") or "test.mrt")
    if not mrt.is_file():
        raise SystemExit(f"нет файла: {mrt}")
    plan = extract_filters(mrt, run_queries=not args.no_run_queries)
    text_out = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text_out, encoding="utf-8")
        print(f"wrote {args.output} filters={plan['count']}", file=sys.stderr)
    else:
        sys.stdout.write(text_out)


if __name__ == "__main__":
    main()
