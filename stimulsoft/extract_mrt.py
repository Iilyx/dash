#!/usr/bin/env python3
"""Turn any Stimulsoft JSON .mrt into a compact catalog for an LLM.

The walk follows the dictionary and page tree, not a fixed dashboard.
Layout, images, scripts, and connection strings are left out.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

TAG = re.compile(r"<[^>]+>")
BREAK = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)

QUERY_KEYS = ("SqlCommand", "Query", "SqlQuery", "Command")
FORMAT_KEYS = ("TextFormat", "ValueFormat", "TargetFormat", "TitleFormat", "AxisFormat")


def numbered(node) -> list:
    if isinstance(node, list):
        return node
    if not isinstance(node, dict) or not node:
        return []
    if all(str(key).isdigit() for key in node):
        return [node[key] for key in sorted(node, key=lambda item: int(item))]
    return []


def text(value) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 4000:
        return None
    return cleaned


def visible_text(value) -> str | None:
    """Keep the words inside Stimulsoft HTML-like tags such as <font> and <text-align>."""
    raw = text(value)
    if raw is None or "<" not in raw or ">" not in raw:
        return raw
    without_tags = TAG.sub("", BREAK.sub("\n", raw))
    plain = html.unescape(without_tags)
    plain = re.sub(r"[ \t]*\n[ \t]*", "\n", plain)
    plain = re.sub(r"\n{2,}", "\n", plain)
    plain = re.sub(r"[ \t]{2,}", " ", plain).strip()
    return plain or None


def brace_expressions(value: str | None) -> list[str]:
    if not value:
        return []
    found = []
    index = 0
    while index < len(value):
        if value[index] != "{":
            index += 1
            continue
        depth = 1
        cursor = index + 1
        while cursor < len(value) and depth:
            if value[cursor] == "{":
                depth += 1
            elif value[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth == 0:
            inner = value[index + 1:cursor - 1].strip()
            if inner and inner not in found:
                found.append(inner)
            index = cursor
        else:
            index += 1
    return found


def put(target: dict, key: str, value) -> None:
    if value is None or value == "" or value == [] or value == {}:
        return
    target[key] = value


def string_list(node) -> list[str]:
    if isinstance(node, list):
        return [item.strip() for item in node if isinstance(item, str) and item.strip()]
    if isinstance(node, dict):
        return [node[key].strip() for key in sorted(node, key=lambda item: int(item))
                if str(key).isdigit() and isinstance(node[key], str) and node[key].strip()]
    return []


def expressions(node, found: list[str] | None = None) -> list[str]:
    found = [] if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"ManuallyEnteredData", "Brush", "TextBrush", "Border", "Shadow", "Font", "Image", "ReportImage"}:
                continue
            if key == "Expression" or key.endswith("Expression"):
                item = text(value)
                if item and item not in found:
                    found.append(item)
            else:
                expressions(value, found)
    elif isinstance(node, list):
        for value in node:
            expressions(value, found)
    return found


def format_of(node: dict) -> dict:
    result = {
        "type": node.get("Ident"),
        "decimals": node.get("DecimalDigits"),
        "state": text(node.get("State")),
        "symbol": text(node.get("Symbol") or node.get("CurrencySymbol")),
        "negative": node.get("NegativePattern"),
        "positive": node.get("PositivePattern"),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def formats(node: dict) -> list[dict]:
    result = []
    for key in FORMAT_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            item = format_of(value)
            if item:
                item["kind"] = key
                result.append(item)
    return result


def data_filter(node: dict) -> dict:
    if "Path" in node or "FilterGroupKey" in node:
        item = {
            "path": text(node.get("Path")),
            "path2": text(node.get("Path2")),
            "condition": text(node.get("Condition")),
            "operation": text(node.get("Operation")),
            "value": text(node.get("Value")),
            "value2": text(node.get("Value2")),
            "group": text(node.get("FilterGroupKey")),
        }
        if node.get("IsEnabled") is False:
            item["enabled"] = False
        if node.get("IsExpression") is True:
            item["expression"] = True
        return {key: value for key, value in item.items() if value not in (None, "")}
    item = {
        "column": text(node.get("Column")),
        "condition": text(node.get("Condition")),
        "data_type": text(node.get("DataType")),
        "item": text(node.get("Item")),
        "value": text(node.get("Value1") or node.get("Value")),
        "value2": text(node.get("Value2")),
        "expression": text(node.get("Expression")),
    }
    return {key: value for key, value in item.items() if value not in (None, "")}


def filters_of(node: dict) -> list[dict]:
    result = []
    for key in ("DataFilters", "Filters"):
        for item in numbered(node.get(key)):
            if isinstance(item, dict):
                parsed = data_filter(item)
                if parsed:
                    result.append(parsed)
    return result


def columns(node) -> list[dict]:
    result = []
    for column in numbered(node):
        if not isinstance(column, dict) or not column.get("Name"):
            continue
        item = {
            "name": column.get("Name"),
            "alias": column.get("Alias"),
            "type": column.get("Type"),
            "expression": text(column.get("Expression")),
            "mode": text(column.get("Mode")),
            "function": text(column.get("Function")),
        }
        if column.get("PrimaryKey") is True:
            item["primary_key"] = True
        result.append({key: value for key, value in item.items() if value not in (None, "")})
    return result


def parameters(node) -> list[dict]:
    result = []
    for parameter in numbered(node):
        if not isinstance(parameter, dict):
            continue
        item = {
            "name": parameter.get("Name"),
            "expression": text(parameter.get("Expression") or parameter.get("Value")),
            "type": parameter.get("Type"),
            "size": parameter.get("Size"),
        }
        parsed = {key: value for key, value in item.items() if value not in (None, "", 0)}
        if parsed.get("name"):
            result.append(parsed)
    return result


def query_of(node: dict) -> str | None:
    for key in QUERY_KEYS:
        value = text(node.get(key))
        if value:
            return value
    return None


def range_parts(value: str) -> tuple[str, str | None]:
    body = value[1:-1] if value.startswith("{") and value.endswith("}") else value
    if "<<|>>" in body:
        left, right = body.split("<<|>>", 1)
        return left, right
    return body, None


def dialog_items(info: dict) -> list[dict]:
    objects = numbered(info.get("Items"))
    if objects and isinstance(objects[0], dict):
        result = []
        for item in objects:
            parsed = {
                "key": text(item.get("Key")),
                "value": text(item.get("Value")),
                "key_to": text(item.get("KeyTo")),
                "value_to": text(item.get("ValueTo")),
            }
            cleaned = {key: value for key, value in parsed.items() if value}
            if cleaned:
                result.append(cleaned)
        return result

    keys = string_list(info.get("Keys"))
    values = string_list(info.get("Values"))
    checked = info.get("CheckedStates")
    checks = checked if isinstance(checked, list) else []
    result = []
    for index, key in enumerate(keys):
        shown = values[index] if index < len(values) else key
        key_from, key_to = range_parts(key)
        value_from, value_to = range_parts(shown)
        item = {"key": key_from, "value": value_from, "key_to": key_to, "value_to": value_to}
        if index < len(checks) and checks[index] is True:
            item["checked"] = True
        result.append({name: value for name, value in item.items() if value not in (None, "")})
    return result


def dialog_info(node: dict | None) -> dict | None:
    if not isinstance(node, dict):
        return None
    info = {
        "items": dialog_items(node),
        "keys_column": text(node.get("KeysColumn")),
        "values_column": text(node.get("ValuesColumn")),
        "checked_column": text(node.get("CheckedColumn")),
        "filter": text(node.get("FilterExpression")),
        "binding_values_column": text(node.get("BindingValuesColumn")),
        "binding_variable": text(node.get("BindingVariable")),
        "mask": text(node.get("Mask")),
        "validation": text(node.get("Validation")),
        "date_time_type": text(node.get("DateTimeType")),
        "sort_direction": text(node.get("SortDirection")),
        "sort_field": text(node.get("SortField")),
    }
    mode = text(node.get("ItemsInitializationType"))
    if mode and mode != "Items":
        info["items_from"] = mode
    if node.get("AllowUserValues") is False:
        info["allow_user_values"] = False
    if node.get("RememberSelection") is True:
        info["remember_selection"] = True
    if node.get("BindingValue") is True:
        info["binding_value"] = True
    cleaned = {key: value for key, value in info.items() if value not in (None, "", [])}
    return cleaned or None


def variables(node) -> list[dict]:
    result = []
    for variable in numbered(node):
        if not isinstance(variable, dict) or not variable.get("Name"):
            continue
        item = {
            "name": variable.get("Name"),
            "alias": variable.get("Alias"),
            "category": text(variable.get("Category")),
            "description": text(variable.get("Description")),
            "type": text(variable.get("Type")),
            "value": text(variable.get("Value")),
            "init_by": text(variable.get("InitBy")),
            "from": text(variable.get("InitByExpressionFrom")),
            "to": text(variable.get("InitByExpressionTo")),
            "selection": text(variable.get("SelectionMode") or variable.get("Selection")),
        }
        if variable.get("RequestFromUser") is True:
            item["request_from_user"] = True
        if variable.get("ShowOnParametersPanel") is False:
            item["show_on_parameters_panel"] = False
        if variable.get("AllowUseAsSqlParameter") is True:
            item["sql_parameter"] = True
        if variable.get("ReadOnly") is True:
            item["read_only"] = True
        if variable.get("AllowNullableString") is True:
            item["nullable"] = True
        dialog = dialog_info(variable.get("DialogInfo"))
        if dialog:
            item["dialog"] = dialog
        result.append({key: value for key, value in item.items() if value not in (None, "")})
    return result


def business_objects(node) -> list[dict]:
    result = []
    for item in numbered(node):
        if not isinstance(item, dict) or not item.get("Name"):
            continue
        parsed = {
            "name": item.get("Name"),
            "alias": item.get("Alias"),
            "category": text(item.get("Category")),
            "columns": columns(item.get("Columns")),
            "business_objects": business_objects(item.get("BusinessObjects")),
        }
        result.append({key: value for key, value in parsed.items() if value not in (None, "", [])})
    return result


def databases(node) -> list[dict]:
    result = []
    for item in numbered(node):
        if not isinstance(item, dict):
            continue
        path = text(item.get("PathData") or item.get("Path"))
        parsed = {
            "name": item.get("Name"),
            "alias": item.get("Alias"),
            "type": item.get("Ident"),
            "path": path if path and path.startswith("resource://") else None,
        }
        cleaned = {key: value for key, value in parsed.items() if value}
        if cleaned.get("name") or cleaned.get("type"):
            result.append(cleaned)
    return result


def sources(node) -> list[dict]:
    result = []
    for item in numbered(node):
        if not isinstance(item, dict) or not item.get("Name"):
            continue
        parsed = {
            "name": item.get("Name"),
            "alias": item.get("Alias"),
            "type": item.get("Ident"),
            "database": text(item.get("NameInSource")),
            "sql": query_of(item),
            "source_type": text(item.get("Type")),
            "columns": columns(item.get("Columns")),
            "parameters": parameters(item.get("Parameters")),
            "filters": filters_of(item),
        }
        if item.get("ConnectOnStart") is False:
            parsed["connect_on_start"] = False
        result.append({key: value for key, value in parsed.items() if value not in (None, "", [])})
    return result


def relations(node) -> list[dict]:
    result = []
    for item in numbered(node):
        if not isinstance(item, dict) or not item.get("Name"):
            continue
        parsed = {
            "name": item.get("Name"),
            "alias": item.get("Alias"),
            "parent": text(item.get("ParentSource")),
            "child": text(item.get("ChildSource")),
            "parent_columns": string_list(item.get("ParentColumns")),
            "child_columns": string_list(item.get("ChildColumns")),
            "join": text(item.get("JoinType")),
        }
        if item.get("Active") is False:
            parsed["active"] = False
        if parsed.get("join") == "Left":
            parsed.pop("join", None)
        result.append({key: value for key, value in parsed.items() if value not in (None, "", [])})
    return result


def resources(node) -> list[dict]:
    result = []
    for item in numbered(node):
        if isinstance(item, dict) and item.get("Name"):
            result.append({"name": item.get("Name"), "alias": item.get("Alias"), "type": item.get("Type") or item.get("Ident")})
    return [{key: value for key, value in item.items() if value} for item in result]


def functions(node) -> list[dict]:
    result = []
    for item in numbered(node):
        if not isinstance(item, dict) or not item.get("Name"):
            continue
        parsed = {
            "name": item.get("Name"),
            "alias": item.get("Alias"),
            "type": text(item.get("ReturnType") or item.get("Type")),
            "arguments": text(item.get("ArgumentNames") or item.get("Arguments")),
        }
        result.append({key: value for key, value in parsed.items() if value})
    return result


def title_of(node: dict) -> str | None:
    title = node.get("Title")
    if isinstance(title, dict):
        return visible_text(title.get("Text"))
    return visible_text(title)


def interaction(node: dict) -> dict | None:
    value = node.get("DashboardInteraction")
    if not isinstance(value, dict):
        return None
    parsed = {
        "on_click": text(value.get("OnClick")),
        "on_hover": text(value.get("OnHover")),
        "drill_down": text(value.get("DrillDownMode") or value.get("OnDrillDown")),
    }
    if parsed.get("on_click") in (None, "None"):
        parsed.pop("on_click", None)
    if parsed.get("on_hover") in (None, "None", "ShowToolTip"):
        parsed.pop("on_hover", None)
    cleaned = {key: item for key, item in parsed.items() if item}
    return cleaned or None


def element(node: dict) -> dict:
    shown = visible_text(node.get("Text")) if isinstance(node.get("Text"), str) else None
    title = title_of(node)
    exprs = expressions(node)
    for item in brace_expressions(shown) + brace_expressions(title):
        if item not in exprs:
            exprs.append(item)
    parsed = {
        "name": node.get("Name"),
        "alias": text(node.get("Alias")),
        "type": node.get("Ident"),
        "title": title,
        "text": shown,
        "expressions": exprs,
        "formats": formats(node),
        "filters": filters_of(node),
        "interaction": interaction(node),
        "data_source": text(node.get("DataSourceName") or node.get("DataSource")),
        "group": text(node.get("Group")),
        "parent": text(node.get("ParentKey")),
    }
    if node.get("FilterOn") is False:
        parsed["filter_on"] = False
    children = elements(node.get("Components"))
    if children:
        parsed["elements"] = children
    return {key: value for key, value in parsed.items() if value not in (None, "", [])}


def elements(node) -> list[dict]:
    return [element(item) for item in numbered(node) if isinstance(item, dict) and (item.get("Name") or item.get("Ident"))]


def pages(node) -> list[dict]:
    result = []
    for page in numbered(node):
        if not isinstance(page, dict):
            continue
        parsed = {
            "name": page.get("Name"),
            "alias": text(page.get("Alias")),
            "type": page.get("Ident"),
            "filters": filters_of(page),
            "elements": elements(page.get("Components")),
        }
        result.append({key: value for key, value in parsed.items() if value not in (None, "", [])})
    return result


def catalog_from_report(report: dict) -> dict:
    dictionary = report.get("Dictionary") if isinstance(report.get("Dictionary"), dict) else {}
    catalog = {
        "report": {
            "name": report.get("ReportName"),
            "alias": report.get("ReportAlias"),
            "description": text(report.get("ReportDescription")),
        },
        "databases": databases(dictionary.get("Databases")),
        "sources": sources(dictionary.get("DataSources")),
        "relations": relations(dictionary.get("Relations")),
        "variables": variables(dictionary.get("Variables")),
        "business_objects": business_objects(dictionary.get("BusinessObjects")),
        "resources": resources(dictionary.get("Resources")),
        "functions": functions(dictionary.get("UserFunctions")),
        "pages": pages(report.get("Pages")),
    }
    catalog["report"] = {key: value for key, value in catalog["report"].items() if value}
    return {key: value for key, value in catalog.items() if value not in (None, "", [])}


def load_mrt(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig")
    stripped = raw.lstrip()
    if stripped.startswith("<"):
        raise SystemExit(f"{path} is XML. This converter reads JSON .mrt files.")
    report = json.loads(raw)
    if not isinstance(report, dict) or "Dictionary" not in report and "Pages" not in report:
        raise SystemExit(f"{path} is not a Stimulsoft report.")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a compact catalog from a JSON .mrt file.")
    parser.add_argument("mrt", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    catalog = catalog_from_report(load_mrt(args.mrt))
    payload = json.dumps(catalog, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload + "\n")


if __name__ == "__main__":
    main()
