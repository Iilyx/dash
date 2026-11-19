#!/usr/bin/env python3
"""Фильтры, которые вводятся до генерации дашборда: Dictionary.Variables.

Не ComboBox/ListBox на странице, а параметры отчёта:
  Category=Фильтр / RequestFromUser / AllowUseAsSqlParameter / DialogInfo.
Как в host.py (filter_variables) и extract_mrt.variables.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FILTER_CATEGORIES = {"фильтр", "фильтры", "filter", "filters"}


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return str(v) if v != "" else None


def _flag_true(value) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}:
        return True
    return False


def _flag_false(value) -> bool:
    if value is False or value == 0:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0", "no"}:
        return True
    return False


def _numbered(node):
    if isinstance(node, list):
        return list(node)
    if isinstance(node, dict):
        return [
            node[k]
            for k in sorted(node.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x))
        ]
    return []


def _dialog_info(node: dict | None) -> dict | None:
    if not isinstance(node, dict):
        return None
    keys_column = _text(node.get("KeysColumn"))
    values_column = _text(node.get("ValuesColumn"))
    source_name = _text(
        node.get("DataSourceName") or node.get("DataSource") or node.get("NameInSource")
    )
    if not source_name and keys_column and "." in keys_column:
        source_name = keys_column.split(".", 1)[0].strip() or None
    info = {
        "items_from": _text(node.get("ItemsInitializationType")),
        "keys_column": keys_column,
        "values_column": values_column,
        "checked_column": _text(node.get("CheckedColumn")),
        "filter": _text(node.get("FilterExpression")),
        "binding_values_column": _text(node.get("BindingValuesColumn")),
        "binding_variable": _text(node.get("BindingVariable")),
        "source": source_name,
        "mask": _text(node.get("Mask")),
        "date_time_type": _text(node.get("DateTimeType")),
        "sort_direction": _text(node.get("SortDirection")),
        "sort_field": _text(node.get("SortField")),
    }
    if _flag_true(node.get("AllowUserValues")) is False and "AllowUserValues" in node:
        if _flag_false(node.get("AllowUserValues")):
            info["allow_user_values"] = False
    if _flag_true(node.get("RememberSelection")):
        info["remember_selection"] = True
    if _flag_true(node.get("BindingValue")):
        info["binding_value"] = True
    # сырые items (Values), если заданы вручную
    items_raw = node.get("Keys") or node.get("Values") or node.get("Items")
    items = []
    for it in _numbered(items_raw):
        if isinstance(it, dict):
            lab = _text(it.get("Value") or it.get("Label") or it.get("Key"))
            if lab:
                items.append(lab)
        else:
            t = _text(it)
            if t:
                items.append(t)
    if items:
        info["items"] = items
    cleaned = {k: v for k, v in info.items() if v not in (None, "", [])}
    return cleaned or None


def _parse_variable(raw: dict, index: int) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("Name"))
    alias = _text(raw.get("Alias"))
    category = _text(raw.get("Category"))
    dialog = _dialog_info(raw.get("DialogInfo") if isinstance(raw.get("DialogInfo"), dict) else None)
    # есть ли DialogInfo-узел вообще (даже пустой) — признак переменной с диалогом
    has_dialog_node = isinstance(raw.get("DialogInfo"), dict)

    request_from_user = _flag_true(raw.get("RequestFromUser"))
    sql_parameter = _flag_true(raw.get("AllowUseAsSqlParameter"))
    category_is_filter = bool(category and category.lower() in FILTER_CATEGORIES)
    # в обрезанных .mrt значения пустые, но ключи RequestFromUser / Category / AllowUseAsSqlParameter остаются
    has_rfu_key = "RequestFromUser" in raw
    has_category_key = "Category" in raw
    has_sql_param_key = "AllowUseAsSqlParameter" in raw

    # фильтр до генерации (параметры отчёта), не элементы дашборда
    is_filter = (
        category_is_filter
        or request_from_user
        or sql_parameter
        or (dialog is not None)
        or has_rfu_key
        or has_category_key
        or has_sql_param_key
    )
    if not is_filter:
        return None

    item: dict = {
        "index": index,
        "name": name,
        "alias": alias,
        "category": category,
        "type": _text(raw.get("Type")),
        "value": _text(raw.get("Value")),
        "init_by": _text(raw.get("InitBy")),
        "selection": _text(raw.get("SelectionMode") or raw.get("Selection")),
    }
    if request_from_user:
        item["request_from_user"] = True
    if "ShowOnParametersPanel" in raw:
        item["show_on_parameters_panel"] = not _flag_false(raw.get("ShowOnParametersPanel"))
    elif request_from_user:
        item["show_on_parameters_panel"] = True
    if sql_parameter:
        item["sql_parameter"] = True
    if _flag_true(raw.get("ReadOnly")):
        item["read_only"] = True
    if dialog:
        item["dialog"] = dialog
    elif has_dialog_node:
        item["dialog"] = {}

    return {k: v for k, v in item.items() if v not in (None, "")}


def extract_filters(mrt_path: Path | str) -> dict:
    """Variables, которые вводятся перед генерацией / SQL (не элементы дашборда)."""
    mrt_path = Path(mrt_path)
    data = json.loads(mrt_path.read_text(encoding="utf-8-sig"))
    dictionary = data.get("Dictionary") or {}
    raw_vars = dictionary.get("Variables") if isinstance(dictionary, dict) else None

    filters: list[dict] = []
    for idx, raw in enumerate(_numbered(raw_vars)):
        parsed = _parse_variable(raw, idx)
        if parsed:
            filters.append(parsed)

    return {
        "mrt": mrt_path.name,
        "kind": "report_variables",
        "description": "Фильтры/параметры Dictionary.Variables до генерации дашборда",
        "count": len(filters),
        "filters": filters,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract pre-generation filter variables from .mrt Dictionary"
    )
    ap.add_argument("mrt", type=Path, nargs="?", default=None)
    ap.add_argument("-o", "--output", type=Path, default=None)
    args = ap.parse_args()
    mrt = args.mrt
    if mrt is None:
        import os

        mrt = Path(os.environ.get("MRT_PATH") or "test.mrt")
    if not mrt.is_file():
        raise SystemExit(f"нет файла: {mrt}")
    plan = extract_filters(mrt)
    text = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output} filters={plan['count']}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
