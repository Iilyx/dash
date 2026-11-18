#!/usr/bin/env python3
"""Снять с .mrt только интерактив: фильтры, кнопки, карты, OnClick-провалы.

Пишет interactive.json — план для «Скан кликов» во viewer.
Классификация по Ident (если есть) и по форме компонента (MapIdent, SelectionMode, …),
чтобы работало даже на частично пустых шаблонах.
"""

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
BUTTON_IDENTS = {"StiButtonElement"}
MAP_IDENTS = {"StiRegionMapElement", "StiOnlineMapElement"}
CHART_IDENTS = {"StiChartElement"}
IMAGE_IDENTS = {"StiImageElement"}
TEXT_IDENTS = {"StiTextElement"}


def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return str(v) if v != "" else None


def _interaction(node: dict) -> dict | None:
    value = node.get("DashboardInteraction") or node.get("Interaction")
    if not isinstance(value, dict):
        return None
    on_click = _text(value.get("OnClick"))
    if on_click in (None, "None"):
        on_click = None
    out = {
        "on_click": on_click,
        "on_hover": _text(value.get("OnHover")),
        "drill_down_page": _text(value.get("DrillDownPageKey") or value.get("DrillDownPage")),
        "hyperlink": _text(value.get("Hyperlink")),
        "allow_user_drill_down": value.get("AllowUserDrillDown")
        if value.get("AllowUserDrillDown") in (True, False, "True", "False", "true", "false")
        else None,
    }
    # поле OnClick присутствует в схеме → потенциально кликабельно
    out["has_onclick_slot"] = "OnClick" in value
    cleaned = {k: v for k, v in out.items() if v not in (None, "", [])}
    return cleaned or None


def _title(node: dict) -> str | None:
    title = node.get("Title")
    if isinstance(title, dict):
        return _text(title.get("Text"))
    return _text(title)


def classify(node: dict) -> str | None:
    """Вернуть kind для интерактивного плана или None если не трогаем."""
    ident = _text(node.get("Ident")) or ""
    keys = set(node.keys())

    if ident in MAP_IDENTS or "MapIdent" in keys:
        return "map"
    if ident in BUTTON_IDENTS or "ButtonText" in keys or "ButtonType" in keys:
        return "button"
    if ident in FILTER_IDENTS or "SelectionMode" in keys:
        if ident == "StiListBoxElement" or (
            "SelectionMode" in keys and "ButtonText" not in keys and "MapIdent" not in keys
        ):
            # Combo vs List: Combo часто без Items на верхнем уровне в том же виде
            if ident == "StiComboBoxElement" or ident == "StiTreeViewBoxElement":
                return "filter_combo"
            if ident in {
                "StiListBoxElement",
                "StiTreeViewElement",
                "StiDatePickerElement",
                "StiNumberBoxElement",
            }:
                return "filter_list"
            return "filter_combo" if "Items" in keys else "filter_list"
        return "filter_combo"
    def _has_real_click(ix: dict | None) -> bool:
        if not ix:
            return False
        if ix.get("on_click") in ("ShowDashboard", "DrillDown", "ApplyFilter"):
            return True
        if ix.get("allow_user_drill_down") in (True, "True", "true"):
            return True
        if ix.get("drill_down_page"):
            return True
        return False

    if ident in CHART_IDENTS or ("Arguments" in keys and "Values" in keys):
        if _has_real_click(_interaction(node)):
            return "chart"
        return None
    if ident in IMAGE_IDENTS or (
        "Image" in keys and "MapIdent" not in keys and "Text" not in keys and "SelectionMode" not in keys
    ):
        if _has_real_click(_interaction(node)):
            return "image"
        return None
    if ident in TEXT_IDENTS or (
        "Text" in keys and "MapIdent" not in keys and "SelectionMode" not in keys and "Image" not in keys
    ):
        if _has_real_click(_interaction(node)):
            return "text"
        return None
    return None


def _actions_for(kind: str, ix: dict | None) -> list[str]:
    if kind == "map":
        return ["region_drill"]
    if kind == "chart":
        on = (ix or {}).get("on_click")
        if on == "ApplyFilter":
            return ["geom_filter"]
        return ["geom_drill"]
    if kind == "filter_combo":
        return ["combo_pick"]
    if kind == "filter_list":
        return ["list_pick"]
    if kind == "button":
        return ["click"]
    if kind in ("image", "text"):
        on = (ix or {}).get("on_click")
        if on == "OpenHyperlink":
            return []  # не уводим со страницы
        return ["click"]
    return []


def walk_components(comps, page_index: int, page_name: str | None, out: list, path: str = "") -> None:
    if not isinstance(comps, dict):
        return
    for key, node in comps.items():
        if not isinstance(node, dict):
            continue
        kind = classify(node)
        ix = _interaction(node)
        name = _text(node.get("Name"))
        alias = _text(node.get("Alias"))
        title = _title(node)
        ident = _text(node.get("Ident"))
        if kind:
            actions = _actions_for(kind, ix)
            if actions:
                out.append(
                    {
                        "page_index": page_index,
                        "page_name": page_name,
                        "comp_key": str(key),
                        "path": f"{path}.{key}" if path else str(key),
                        "kind": kind,
                        "type": ident,
                        "name": name,
                        "alias": alias,
                        "title": title,
                        "interaction": {
                            k: v
                            for k, v in (ix or {}).items()
                            if k != "has_onclick_slot" and v not in (None, "")
                        }
                        or None,
                        "actions": actions,
                    }
                )
        nested = node.get("Components")
        if isinstance(nested, dict):
            walk_components(
                nested,
                page_index,
                page_name,
                out,
                f"{path}.{key}" if path else str(key),
            )


def extract_interactive(mrt_path: Path | str) -> dict:
    mrt_path = Path(mrt_path)
    data = json.loads(mrt_path.read_text(encoding="utf-8-sig"))
    pages = data.get("Pages") or {}
    if isinstance(pages, list):
        pages = {str(i): p for i, p in enumerate(pages)}

    items: list[dict] = []
    page_meta: list[dict] = []
    for idx, (pk, page) in enumerate(sorted(pages.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else str(x[0]))):
        if not isinstance(page, dict):
            continue
        page_name = _text(page.get("Name")) or _text(page.get("Alias")) or f"page{idx}"
        page_meta.append(
            {
                "index": idx,
                "key": str(pk),
                "name": page_name,
                "ident": _text(page.get("Ident")),
            }
        )
        walk_components(page.get("Components") or {}, idx, page_name, items)

    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1

    return {
        "mrt": str(mrt_path.name),
        "pages": page_meta,
        "counts": by_kind,
        "items": items,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract interactive plan from Stimulsoft .mrt")
    ap.add_argument("mrt", type=Path, nargs="?", default=None)
    ap.add_argument("-o", "--output", type=Path, default=Path("interactive.json"))
    args = ap.parse_args()
    mrt = args.mrt
    if mrt is None:
        import os

        mrt = Path(os.environ.get("MRT_PATH") or "test.mrt")
    if not mrt.is_file():
        raise SystemExit(f"нет файла: {mrt}")
    plan = extract_interactive(mrt)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {args.output} pages={len(plan['pages'])} interactive={len(plan['items'])} {plan['counts']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
