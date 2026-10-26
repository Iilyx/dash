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

    return {
        "report": report.get("ReportName") or report.get("ReportAlias"),
        "databases": databases,
        "sources": sources,
        "relations": relations,
        "elements": elements,
    }


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
        f"{len(catalog['relations'])} relations, {len(catalog['elements'])} elements"
    )


if __name__ == "__main__":
    main()
