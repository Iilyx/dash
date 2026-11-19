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
            item["key"] = _jsonable(key)
        if value is not None and str(value) != "":
            item["value"] = _jsonable(value)
        if item:
            out.append(item)
    return out


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

        src = _resolve_source(source_name, sources_by_name, all_sources)
        if src and not source_name and src.get("name"):
            item.setdefault("dialog", {})["source"] = src.get("name")
            item["dialog"] = {k: v for k, v in item["dialog"].items() if v}
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
            elif not database_url:
                item["query_ok"] = False
                item["query_error"] = "нет DATABASE_URL / POSTGRES_*"
            elif not run_queries:
                item["query_ok"] = False
                item["query_error"] = "run_queries выключен"
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
        if parsed:
            filters.append(parsed)

    return {
        "mrt": mrt_path.name,
        "kind": "category_filters",
        "description": "Variables с Category «Фильтр(ы)»: items или результат SQL dialog.source",
        "run_queries": run_queries,
        "database": bool(db_url),
        "count": len(filters),
        "filters": filters,
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
