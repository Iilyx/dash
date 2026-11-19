#!/usr/bin/env python3
"""Достать из .mrt элементы-фильтры (ComboBox / ListBox / DatePicker / …)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FILTER_IDENTS = {
    "StiComboBoxElement",
    "StiListBoxElement",
    "StiTreeViewElement",
    "StiTreeViewBoxElement",
    "StiDatePickerElement",
    "StiNumberBoxElement",
}


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return str(v) if v != "" else None


def _infer_type(node: dict) -> str | None:
    ident = _text(node.get("Ident"))
    if ident:
        return ident
    # обрезанный .mrt — только форма
    if "InitialDateRange" in node or "InitialDateSelection" in node or "InitialTimeSelection" in node:
        return "StiDatePickerElement"
    if "KeyMeter" in node and "NameMeter" in node and "SelectionMode" in node:
        return "StiComboBoxElement|StiListBoxElement"
    if "SelectionMode" in node and "ShowAllValue" in node:
        return "StiFilterElement"
    return None


def _is_filter(node: dict) -> bool:
    ident = _text(node.get("Ident"))
    if ident in FILTER_IDENTS:
        return True
    if _infer_type(node) is not None:
        return True
    return False


def _data_filters(node: dict) -> list[dict]:
    out: list[dict] = []
    for key in ("DataFilters", "Filters"):
        raw = node.get(key)
        items = []
        if isinstance(raw, dict):
            items = [raw[k] for k in sorted(raw.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x))]
        elif isinstance(raw, list):
            items = raw
        for item in items:
            if not isinstance(item, dict):
                continue
            row = {
                "path": _text(item.get("Path") or item.get("Column")),
                "condition": _text(item.get("Condition")),
                "value": _text(item.get("Value") or item.get("Value1")),
                "value2": _text(item.get("Value2")),
            }
            row = {k: v for k, v in row.items() if v is not None}
            if row:
                out.append(row)
    return out


def _key_column(node: dict) -> str | None:
    for key in ("KeyDataColumn", "Key", "Keys", "DataColumn", "NameDataColumn"):
        v = node.get(key)
        if isinstance(v, dict):
            return _text(v.get("Expression") or v.get("Label") or v.get("Column") or v.get("Name"))
        t = _text(v)
        if t:
            return t
    return None


def walk_components(
    components,
    page_index: int,
    page_name: str | None,
    out: list[dict],
    path: str = "",
) -> None:
    if isinstance(components, list):
        components = {str(i): c for i, c in enumerate(components)}
    if not isinstance(components, dict):
        return
    for key, node in sorted(
        components.items(),
        key=lambda x: int(x[0]) if str(x[0]).isdigit() else str(x[0]),
    ):
        if not isinstance(node, dict):
            continue
        comp_path = f"{path}.{key}" if path else str(key)
        if _is_filter(node):
            show_all = node.get("ShowAllValue")
            if not isinstance(show_all, bool):
                show_all = None
            item = {
                "page_index": page_index,
                "page_name": page_name,
                "path": comp_path,
                "name": _text(node.get("Name")),
                "alias": _text(node.get("Alias")),
                "type": _infer_type(node),
                "selection_mode": _text(node.get("SelectionMode")),
                "show_all_value": show_all,
                "initial_value": _text(node.get("InitialValue")),
                "key": _key_column(node)
                or _text((node.get("KeyMeter") or {}).get("Expression") if isinstance(node.get("KeyMeter"), dict) else None)
                or _text((node.get("NameMeter") or {}).get("Expression") if isinstance(node.get("NameMeter"), dict) else None),
                "group": _text(node.get("Group")),
                "parent_key": _text(node.get("ParentKey")),
                "data_filters": _data_filters(node),
            }
            out.append({k: v for k, v in item.items() if v not in (None, [], "")})
        nested = node.get("Components")
        if isinstance(nested, (dict, list)):
            walk_components(nested, page_index, page_name, out, comp_path)


def extract_filters(mrt_path: Path | str) -> dict:
    mrt_path = Path(mrt_path)
    data = json.loads(mrt_path.read_text(encoding="utf-8-sig"))
    pages = data.get("Pages") or {}
    if isinstance(pages, list):
        pages = {str(i): p for i, p in enumerate(pages)}

    filters: list[dict] = []
    page_meta: list[dict] = []
    for idx, (pk, page) in enumerate(
        sorted(pages.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else str(x[0]))
    ):
        if not isinstance(page, dict):
            continue
        page_name = _text(page.get("Name")) or _text(page.get("Alias")) or f"page{idx}"
        page_meta.append({"index": idx, "key": str(pk), "name": page_name})
        walk_components(page.get("Components") or {}, idx, page_name, filters)

    return {
        "mrt": mrt_path.name,
        "pages": page_meta,
        "count": len(filters),
        "filters": filters,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract filter elements from .mrt")
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
