#!/usr/bin/env python3
"""Фильтры до генерации дашборда: Dictionary.Variables с Category «Фильтр(ы)».

Два вида выбора значений:
  - items  — Keys/Values (или Items) заданы в DialogInfo;
  - query  — KeysColumn/ValuesColumn → DataSource.SqlCommand (+ опционально выполнить SQL).
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


def _datasources_by_name(dictionary: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in numbered(dictionary.get("DataSources")):
        if not isinstance(item, dict):
            continue
        name = text(item.get("Name")) or text(item.get("Alias"))
        if not name:
            # обрезанный .mrt — индекс как ключ не поможет для KeysColumn; всё равно сохраним sql
            continue
        out[name.lower()] = {
            "name": name,
            "alias": text(item.get("Alias")),
            "sql": query_of(item),
            "columns": [
                text(c.get("Name")) or text(c.get("NameInSource"))
                for c in numbered(item.get("Columns"))
                if isinstance(c, dict)
            ],
        }
    return out


def _column_tail(path: str | None) -> str | None:
    if not path:
        return None
    if "." in path:
        return path.split(".", 1)[1].strip() or None
    return path.strip() or None


def _source_head(path: str | None) -> str | None:
    if not path:
        return None
    if "." in path:
        return path.split(".", 1)[0].strip() or None
    return None


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


def _choices_from_rows(
    rows: list[dict],
    key_col: str | None,
    value_col: str | None,
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
            item["key"] = key if not isinstance(key, (dict, list)) else str(key)
        if value is not None and str(value) != "":
            item["value"] = value if not isinstance(value, (dict, list)) else str(value)
        if item:
            out.append(item)
    return out


def _parse_filter_variable(
    raw: dict,
    index: int,
    sources: dict[str, dict],
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
        "KeysColumn" in di or "ValuesColumn" in di or "ItemsInitializationType" in di
    )
    keys_column = text(di.get("KeysColumn"))
    values_column = text(di.get("ValuesColumn"))
    init_type = text(di.get("ItemsInitializationType"))
    source_name = text(
        di.get("DataSourceName") or di.get("DataSource") or di.get("NameInSource")
    ) or _source_head(keys_column) or _source_head(values_column)

    item: dict = {
        "index": index,
        "name": text(raw.get("Name")),
        "alias": text(raw.get("Alias")),
        "category": category or "Фильтры",
        "type": text(raw.get("Type")),
        "value": text(raw.get("Value")),
        "request_from_user": True if _flag_true(raw.get("RequestFromUser")) or "RequestFromUser" in raw else None,
        "sql_parameter": True if _flag_true(raw.get("AllowUseAsSqlParameter")) else None,
    }

    is_query = bool(
        keys_column
        or values_column
        or (init_type and init_type.lower() in {"columns", "column"})
        or (has_query_shape and not has_keys_values)
    )
    is_items = bool(items) or (has_keys_values and not is_query)

    if is_items and not is_query:
        item["choices_from"] = "items"
        if items:
            item["choices"] = items
        else:
            # структура Keys/Values есть, значения в .mrt обрезаны
            keys = string_list(di.get("Keys"))
            values = string_list(di.get("Values"))
            item["choices_count"] = max(len(keys), len(values))
            item["choices"] = []
    elif is_query or source_name:
        item["choices_from"] = "query"
        item["dialog"] = {
            "source": source_name,
            "keys_column": keys_column,
            "values_column": values_column,
            "items_from": init_type,
        }
        item["dialog"] = {k: v for k, v in item["dialog"].items() if v}

        src = sources.get((source_name or "").lower()) if source_name else None
        sql = (src or {}).get("sql") if src else None
        if sql:
            item["sql"] = sql
        if src and src.get("columns"):
            item["source_columns"] = src["columns"]

        if run_queries and sql and database_url:
            try:
                rows = _run_sql(database_url, sql)
                key_tail = _column_tail(keys_column)
                val_tail = _column_tail(values_column)
                item["choices"] = _choices_from_rows(rows, key_tail, val_tail)
                item["query_ok"] = True
                item["query_rows"] = len(rows)
            except Exception as exc:
                item["query_ok"] = False
                item["query_error"] = str(exc)
                item["choices"] = []
        else:
            item["choices"] = []
            if not sql:
                item["query_ok"] = False
                item["query_error"] = "sql/source не найдены в Dictionary.DataSources"
    else:
        # Category Фильтры, но без списка и без колонок — всё равно вернём переменную
        item["choices_from"] = "none"
        item["choices"] = []

    return {k: v for k, v in item.items() if v not in (None, "")}


def extract_filters(
    mrt_path: Path | str,
    *,
    run_queries: bool = False,
    database_url: str | None = None,
) -> dict:
    mrt_path = Path(mrt_path)
    data = json.loads(mrt_path.read_text(encoding="utf-8-sig"))
    dictionary = data.get("Dictionary") or {}
    sources = _datasources_by_name(dictionary if isinstance(dictionary, dict) else {})
    db_url = database_url or os.environ.get("DATABASE_URL", "").strip() or None

    filters: list[dict] = []
    raw_vars = dictionary.get("Variables") if isinstance(dictionary, dict) else None
    for idx, raw in enumerate(numbered(raw_vars)):
        parsed = _parse_filter_variable(
            raw,
            idx,
            sources,
            run_queries=run_queries,
            database_url=db_url,
        )
        if parsed:
            filters.append(parsed)

    return {
        "mrt": mrt_path.name,
        "kind": "category_filters",
        "description": "Variables с Category «Фильтр(ы)»: items или результат SQL dialog.source",
        "count": len(filters),
        "filters": filters,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract Category=Фильтры variables from .mrt")
    ap.add_argument("mrt", type=Path, nargs="?", default=None)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument(
        "--run-queries",
        action="store_true",
        help="Выполнить SQL dialog.source (нужен DATABASE_URL)",
    )
    args = ap.parse_args()
    mrt = args.mrt or Path(os.environ.get("MRT_PATH") or "test.mrt")
    if not mrt.is_file():
        raise SystemExit(f"нет файла: {mrt}")
    plan = extract_filters(mrt, run_queries=args.run_queries)
    text_out = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text_out, encoding="utf-8")
        print(f"wrote {args.output} filters={plan['count']}", file=sys.stderr)
    else:
        sys.stdout.write(text_out)


if __name__ == "__main__":
    main()
