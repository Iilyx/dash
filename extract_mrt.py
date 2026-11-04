#!/usr/bin/env python3
"""Turn any Stimulsoft JSON .mrt into a compact catalog for an LLM.

Entities follow Stimulsoft class reference / dashboard docs
(https://admin.stimulsoft.com/documentation/classreference-js/,
https://www.stimulsoft.com/en/documentation/dashboard-elements):

Dictionary: Databases, DataSources (SqlCommand/Parameters/Columns), Relations,
Variables (+ DialogInfo), BusinessObjects, Resources, UserFunctions.

Pages: StiDashboard / StiPage → Components (IStiElement family):
  viz — Chart, Table, PivotTable, Indicator, Gauge, Progress, Cards,
        RegionMap, OnlineMap, Text, Image, Shape, WebContent, Button, Panel
  filters — ComboBox, ListBox, TreeView, TreeViewBox, DatePicker, NumberBox

Per element (valuable fields only):
  meters (Arguments/Values/Series/KeyMeters/Columns/Rows/Summaries/Target/…),
  DataFilters (StiDataFilterRule: Path, Condition, Value, IsExpression, …),
  DashboardInteraction (OnClick/OnHover/Hyperlink/DrillDownPageKey/
  DrillDownParameters Name+Expression), TopN (Mode/Count/ShowOthers/…),
  DataTransformation + TransformActions/Filters/Sorts, UserFilters/UserSorts,
  ParentKey, Group, CrossFiltering, SelectionMode, ShowAllValue, InitialValue,
  conditions (table/pivot/indicator — value side only).

Layout brushes/rects, images blobs, scripts, connection strings are left out.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from pathlib import Path

TAG = re.compile(r"<[^>]+>")
BREAK = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)

QUERY_KEYS = ("SqlCommand", "Query", "SqlQuery", "Command")
FORMAT_KEYS = (
    "TextFormat",
    "ValueFormat",
    "TargetFormat",
    "TitleFormat",
    "AxisFormat",
    "ArgumentFormat",
    "SeriesFormat",
)
# IStiMeter collections / single meters across Chart/Table/Pivot/Indicator/…
METER_KEYS = {
    "Value",
    "Values",
    "ValueMeters",
    "Argument",
    "Arguments",
    "ArgumentMeters",
    "Series",
    "SeriesMeters",
    "SeriesValues",
    "KeyMeters",
    "KeyMeter",
    "NameMeter",
    "ValueMeter",
    "RowMeters",
    "ColumnMeters",
    "Rows",
    "Columns",
    "Summaries",
    "Summary",
    "Target",
    "Targets",
    "TargetMeters",
    "Weight",
    "Weights",
    "X",
    "Y",
    "XMeters",
    "YMeters",
    "Latitude",
    "Longitude",
    "Location",
    "Color",
    "Colors",
    "Size",
    "MaxValue",
    "MinValue",
    "EndValue",
    "EndValues",
    "CloseValue",
    "OpenValue",
    "HighValue",
    "LowValue",
    "Indicator",
    "Meters",
    "ManuallyEnteredChartMeter",
}
FILTER_ELEMENT_IDENTS = {
    "StiComboBoxElement",
    "StiListBoxElement",
    "StiTreeViewElement",
    "StiTreeViewBoxElement",
    "StiDatePickerElement",
    "StiNumberBoxElement",
}
VIZ_ELEMENT_IDENTS = {
    "StiChartElement",
    "StiTableElement",
    "StiPivotTableElement",
    "StiIndicatorElement",
    "StiGaugeElement",
    "StiProgressElement",
    "StiCardsElement",
    "StiRegionMapElement",
    "StiOnlineMapElement",
    "StiTextElement",
    "StiImageElement",
    "StiShapeElement",
    "StiWebContentElement",
    "StiButtonElement",
    "StiPanelElement",
}
SKIP_DEEP = {
    "ManuallyEnteredData",
    "Brush",
    "TextBrush",
    "Border",
    "Shadow",
    "Font",
    "Image",
    "ReportImage",
    "ClientRectangle",
    "AltClientRectangle",
    "CornerRadius",
    "Style",
    "Watermark",
    "PageWatermark",
    "HeaderFont",
    "FooterFont",
}


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
            if key in SKIP_DEEP or key == "Components":
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


def meter_of(node: dict) -> dict | None:
    """One IStiMeter / Dimension|Measure column (Expression, Label, Function, …)."""
    if not isinstance(node, dict):
        return None
    item = {
        "type": node.get("Ident"),
        "key": text(node.get("Key")),
        "expression": text(node.get("Expression")),
        "label": text(node.get("Label") or node.get("Alias") or node.get("Name")),
        "function": text(node.get("Function")),
        "mode": text(node.get("Mode")),
        "column": text(node.get("Column") or node.get("Path")),
        "display": text(node.get("DisplayValue") or node.get("DisplaySource")),
    }
    cleaned = {key: value for key, value in item.items() if value not in (None, "")}
    return cleaned or None


def meters_bucket(value) -> list[dict]:
    result = []
    if isinstance(value, dict) and value.get("Ident") and (
        "Meter" in str(value.get("Ident")) or value.get("Expression") is not None
    ):
        parsed = meter_of(value)
        if parsed:
            result.append(parsed)
        return result
    for item in numbered(value):
        if isinstance(item, dict):
            parsed = meter_of(item)
            if parsed:
                result.append(parsed)
    return result


def meters_of(node: dict) -> dict[str, list[dict]]:
    """Collect all Value / Arguments / Series / *Meters bindings on a component."""
    result: dict[str, list[dict]] = {}
    for key, value in node.items():
        if key in SKIP_DEEP:
            continue
        is_meter_key = key in METER_KEYS or key.endswith("Meters") or key.endswith("Meter")
        if not is_meter_key:
            continue
        bucket = meters_bucket(value)
        if bucket:
            result[key] = bucket
    return result


def drill_down_parameters(node) -> list[dict]:
    """StiDashboardDrillDownParameter: Name + Expression."""
    result = []
    for item in numbered(node):
        if not isinstance(item, dict):
            continue
        parsed = {
            "name": text(item.get("Name") or item.get("ParamName")),
            "expression": text(item.get("Expression") or item.get("Value")),
        }
        cleaned = {key: value for key, value in parsed.items() if value}
        if cleaned:
            result.append(cleaned)
    return result


def top_n_of(node) -> dict | None:
    """StiDataTopN / series TopN: Mode, Count, ShowOthers, OthersText, MeasureField."""
    value = node.get("TopN")
    if not isinstance(value, dict):
        return None
    mode = text(value.get("Mode"))
    parsed = {
        "mode": mode,
        "count": value.get("Count"),
        "show_others": value.get("ShowOthers"),
        "others_text": text(value.get("OthersText")),
        "measure_field": text(value.get("MeasureField")),
    }
    if parsed.get("mode") in (None, "None"):
        return None
    if parsed.get("show_others") not in (True, False):
        parsed.pop("show_others", None)
    if parsed.get("count") in (None, ""):
        parsed.pop("count", None)
    return {key: item for key, item in parsed.items() if item not in (None, "")} or None


def sorts_of(node) -> list[dict]:
    result = []
    for item in numbered(node):
        if not isinstance(item, dict):
            continue
        sort = {
            "column": text(item.get("Column") or item.get("Path") or item.get("Field")),
            "direction": text(item.get("Direction") or item.get("SortDirection")),
            "expression": text(item.get("Expression")),
        }
        cleaned = {key: value for key, value in sort.items() if value}
        if cleaned:
            result.append(cleaned)
    return result


def conditions_of(node: dict) -> list[dict]:
    """Table / Pivot / Indicator conditions — keep value side, drop colors/fonts."""
    result = []
    for key in (
        "TableConditions",
        "PivotTableConditions",
        "IndicatorConditions",
        "ChartConditions",
        "Conditions",
    ):
        for item in numbered(node.get(key)):
            if not isinstance(item, dict):
                continue
            meters = string_list(item.get("KeyDataFieldMeters"))
            key_meter = text(item.get("KeyValueMeter"))
            if key_meter and key_meter not in meters:
                meters.append(key_meter)
            destinations = string_list(item.get("KeyDestinationMeters"))
            dest_meter = text(item.get("DestinationValueMeter"))
            if dest_meter and dest_meter not in destinations:
                destinations.append(dest_meter)
            cond = {
                "type": text(item.get("Ident")),
                "condition": text(item.get("Condition")),
                "data_type": text(item.get("DataType")),
                "value": text(item.get("Value") or item.get("Value1")),
                "value2": text(item.get("Value2")),
                "field": text(item.get("Field") or item.get("Path")),
                "meters": meters,
                "destination_meters": destinations,
            }
            if item.get("IsExpression") is True:
                cond["is_expression"] = True
            cleaned = {name: value for name, value in cond.items() if value not in (None, "", [])}
            if cleaned:
                result.append(cleaned)
    return result


def data_transformation(node: dict | None) -> dict | None:
    if not isinstance(node, dict):
        return None
    parsed = {
        "actions": [],
        "filters": filters_of(node) if ("Filters" in node or "DataFilters" in node) else [],
        "sorts": sorts_of(node.get("TransformSorts") or node.get("Sorts")),
    }
    for item in numbered(node.get("TransformActions") or node.get("Actions")):
        if not isinstance(item, dict):
            continue
        action = {
            "type": text(item.get("Ident") or item.get("Type") or item.get("Action")),
            "expression": text(item.get("Expression")),
            "column": text(item.get("Column") or item.get("Path")),
        }
        cleaned = {key: value for key, value in action.items() if value}
        if cleaned:
            parsed["actions"].append(cleaned)
    for item in numbered(node.get("TransformFilters")):
        if isinstance(item, dict):
            filt = data_filter(item)
            if filt:
                parsed["filters"].append(filt)
    cleaned = {key: value for key, value in parsed.items() if value}
    return cleaned or None


def element_role(ident: str | None) -> str | None:
    if not ident:
        return None
    if ident in FILTER_ELEMENT_IDENTS:
        return "filter"
    if ident in VIZ_ELEMENT_IDENTS:
        return "viz"
    if ident and ident.endswith("Element"):
        return "element"
    return None


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
    """StiDataFilterRule: Path, Condition, Value/Value2, IsExpression, FilterGroupKey, …"""
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
            item["is_expression"] = True
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
    if node.get("IsExpression") is True:
        item["is_expression"] = True
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
    keys_column = text(node.get("KeysColumn"))
    values_column = text(node.get("ValuesColumn"))
    # путь к source: KeysColumn вида "categories.CategoryID" или явное DataSourceName
    source_name = text(
        node.get("DataSourceName") or node.get("DataSource") or node.get("NameInSource")
    )
    if not source_name and keys_column and "." in keys_column:
        source_name = keys_column.split(".", 1)[0].strip() or None
    info = {
        "items": dialog_items(node),
        "keys_column": keys_column,
        "values_column": values_column,
        "checked_column": text(node.get("CheckedColumn")),
        "filter": text(node.get("FilterExpression")),
        "binding_values_column": text(node.get("BindingValuesColumn")),
        "binding_variable": text(node.get("BindingVariable")),
        "source": source_name,
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
    value = node.get("DashboardInteraction") or node.get("Interaction")
    if not isinstance(value, dict):
        return None
    parsed = {
        "type": text(value.get("Ident")),
        "on_click": text(value.get("OnClick")),
        "on_hover": text(value.get("OnHover")),
        "drill_down": text(value.get("DrillDownMode") or value.get("OnDrillDown")),
        "drill_down_page": text(value.get("DrillDownPageKey") or value.get("DrillDownPage")),
        "hyperlink": text(value.get("Hyperlink")),
        "hyperlink_destination": text(value.get("HyperlinkDestination")),
        "tooltip": text(value.get("ToolTip")),
        "allow_user_drill_down": value.get("AllowUserDrillDown"),
        "drill_down_parameters": drill_down_parameters(
            value.get("DrillDownParameters") or node.get("DrillDownParameters")
        ),
    }
    if parsed.get("on_click") in (None, "None"):
        parsed.pop("on_click", None)
    if parsed.get("on_hover") in (None, "None"):
        parsed.pop("on_hover", None)
    if parsed.get("allow_user_drill_down") not in (True, False):
        parsed.pop("allow_user_drill_down", None)
    cleaned = {key: item for key, item in parsed.items() if item not in (None, "", [])}
    return cleaned or None


def collect_filter_rules(node) -> list[dict]:
    result = []
    for item in numbered(node):
        if isinstance(item, dict):
            filt = data_filter(item)
            if filt:
                result.append(filt)
        elif isinstance(item, list):
            result.extend(collect_filter_rules(item))
    return result


def element(node: dict) -> dict:
    ident = node.get("Ident")
    shown = visible_text(node.get("Text")) if isinstance(node.get("Text"), str) else None
    title = title_of(node)
    meters = meters_of(node)
    exprs = expressions(node)
    for item in brace_expressions(shown) + brace_expressions(title):
        if item not in exprs:
            exprs.append(item)
    for bucket in meters.values():
        for meter in bucket:
            expr = meter.get("expression")
            if expr and expr not in exprs:
                exprs.append(expr)

    dd_filters = collect_filter_rules(node.get("DrillDownFilters"))
    dd_filters.extend(collect_filter_rules(node.get("DrillDownFiltersList")))

    user_filters = collect_filter_rules(node.get("UserFilters"))
    transform_filters = collect_filter_rules(node.get("TransformFilters"))
    user_sorts = sorts_of(node.get("UserSorts"))
    transform_sorts = sorts_of(node.get("TransformSorts"))

    # Named Expressions collection: {0: {Name, Expression}, …}
    named_expressions = []
    for item in numbered(node.get("Expressions")):
        if not isinstance(item, dict):
            continue
        named = {
            "name": text(item.get("Name")),
            "expression": text(item.get("Expression")),
        }
        cleaned = {key: value for key, value in named.items() if value}
        if cleaned:
            named_expressions.append(cleaned)
            expr = cleaned.get("expression")
            if expr and expr not in exprs:
                exprs.append(expr)

    parsed = {
        "name": node.get("Name"),
        "alias": text(node.get("Alias")),
        "type": ident,
        "role": element_role(ident if isinstance(ident, str) else None),
        "key": text(node.get("Key") or node.get("Guid")),
        "title": title,
        "text": shown,
        "meters": meters,
        "expressions": exprs,
        "named_expressions": named_expressions,
        "formats": formats(node),
        "filters": filters_of(node),
        "user_filters": user_filters,
        "transform_filters": transform_filters,
        "user_sorts": user_sorts,
        "transform_sorts": transform_sorts,
        "top_n": top_n_of(node),
        "conditions": conditions_of(node),
        "interaction": interaction(node),
        "drill_down_parameters": drill_down_parameters(node.get("DrillDownParameters")),
        "drill_down_filters": dd_filters,
        "data_transformation": data_transformation(node.get("DataTransformation")),
        "data_source": text(
            node.get("DataSourceName") or node.get("DataSource") or node.get("NameInSource")
        ),
        "data_mode": text(node.get("DataMode")),
        "group": text(node.get("Group")),
        "parent": text(node.get("ParentKey") or node.get("MasterComponent")),
        "cross_filtering": node.get("CrossFiltering"),
        "selection_mode": text(node.get("SelectionMode")),
        "initial_value": text(node.get("InitialValue")),
        "summary_direction": text(node.get("SummaryDirection")),
        "visibility_expression": text(node.get("VisibilityExpression")),
    }
    if node.get("FilterOn") is False:
        parsed["filter_on"] = False
    if node.get("ShowAllValue") is True:
        parsed["show_all_value"] = True
    if node.get("ShowBlanks") is True:
        parsed["show_blanks"] = True
    if parsed.get("cross_filtering") not in (True, False):
        parsed.pop("cross_filtering", None)
    children = elements(node.get("Components"))
    if children:
        parsed["elements"] = children
    return {key: value for key, value in parsed.items() if value not in (None, "", [])}


def elements(node) -> list[dict]:
    return [
        element(item)
        for item in numbered(node)
        if isinstance(item, dict) and (item.get("Name") or item.get("Ident"))
    ]


def pages(node) -> list[dict]:
    result = []
    for page in numbered(node):
        if not isinstance(page, dict):
            continue
        ident = page.get("Ident")
        parsed = {
            "name": page.get("Name"),
            "alias": text(page.get("Alias")),
            "type": ident,
            "guid": text(page.get("Guid") or page.get("Key")),
            "is_dashboard": ident == "StiDashboard" or bool(page.get("IsDashboard")),
            "filters": filters_of(page),
            "elements": elements(page.get("Components")),
        }
        if parsed.get("is_dashboard") is False:
            parsed.pop("is_dashboard", None)
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


def page_index_entry(index: int, page: dict) -> dict:
    """Compact card for the LLM: enough to pick a page, not the full body."""
    titles: list[str] = []
    element_names: list[str] = []
    roles: list[str] = []
    for item in page.get("elements") or []:
        if not isinstance(item, dict):
            continue
        if item.get("name"):
            element_names.append(str(item["name"]))
        if item.get("title"):
            titles.append(str(item["title"]))
        if item.get("text"):
            titles.append(str(item["text"]))
        role = item.get("role")
        if isinstance(role, str) and role not in roles:
            roles.append(role)
    entry = {
        "index": index,
        "name": page.get("name"),
        "alias": page.get("alias"),
        "type": page.get("type"),
        "element_count": len(page.get("elements") or []),
        "roles": roles,
        "element_names": element_names[:40],
        "titles": titles[:40],
    }
    if page.get("is_dashboard") is True:
        entry["is_dashboard"] = True
    return {
        key: value
        for key, value in entry.items()
        if value is not None and value != "" and value != []
    }


def build_page_index(catalog: dict) -> list[dict]:
    return [
        page_index_entry(index, page)
        for index, page in enumerate(catalog.get("pages") or [])
        if isinstance(page, dict)
    ]


def catalog_for_page(catalog: dict, index: int) -> dict:
    """Same shape as catalog.json, but pages[] contains only one page."""
    pages_list = [page for page in (catalog.get("pages") or []) if isinstance(page, dict)]
    if index < 0 or index >= len(pages_list):
        raise KeyError(f"page index out of range: {index}")
    out = {key: value for key, value in catalog.items() if key != "pages"}
    out["pages"] = [pages_list[index]]
    return out


def write_page_packs(catalog: dict, target: Path) -> Path:
    """catalog_pages/0.json, 1.json, … — полный catalog на каждую page."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    pages_list = [page for page in (catalog.get("pages") or []) if isinstance(page, dict)]
    for index in range(len(pages_list)):
        payload = catalog_for_page(catalog, index)
        (target / f"{index}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract a compact catalog from a JSON .mrt file. "
        "Writes catalog.json and catalog_pages/N.json (one catalog per page)."
    )
    parser.add_argument("mrt", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="Full catalog JSON path (default: catalog.json)")
    parser.add_argument(
        "--pages-dir",
        type=Path,
        default=Path("catalog_pages"),
        help="Directory for per-page catalog JSON files (default: catalog_pages)",
    )
    parser.add_argument(
        "--no-pages",
        action="store_true",
        help="Do not write catalog_pages/",
    )
    args = parser.parse_args()
    catalog = catalog_from_report(load_mrt(args.mrt))
    payload = json.dumps(catalog, ensure_ascii=False, indent=2)
    output = args.output or Path("catalog.json")
    output.write_text(payload + "\n", encoding="utf-8")
    if not args.no_pages:
        pages_path = write_page_packs(catalog, args.pages_dir)
        page_count = len([p for p in (catalog.get("pages") or []) if isinstance(p, dict)])
        print(
            f"wrote {output} ({len(payload)} bytes), "
            f"{pages_path}/0..{page_count - 1}.json ({page_count} pages)",
            file=sys.stderr,
        )
    else:
        print(f"wrote {output} ({len(payload)} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
