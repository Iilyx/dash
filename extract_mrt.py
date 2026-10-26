#!/usr/bin/env python3
"""Pull a short catalog out of a Stimulsoft .mrt dashboard (JSON form)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def numbered(node: dict | None) -> list:
    if not isinstance(node, dict):
        return []
    def sort_key(key: str):
        return int(key) if str(key).isdigit() else key

    return [node[key] for key in sorted(node, key=sort_key)]


def columns(node: dict | None) -> list[dict]:
    result = []
    for column in numbered(node):
        if not isinstance(column, dict) or "Name" not in column:
            continue
        result.append(
            {
                "name": column.get("Name"),
                "alias": column.get("Alias") or column.get("Name"),
                "type": column.get("Type"),
            }
        )
    return result


def string_list(node) -> list[str]:
    if isinstance(node, list):
        return [str(item) for item in node]
    if isinstance(node, dict):
        return [str(item) for item in numbered(node)]
    if isinstance(node, str) and node:
        return [node]
    return []


def dialog_items(info: dict) -> list[dict]:
    item_nodes = info.get("Items")
    parsed = []
    for item in numbered(item_nodes) if isinstance(item_nodes, dict) else item_nodes or []:
        if not isinstance(item, dict):
            continue
        row = {"key": item.get("Key"), "value": item.get("Value")}
        if item.get("KeyTo") not in (None, ""):
            row["key_to"] = item.get("KeyTo")
        if item.get("ValueTo") not in (None, ""):
            row["value_to"] = item.get("ValueTo")
        parsed.append(row)
    if parsed:
        return parsed
    keys = string_list(info.get("Keys"))
    values = string_list(info.get("Values"))
    rows = []
    for index in range(max(len(keys), len(values))):
        rows.append(
            {
                "key": keys[index] if index < len(keys) else None,
                "value": values[index] if index < len(values) else None,
            }
        )
    return rows


def dialog_info(node: dict | None) -> dict | None:
    """StiDialogInfo: the list a user picks from when the variable is requested."""
    if not isinstance(node, dict):
        return None
    info: dict = {}
    items = dialog_items(node)
    if items:
        info["items"] = items
    for source, target in (
        ("KeysColumn", "keys_column"),
        ("ValuesColumn", "values_column"),
        ("BindingValuesColumn", "binding_values_column"),
        ("Mask", "mask"),
        ("DateTimeType", "date_time_type"),
        ("ItemsInitializationType", "items_from"),
    ):
        value = node.get(source)
        if value not in (None, ""):
            info[target] = value
    if "AllowUserValues" in node:
        info["allow_user_values"] = bool(node.get("AllowUserValues"))
    binding = node.get("BindingVariable")
    if isinstance(binding, dict) and binding.get("Name"):
        info["binding_variable"] = binding.get("Name")
    elif isinstance(binding, str) and binding:
        info["binding_variable"] = binding
    return info or None


def variables(node: dict | None) -> list[dict]:
    result = []
    for variable in numbered(node):
        if not isinstance(variable, dict) or not variable.get("Name"):
            continue
        item = {
            "name": variable.get("Name"),
            "alias": variable.get("Alias") or variable.get("Name"),
            "type": variable.get("Type"),
            "value": variable.get("Value"),
        }
        for source, target in (
            ("Category", "category"),
            ("Description", "description"),
            ("InitBy", "init_by"),
            ("InitByExpressionFrom", "from"),
            ("InitByExpressionTo", "to"),
            ("Selection", "selection"),
        ):
            value = variable.get(source)
            if value not in (None, ""):
                item[target] = value
        if variable.get("RequestFromUser"):
            item["request_from_user"] = True
        if variable.get("AllowUseAsSqlParameter"):
            item["sql_parameter"] = True
        if variable.get("ReadOnly"):
            item["read_only"] = True
        dialog = dialog_info(variable.get("DialogInfo"))
        if dialog:
            item["dialog"] = dialog
        result.append(item)
    return result


def business_objects(node: dict | None) -> list[dict]:
    result = []
    for obj in numbered(node):
        if not isinstance(obj, dict) or not obj.get("Name"):
            continue
        item = {
            "name": obj.get("Name"),
            "alias": obj.get("Alias") or obj.get("Name"),
            "columns": columns(obj.get("Columns")),
        }
        nested = business_objects(obj.get("BusinessObjects"))
        if nested:
            item["objects"] = nested
        result.append(item)
    return result


def parameters(node: dict | None) -> list[dict]:
    result = []
    for parameter in numbered(node):
        if not isinstance(parameter, dict) or "Name" not in parameter:
            continue
        result.append(
            {
                "name": parameter.get("Name"),
                "expression": parameter.get("Expression"),
                "type": parameter.get("Type"),
            }
        )
    return result


def expressions(node) -> list[str]:
    found: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            expression = value.get("Expression")
            if isinstance(expression, str) and expression.strip():
                found.append(expression.strip())
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    # Keep order, drop duplicates.
    unique: list[str] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique


def title_of(component: dict) -> str | None:
    title = component.get("Title")
    if isinstance(title, dict):
        text = title.get("Text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    text = component.get("Text")
    if isinstance(text, str) and "<" not in text and text.strip():
        return text.strip()
    return None


def catalog_from_report(report: dict) -> dict:
    dictionary = report.get("Dictionary") or {}
    databases = []
    for database in numbered(dictionary.get("Databases")):
        if not isinstance(database, dict):
            continue
        databases.append(
            {
                "name": database.get("Name"),
                "alias": database.get("Alias"),
                "type": database.get("Ident"),
            }
        )

    sources = []
    for source in numbered(dictionary.get("DataSources")):
        if not isinstance(source, dict) or "SqlCommand" not in source and "Columns" not in source:
            continue
        item = {
            "name": source.get("Name"),
            "alias": source.get("Alias"),
            "type": source.get("Ident"),
            "database": source.get("NameInSource"),
            "sql": source.get("SqlCommand"),
            "columns": columns(source.get("Columns")),
        }
        params = parameters(source.get("Parameters"))
        if params:
            item["parameters"] = params
        sources.append(item)

    relations = []
    for relation in numbered(dictionary.get("Relations")):
        if not isinstance(relation, dict):
            continue
        relations.append(
            {
                "name": relation.get("Name"),
                "parent": relation.get("ParentSource"),
                "child": relation.get("ChildSource"),
                "parent_columns": [value for value in numbered(relation.get("ParentColumns"))],
                "child_columns": [value for value in numbered(relation.get("ChildColumns"))],
            }
        )

    variables_list = variables(dictionary.get("Variables"))
    objects = business_objects(dictionary.get("BusinessObjects"))

    elements = []
    for page in numbered(report.get("Pages")):
        if not isinstance(page, dict):
            continue
        for component in numbered(page.get("Components")):
            if not isinstance(component, dict):
                continue
            exprs = expressions(component)
            if not exprs and component.get("Ident") == "StiTextElement":
                continue
            if not exprs:
                continue
            elements.append(
                {
                    "page": page.get("Alias") or page.get("Name"),
                    "name": component.get("Name"),
                    "type": component.get("Ident"),
                    "title": title_of(component),
                    "expressions": exprs,
                }
            )

    catalog = {
        "report": report.get("ReportName") or report.get("ReportAlias"),
        "databases": databases,
        "sources": sources,
        "relations": relations,
        "variables": variables_list,
        "business_objects": objects,
        "elements": elements,
    }
    description = report.get("ReportDescription")
    if description:
        catalog["description"] = description
    return catalog


def load_mrt(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        raise SystemExit(
            f"{path} is not a JSON .mrt. This step reads the JSON form Stimulsoft writes now."
        )
    return json.loads(stripped)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a catalog from a Stimulsoft .mrt file.")
    parser.add_argument("mrt", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    catalog = catalog_from_report(load_mrt(args.mrt))
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"{catalog['report']}: {len(catalog['sources'])} sources, "
        f"{len(catalog['variables'])} variables, "
        f"{len(catalog['relations'])} relations, {len(catalog['elements'])} elements"
    )


if __name__ == "__main__":
    main()
