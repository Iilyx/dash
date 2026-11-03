#!/usr/bin/env python3
"""Ask pipeline (staged):

1) start: MRT → catalog.json + catalog_pages/
2) resolve {Name}: from question, else default — log both
3) router LLM: which pages (from full index)
4) host loads those packs; SQL = sources from selected pages with params filled
5) host execute_sql via MCP
6) answerer LLM: final answer from rows + widget logic
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

from extract_mrt import catalog_from_report, load_mrt

ROOT = Path(__file__).resolve().parent
MAX_TOOL_ROUNDS = 4
MAX_TOOL_CHARS = 24_000
LOG_PREVIEW = 400
# Only {Name} is a Stimulsoft variable. Bare Name is a column.
PLACEHOLDER_BRACE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
LOCAL_PAGE_TOOLS = {"list_pages", "load_page"}

log = logging.getLogger("host")


class Ask(BaseModel):
    question: str = Field(min_length=1)


def setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)


def preview(value: Any, limit: int = LOG_PREVIEW) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def redact_url(url: str) -> str:
    return re.sub(r":([^:@/]+)@", r":***@", url)


def load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def resolve_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def database_url() -> str:
    configured = os.environ.get("DATABASE_URL", "").strip()
    if configured:
        return configured
    user = os.environ.get("POSTGRES_USER", "").strip()
    password = os.environ.get("POSTGRES_PASSWORD", "").strip()
    db = os.environ.get("POSTGRES_DB", "").strip()
    port = os.environ.get("POSTGRES_PORT", "5432").strip() or "5432"
    host = os.environ.get("POSTGRES_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if user and password and db:
        return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    return ""


def llm_base_url() -> str:
    raw = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    if not raw:
        raise RuntimeError("Нужна LLM_BASE_URL, например http://127.0.0.1:8080/v1")
    if not raw.endswith("/v1"):
        raw = f"{raw}/v1"
    return raw


def pages_dir() -> Path:
    return resolve_path(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages"))


def ensure_catalog() -> dict:
    catalog_path = resolve_path(os.environ.get("CATALOG_PATH", "catalog.json"))
    mrt_raw = os.environ.get("MRT_PATH", "").strip()
    if mrt_raw:
        mrt_path = resolve_path(mrt_raw)
        if mrt_path.is_file():
            log.info("Собираю каталог из %s", mrt_path)
            catalog = catalog_from_report(load_mrt(mrt_path))
            catalog_path.write_text(
                json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_page_packs(catalog)
            log.info(
                "Каталог готов → %s (sources=%s, pages=%s, bytes=%s)",
                catalog_path,
                len(catalog.get("sources") or []),
                len(catalog.get("pages") or []),
                catalog_path.stat().st_size,
            )
            return catalog
    if not catalog_path.is_file():
        raise RuntimeError("Нужен MRT_PATH или существующий CATALOG_PATH")
    log.info("Читаю готовый каталог %s", catalog_path)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    write_page_packs(catalog)
    return catalog


def _walk_strings(node: Any, out: list[str]) -> None:
    if isinstance(node, str):
        if node.strip():
            out.append(node.strip())
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in {"key", "guid", "Guid", "Key"}:
                continue
            _walk_strings(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_strings(item, out)


def page_index_entry(index: int, page: dict) -> dict:
    titles: list[str] = []
    element_names: list[str] = []
    for element in page.get("elements") or []:
        if not isinstance(element, dict):
            continue
        if element.get("name"):
            element_names.append(str(element["name"]))
        if element.get("title"):
            titles.append(str(element["title"]))
        if element.get("text"):
            titles.append(str(element["text"]))
    return {
        "index": index,
        "name": page.get("name"),
        "alias": page.get("alias"),
        "type": page.get("type"),
        "is_dashboard": page.get("is_dashboard"),
        "element_count": len(page.get("elements") or []),
        "element_names": element_names[:40],
        "titles": titles[:40],
    }


def build_page_index(catalog: dict) -> list[dict]:
    return [
        page_index_entry(index, page)
        for index, page in enumerate(catalog.get("pages") or [])
        if isinstance(page, dict)
    ]


def source_names_in_page(page: dict) -> set[str]:
    names: set[str] = set()
    blobs: list[str] = []
    _walk_strings(page, blobs)
    for blob in blobs:
        data_source = re.search(r"\bdata_source\b", blob, re.I)
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_]", blob):
            names.add(match.group(1).lower())
        for match in re.finditer(
            r"(?:Sum|Count|Avg|Min|Max)\(\s*([A-Za-z_][A-Za-z0-9_]*)\.",
            blob,
            re.I,
        ):
            names.add(match.group(1).lower())
        if data_source:
            pass
    for element in page.get("elements") or []:
        if not isinstance(element, dict):
            continue
        ds = element.get("data_source")
        if isinstance(ds, str) and ds.strip():
            names.add(ds.strip().lower())
    return names


def variable_names_in_node(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, str):
        found.update(PLACEHOLDER_BRACE.findall(node))
    elif isinstance(node, dict):
        for value in node.values():
            found.update(variable_names_in_node(value))
    elif isinstance(node, list):
        for item in node:
            found.update(variable_names_in_node(item))
    return found


def build_page_pack(catalog: dict, index: int, resolved: dict[str, Any] | None = None) -> dict:
    pages = [page for page in (catalog.get("pages") or []) if isinstance(page, dict)]
    if index < 0 or index >= len(pages):
        raise KeyError(f"page index out of range: {index}")
    page = pages[index]
    wanted_sources = source_names_in_page(page)
    sources = []
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict):
            continue
        name = str(source.get("name") or "")
        if wanted_sources and name.lower() not in wanted_sources:
            continue
        item = {
            "name": name,
            "alias": source.get("alias"),
            "sql": source.get("sql"),
            "columns": source.get("columns"),
            "parameters": source.get("parameters"),
        }
        if resolved and source.get("sql"):
            item["sql_filled"] = fill_sql(str(source["sql"]), resolved)
        sources.append({key: value for key, value in item.items() if value not in (None, "", [])})
    if not sources:
        # page didn't name sources clearly — attach all sources so LLM can still query
        for source in catalog.get("sources") or []:
            if not isinstance(source, dict) or not source.get("sql"):
                continue
            item = {
                "name": source.get("name"),
                "alias": source.get("alias"),
                "sql": source.get("sql"),
            }
            if resolved:
                item["sql_filled"] = fill_sql(str(source["sql"]), resolved)
            sources.append(item)

    needed_vars = variable_names_in_node(page)
    for source in sources:
        needed_vars.update(braces_in_sql(str(source.get("sql") or "")))
        needed_vars.update(braces_in_sql(str(source.get("sql_filled") or "")))
    meta = variables_by_name(catalog)
    variables = [meta[name] for name in needed_vars if name in meta]
    # always include defaults for declared variables if page pack has none
    if not variables:
        variables = list(meta.values())

    return {
        "index": index,
        "page": page,
        "sources": sources,
        "variables": variables,
        "index_entry": page_index_entry(index, page),
    }


def resolve_page_ref(catalog: dict, ref: Any) -> int:
    pages = [page for page in (catalog.get("pages") or []) if isinstance(page, dict)]
    if isinstance(ref, bool):
        raise KeyError(f"bad page ref: {ref}")
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        index = int(ref)
        if 0 <= index < len(pages):
            return index
        raise KeyError(f"page index out of range: {ref}")
    text_ref = str(ref or "").strip().lower()
    if not text_ref:
        raise KeyError("empty page ref")
    for index, page in enumerate(pages):
        for key in ("name", "alias"):
            value = page.get(key)
            if isinstance(value, str) and value.strip().lower() == text_ref:
                return index
    raise KeyError(f"page not found: {ref}")


def write_page_packs(catalog: dict) -> Path:
    target = pages_dir()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    index = build_page_index(catalog)
    (target / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for entry in index:
        pack = build_page_pack(catalog, int(entry["index"]))
        path = target / f"{entry['index']}.json"
        path.write_text(json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("Page packs → %s (%s pages)", target, len(index))
    return target


def page_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_pages",
                "description": (
                    "Индекс всех pages/dashboard из .mrt. "
                    "Сначала пройдись по списку, потом load_page для нужных "
                    "(можно несколько или все — top-K нет, ничего не отбрасываем заранее)."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "load_page",
                "description": (
                    "Тянет полный pack страницы: elements/meters/filters/interaction "
                    "+ связанные sources и sql_filled. Вызывай для каждой нужной page."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page": {
                            "type": ["string", "integer"],
                            "description": "index (0,1,2…) или Name/Alias страницы",
                        }
                    },
                    "required": ["page"],
                },
            },
        },
    ]


def braces_in_sql(sql: str) -> list[str]:
    """Everything inside {…} is a variable name."""
    seen: set[str] = set()
    ordered: list[str] = []
    for name in PLACEHOLDER_BRACE.findall(sql):
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def variables_by_name(catalog: dict) -> dict[str, dict]:
    """variables[] from schema: name → alias / description / default (value)."""
    result: dict[str, dict] = {}
    for variable in catalog.get("variables") or []:
        if not isinstance(variable, dict) or not variable.get("name"):
            continue
        name = str(variable["name"])
        result[name] = {
            "name": name,
            "token": f"{{{name}}}",
            "alias": variable.get("alias"),
            "description": variable.get("description"),
            "default": variable.get("value"),
            "type": variable.get("type"),
        }
    return result


def query_templates(catalog: dict) -> list[dict]:
    """All SQL queries from the schema + {variables} used in each."""
    meta = variables_by_name(catalog)
    templates: list[dict] = []
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict):
            continue
        sql = source.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            continue
        used = []
        for name in braces_in_sql(sql):
            info = meta.get(name) or {"name": name, "token": f"{{{name}}}"}
            used.append(
                {
                    "name": name,
                    "token": f"{{{name}}}",
                    "alias": info.get("alias"),
                    "description": info.get("description"),
                    "default": info.get("default"),
                }
            )
        templates.append(
            {
                "source": source.get("name"),
                "sql": sql.strip(),
                "variables": used,
            }
        )
    return templates


def default_parameters(catalog: dict) -> list[dict]:
    return [
        {
            "token": item["token"],
            "name": item["name"],
            "alias": item.get("alias"),
            "description": item.get("description"),
            "value": item.get("default"),
        }
        for item in variables_by_name(catalog).values()
        if item.get("default") not in (None, "")
    ]


def needed_variables(catalog: dict) -> dict[str, dict]:
    """Variables that appear as {Name} in any query, with meta from variables[]."""
    meta = variables_by_name(catalog)
    needed: dict[str, dict] = {}
    for template in query_templates(catalog):
        for item in template.get("variables") or []:
            name = item["name"]
            if name in needed:
                continue
            needed[name] = meta.get(name) or {
                "name": name,
                "token": f"{{{name}}}",
                "default": None,
            }
    # also keep variables declared in schema even if unused in SQL text
    for name, info in meta.items():
        needed.setdefault(name, info)
    return needed


MONTH_ALIASES: dict[str, int] = {
    "январ": 1,
    "january": 1,
    "jan": 1,
    "феврал": 2,
    "february": 2,
    "feb": 2,
    "март": 3,
    "march": 3,
    "mar": 3,
    "апрел": 4,
    "april": 4,
    "apr": 4,
    "ма": 5,  # май/мая — handled carefully below
    "may": 5,
    "июн": 6,
    "june": 6,
    "jun": 6,
    "июл": 7,
    "july": 7,
    "jul": 7,
    "август": 8,
    "august": 8,
    "aug": 8,
    "сентябр": 9,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "октябр": 10,
    "october": 10,
    "oct": 10,
    "ноябр": 11,
    "november": 11,
    "nov": 11,
    "декабр": 12,
    "december": 12,
    "dec": 12,
}


def coerce_param_value(raw: str) -> Any:
    text = raw.strip().strip("\"'`")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+[.,]\d+", text):
        return float(text.replace(",", "."))
    return text


def looks_like_month_var(name: str, alias: Any) -> bool:
    labels = [name.lower()]
    if isinstance(alias, str) and alias.strip():
        labels.append(alias.strip().lower())
    return any(
        key in label
        for label in labels
        for key in ("month", "месяц", "period", "период")
    )


def parse_month_from_question(question: str) -> int | None:
    """'за февраль' / 'February' / 'месяц 2' / '02.2025' → month number."""
    text = question.lower()
    # numeric: месяц=2, month: 02, за 2 месяц
    numeric = re.search(
        r"(?i)\b(?:месяц(?:у|е|а)?|month)\b\s*[:=]?\s*(\d{1,2})\b",
        text,
    )
    if numeric:
        value = int(numeric.group(1))
        if 1 <= value <= 12:
            return value
    # date-like 02.2025 or 2025-02
    dated = re.search(r"(?<!\d)(\d{1,2})[./]((?:19|20)\d{2})(?!\d)", text)
    if dated:
        value = int(dated.group(1))
        if 1 <= value <= 12:
            return value
    dated_iso = re.search(r"(?<!\d)((?:19|20)\d{2})-(\d{1,2})(?!\d)", text)
    if dated_iso:
        value = int(dated_iso.group(2))
        if 1 <= value <= 12:
            return value
    # named months (ru/en stems)
    for stem, number in MONTH_ALIASES.items():
        if stem == "ма":
            if re.search(r"(?i)\bма[йяюе]\b", text):
                return 5
            continue
        if stem in text:
            return number
    return None


def extract_from_question(question: str, variables: dict[str, dict]) -> dict[str, Any]:
    """Pull values from the user question for each variable."""
    found: dict[str, Any] = {}
    for name, meta in variables.items():
        labels = [name]
        alias = meta.get("alias")
        if isinstance(alias, str) and alias.strip() and alias.strip().lower() != name.lower():
            labels.append(alias.strip())
        patterns: list[str] = [
            rf"(?i)\{{{re.escape(name)}\}}\s*[:=]?\s*([^\s,;]+)",
        ]
        for label in labels:
            patterns.extend(
                [
                    rf"(?i)\b{re.escape(label)}\b\s*[:=]\s*([^\s,;]+)",
                    rf"(?i)\b{re.escape(label)}\b\s+(?:is|равен|равна|=)?\s*([^\s,;]+)",
                    rf"(?i)\b{re.escape(label)}\b\s+(-?\d+(?:[.,]\d+)?)",
                ]
            )
        lower = name.lower()
        if lower == "year" or (isinstance(alias, str) and alias.lower() in {"год", "year"}):
            patterns.extend(
                [
                    r"(?i)\b(?:за|в|год(?:у)?)\s*[:=]?\s*((?:19|20)\d{2})\b",
                    r"(?i)\b((?:19|20)\d{2})\s*(?:год(?:у|а)?)?\b",
                ]
            )
        if lower == "category" or (isinstance(alias, str) and "категор" in str(alias).lower()):
            patterns.extend(
                [
                    r"(?i)\bкатегор\w*\s*(?:№|#|id)?\s*[:=]?\s*(-?\d+)",
                    r"(?i)\bcategory\s*(?:id|№|#)?\s*[:=]?\s*(-?\d+)",
                ]
            )
        if looks_like_month_var(name, alias):
            month = parse_month_from_question(question)
            if month is not None:
                found[name] = month
                continue
        for pattern in patterns:
            match = re.search(pattern, question)
            if not match:
                continue
            found[name] = coerce_param_value(match.group(1))
            break
    return found


def resolve_parameters(question: str, catalog: dict) -> dict[str, Any]:
    """For each {var}: value from question, else default from variables[]."""
    variables = needed_variables(catalog)
    extracted = extract_from_question(question, variables)
    resolved: dict[str, Any] = {}
    for name, meta in variables.items():
        token = meta.get("token") or f"{{{name}}}"
        default = meta.get("default")
        if name in extracted:
            resolved[name] = {
                "value": extracted[name],
                "source": "question",
                "token": token,
                "alias": meta.get("alias"),
                "default": default,
            }
        elif default not in (None, ""):
            resolved[name] = {
                "value": default,
                "source": "default",
                "token": token,
                "alias": meta.get("alias"),
                "default": default,
            }
        else:
            # declared but neither question nor default — still surface for debugging
            resolved[name] = {
                "value": None,
                "source": "missing",
                "token": token,
                "alias": meta.get("alias"),
                "default": None,
            }
    return resolved


def params_breakdown(resolved: dict[str, Any]) -> dict[str, Any]:
    """Split params for logs/API: defaults vs taken from the question vs final used."""
    defaults: dict[str, Any] = {}
    from_question: dict[str, Any] = {}
    used: dict[str, Any] = {}
    missing: list[str] = []
    for meta in resolved.values():
        token = meta.get("token") or ""
        alias = meta.get("alias")
        entry = {
            "value": meta.get("value"),
            "alias": alias,
            "default": meta.get("default"),
            "from": meta.get("source"),
        }
        if meta.get("default") not in (None, ""):
            defaults[token] = {
                "value": meta.get("default"),
                "alias": alias,
            }
        if meta.get("source") == "question":
            from_question[token] = {
                "value": meta.get("value"),
                "alias": alias,
                "overrode_default": meta.get("default"),
            }
        if meta.get("source") == "missing":
            missing.append(token)
        elif meta.get("value") is not None:
            used[token] = entry
    return {
        "defaults": defaults,
        "from_question": from_question,
        "used": used,
        "missing": missing,
    }


def log_parameters(question: str, resolved: dict[str, Any]) -> dict[str, Any]:
    breakdown = params_breakdown(resolved)
    log.info("PARAMS question=%s", preview(question, 200))
    log.info("PARAMS defaults=%s", preview(breakdown["defaults"], 500))
    log.info("PARAMS from_question=%s", preview(breakdown["from_question"], 500))
    log.info("PARAMS used=%s", preview(breakdown["used"], 500))
    if breakdown["missing"]:
        log.warning("PARAMS missing (no value, placeholder kept)=%s", breakdown["missing"])
    return breakdown


def queries_from_packs(packs: list[dict]) -> list[dict]:
    """SQL from selected page packs only — already filled with resolved params."""
    planned: list[dict] = []
    seen: set[str] = set()
    for pack in packs:
        page_i = pack.get("index")
        page_name = (pack.get("index_entry") or {}).get("name") or (pack.get("page") or {}).get("name")
        for source in pack.get("sources") or []:
            if not isinstance(source, dict):
                continue
            sql = source.get("sql_filled") or source.get("sql")
            name = str(source.get("name") or "query")
            if not isinstance(sql, str) or not sql.strip():
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            planned.append(
                {
                    "source": name,
                    "page": page_i,
                    "page_name": page_name,
                    "sql": to_postgres_sql(sql.strip()),
                    "sql_template": source.get("sql"),
                }
            )
    return planned


def pages_detail(catalog: dict, indexes: list[int] | None) -> list[dict]:
    """Human-readable selected pages for /ask and ask_trace."""
    index = build_page_index(catalog)
    by_i = {int(entry["index"]): entry for entry in index if "index" in entry}
    details: list[dict] = []
    for item in indexes or []:
        try:
            i = int(item)
        except (TypeError, ValueError):
            continue
        entry = by_i.get(i) or {"index": i}
        details.append(
            {
                "index": i,
                "name": entry.get("name"),
                "alias": entry.get("alias"),
                "titles": entry.get("titles"),
                "element_count": entry.get("element_count"),
            }
        )
    return details


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if re.fullmatch(r"-?\d+", text):
        return text
    if re.fullmatch(r"-?\d+[.,]\d+", text):
        return text.replace(",", ".")
    return "'" + text.replace("'", "''") + "'"


def fill_sql(sql: str, resolved: dict[str, Any]) -> str:
    """Substitute {Name} with resolved values. Leave the rest of SQL as-is."""
    filled = sql
    for name, meta in resolved.items():
        if meta.get("source") == "missing" or meta.get("value") is None:
            continue
        token = meta.get("token") or f"{{{name}}}"
        filled = filled.replace(token, sql_literal(meta["value"]))
    return filled


def filled_queries(catalog: dict, resolved: dict[str, Any]) -> list[dict]:
    queries = []
    for template in query_templates(catalog):
        sql = template.get("sql") or ""
        queries.append(
            {
                "source": template.get("source"),
                "sql_template": sql,
                "sql_filled": fill_sql(sql, resolved),
                "variables": template.get("variables") or [],
            }
        )
    return queries


def dump_filled_sql(queries: list[dict], question: str, resolved: dict[str, Any]) -> Path:
    """Write sql_filled to txt for debugging."""
    path = resolve_path(os.environ.get("SQL_DUMP_PATH", "sql_filled.txt"))
    blocks: list[str] = [
        f"-- question: {question}",
        "-- variables: "
        + json.dumps(
            {meta["token"]: meta["value"] for meta in resolved.values()},
            ensure_ascii=False,
        ),
        "",
    ]
    for item in queries:
        source = item.get("source") or "query"
        sql = (item.get("sql_filled") or "").strip()
        if not sql:
            continue
        blocks.append(f"-- source: {source}")
        blocks.append(sql if sql.endswith(";") else sql + ";")
        blocks.append("")
    path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")
    log.info("SQL dump → %s (%s queries)", path, len(queries))
    return path


def filters_payload(resolved: dict[str, Any]) -> dict:
    return {
        meta["token"]: {
            "value": meta["value"],
            "from": meta["source"],
            "alias": meta.get("alias"),
        }
        for meta in resolved.values()
    }


def dump_ask_trace(
    *,
    question: str,
    resolved: dict[str, Any],
    pages: list[Any] | None,
    queries: list[dict] | None,
    sql_results: list[dict] | None,
    answer: str,
    elapsed_s: float | None = None,
) -> Path:
    """Write full ask cycle: request → params → pages → sql → rows → answer."""
    path = resolve_path(os.environ.get("ASK_DUMP_PATH", "ask_trace.txt"))
    breakdown = params_breakdown(resolved)
    lines = [
        "=== ASK ===",
        f"question: {question}",
        "",
        "=== PARAMS defaults (из .mrt) ===",
        json.dumps(breakdown["defaults"], ensure_ascii=False, indent=2),
        "",
        "=== PARAMS from_question (подставленные из текста) ===",
        json.dumps(breakdown["from_question"], ensure_ascii=False, indent=2),
        "",
        "=== PARAMS used (что ушло в SQL) ===",
        json.dumps(breakdown["used"], ensure_ascii=False, indent=2),
        "",
        "=== PAGES ===",
        json.dumps(pages or [], ensure_ascii=False, indent=2),
        "",
        "=== SQL (с выбранных pages, уже с параметрами) ===",
    ]
    for item in queries or []:
        source = item.get("source") or "query"
        page = item.get("page_name") or item.get("page")
        sql = (item.get("sql") or item.get("sql_filled") or "").strip()
        lines.append(f"-- source: {source} page: {page}")
        if item.get("sql_template"):
            lines.append(f"-- template: {item['sql_template']}")
        if sql:
            lines.append(sql if sql.endswith(";") else sql + ";")
        lines.append("")
    lines.append("=== SQL RESULTS ===")
    for item in sql_results or []:
        lines.append(f"-- source: {item.get('source')} ok={item.get('ok')}")
        lines.append(str(item.get("result") or ""))
        lines.append("")
    lines.extend(["=== ANSWER ===", answer or "", ""])
    if elapsed_s is not None:
        lines.append(f"=== elapsed_s: {elapsed_s:.2f} ===")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    log.info("Ask trace → %s", path)
    return path


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def to_postgres_sql(sql: str) -> str:
    return re.sub(r"`([^`]+)`", r'"\1"', sql)


def compact_pack_for_planner(pack: dict) -> dict:
    """Shrink pack for planner context: keep widgets + sql_filled."""
    page = pack.get("page") or {}
    elements = []
    for element in page.get("elements") or []:
        if not isinstance(element, dict):
            continue
        elements.append(
            {
                "name": element.get("name"),
                "type": element.get("type"),
                "role": element.get("role"),
                "title": element.get("title"),
                "text": element.get("text"),
                "meters": element.get("meters"),
                "filters": element.get("filters"),
                "top_n": element.get("top_n"),
                "expressions": element.get("expressions"),
                "interaction": element.get("interaction"),
            }
        )
    sources = []
    for source in pack.get("sources") or []:
        if not isinstance(source, dict):
            continue
        sources.append(
            {
                "name": source.get("name"),
                "sql_filled": source.get("sql_filled") or source.get("sql"),
            }
        )
    return {
        "index": pack.get("index"),
        "name": page.get("name"),
        "alias": page.get("alias"),
        "elements": elements,
        "sources": sources,
        "variables": pack.get("variables"),
    }


def chat_tools(tools) -> list[dict]:
    converted = []
    for tool in tools:
        schema = dict(tool.input_schema or {"type": "object", "properties": {}})
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            }
        )
    return converted


def parse_arguments(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must be a JSON object")
        return parsed
    if isinstance(raw, dict):
        return raw
    raise ValueError("tool arguments must be an object")


def pending_tool_calls(message: dict) -> tuple[dict, list[tuple[str, str, Any]]]:
    stored = []
    pending = []
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        name = function.get("name") or ""
        call_id = call.get("id") or f"call_{index}"
        arguments = function.get("arguments")
        stored.append(
            {
                "id": call_id,
                "type": call.get("type") or "function",
                "function": {
                    "name": name,
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments or {}),
                },
            }
        )
        pending.append((call_id, name, arguments))
    return {
        "role": "assistant",
        "content": message.get("content"),
        "tool_calls": stored,
    }, pending


def render_tool_result(result) -> str:
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    body = "\n".join(parts) if parts else str(result)
    if getattr(result, "is_error", False):
        body = f"SQL error: {body}"
    if len(body) > MAX_TOOL_CHARS:
        body = body[:MAX_TOOL_CHARS] + "\n...truncated"
    return body


def run_page_tool(
    name: str,
    arguments: dict,
    *,
    catalog: dict,
    resolved: dict[str, Any],
) -> str:
    if name == "list_pages":
        payload = {"pages": build_page_index(catalog)}
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        log.info("local tool list_pages → %s pages", len(payload["pages"]))
        return body
    if name == "load_page":
        ref = arguments.get("page", arguments.get("index", arguments.get("name")))
        try:
            index = resolve_page_ref(catalog, ref)
            pack = build_page_pack(catalog, index, resolved=resolved)
        except KeyError as exc:
            return f"Page error: {exc}"
        body = json.dumps(pack, ensure_ascii=False, indent=2)
        if len(body) > MAX_TOOL_CHARS:
            body = body[:MAX_TOOL_CHARS] + "\n...truncated"
        log.info(
            "local tool load_page ref=%s → index=%s sources=%s chars=%s",
            ref,
            index,
            len(pack.get("sources") or []),
            len(body),
        )
        return body
    return f"Unknown local tool: {name}"


async def run_tool(
    session: ClientSession,
    name: str,
    arguments: dict,
    known: set[str],
    *,
    catalog: dict | None = None,
    resolved: dict[str, Any] | None = None,
) -> str:
    if name in LOCAL_PAGE_TOOLS:
        return run_page_tool(name, arguments, catalog=catalog or {}, resolved=resolved or {})
    if name not in known:
        log.warning("Неизвестный tool: %s", name)
        return f"Unknown tool: {name}"
    log.info("MCP tool %s args=%s", name, preview(arguments))
    started = time.perf_counter()
    try:
        result = await session.call_tool(name, arguments)
    except Exception as exc:
        log.exception("MCP tool %s упал: %s", name, exc)
        return f"SQL error: {exc}"
    body = render_tool_result(result)
    log.info(
        "MCP tool %s готово за %.2fs error=%s result=%s",
        name,
        time.perf_counter() - started,
        getattr(result, "is_error", False),
        preview(body),
    )
    return body


def message_text(message: dict) -> str:
    """Pull visible text from OpenAI-style and local-server quirks."""
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        parts.append(content.strip())
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    for key in ("reasoning_content", "reasoning", "refusal"):
        value = message.get(key)
        if isinstance(value, str) and value.strip() and key != "reasoning_content":
            parts.append(value.strip())
    return "\n".join(parts).strip()


async def llm_chat(
    llm: httpx.AsyncClient,
    *,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    round_no: int | str,
    allow_tools: bool,
) -> dict:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if allow_tools and tools:
        payload["tools"] = tools
    else:
        # After SQL results local models often return empty content if tools stay enabled.
        payload["tool_choice"] = "none"
    log.info(
        "LLM round %s → %s model=%s messages=%s tools=%s",
        round_no,
        llm_base_url(),
        model,
        len(messages),
        allow_tools,
    )
    started = time.perf_counter()
    response = await llm.post("chat/completions", json=payload)
    response.raise_for_status()
    body = response.json()
    try:
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        log.error("LLM raw payload без choices: %s", preview(body, 800))
        raise RuntimeError("LLM response has no choices") from exc
    finish = choice.get("finish_reason")
    assistant, pending = pending_tool_calls(message)
    text = message_text(message)
    log.info(
        "LLM round %s за %.2fs finish=%s tool_calls=%s answer=%s",
        round_no,
        time.perf_counter() - started,
        finish,
        [name for _, name, _ in pending] or None,
        preview(text, 200) or None,
    )
    if not pending and not text:
        log.warning("Пустой message от LLM: %s", preview(message, 800))
    return {"assistant": assistant, "pending": pending, "text": text, "raw": message}


async def llm_text(
    llm: httpx.AsyncClient,
    *,
    model: str,
    system: str,
    user: str,
    round_no: str,
) -> str:
    result = await llm_chat(
        llm,
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=None,
        round_no=round_no,
        allow_tools=False,
    )
    return result["text"]


async def route_pages(
    llm: httpx.AsyncClient,
    *,
    model: str,
    catalog: dict,
    question: str,
    resolved: dict[str, Any],
) -> list[int]:
    """Stage 1: pick pages from the full index. Fallback = all pages."""
    pages = [page for page in (catalog.get("pages") or []) if isinstance(page, dict)]
    if not pages:
        return []
    if len(pages) == 1:
        return [0]
    index = build_page_index(catalog)
    system = (
        "Ты роутер Stimulsoft-дашборда. По вопросу выбери какие pages нужны.\n"
        "Верни ТОЛЬКО JSON вида {\"pages\":[0,2]} — индексы из списка.\n"
        "Если не уверен — верни все индексы. Никакого текста кроме JSON."
    )
    user = (
        f"Вопрос: {question}\n"
        f"Фильтры: {json.dumps(filters_payload(resolved), ensure_ascii=False)}\n"
        f"Pages index:\n{json.dumps(index, ensure_ascii=False, indent=2)}"
    )
    raw = await llm_text(llm, model=model, system=system, user=user, round_no="route")
    parsed = extract_json_object(raw) or {}
    selected: list[int] = []
    for item in parsed.get("pages") or []:
        try:
            selected.append(resolve_page_ref(catalog, item))
        except KeyError:
            continue
    # unique preserve order
    ordered: list[int] = []
    for index in selected:
        if index not in ordered:
            ordered.append(index)
    if not ordered:
        log.warning("Router не выбрал pages — беру все %s", len(pages))
        return list(range(len(pages)))
    log.info("Router pages=%s", ordered)
    return ordered


async def plan_queries(
    llm: httpx.AsyncClient,
    *,
    model: str,
    question: str,
    resolved: dict[str, Any],
    packs: list[dict],
) -> list[dict]:
    """Stage 2: choose SQL from loaded packs. Fallback = all sql_filled."""
    compact = [compact_pack_for_planner(pack) for pack in packs]
    system = (
        "Ты планировщик SQL по Stimulsoft page packs.\n"
        "Выбери 1..N запросов для ответа. Верни ТОЛЬКО JSON:\n"
        '{"queries":[{"source":"name","sql":"SELECT ..."}]}\n'
        "Бери sql_filled из packs; при необходимости слегка поправь под PostgreSQL "
        "(`order details` → \"order details\"). Не выдумывай таблицы."
    )
    user = (
        f"Вопрос: {question}\n"
        f"Фильтры: {json.dumps(filters_payload(resolved), ensure_ascii=False)}\n"
        f"Packs:\n{json.dumps(compact, ensure_ascii=False, indent=2)}"
    )
    raw = await llm_text(llm, model=model, system=system, user=user, round_no="plan")
    parsed = extract_json_object(raw) or {}
    planned: list[dict] = []
    for item in parsed.get("queries") or []:
        if not isinstance(item, dict):
            continue
        sql = item.get("sql") or item.get("sql_filled")
        if not isinstance(sql, str) or not sql.strip():
            continue
        planned.append(
            {
                "source": item.get("source") or item.get("name") or "query",
                "sql": to_postgres_sql(sql.strip()),
            }
        )
    if planned:
        log.info("Planner queries=%s", [item["source"] for item in planned])
        return planned

    fallback: list[dict] = []
    seen: set[str] = set()
    for pack in packs:
        for source in pack.get("sources") or []:
            if not isinstance(source, dict):
                continue
            sql = source.get("sql_filled") or source.get("sql")
            name = str(source.get("name") or "query")
            if not isinstance(sql, str) or not sql.strip():
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            fallback.append({"source": name, "sql": to_postgres_sql(sql.strip())})
    log.warning("Planner пуст — fallback %s sources", len(fallback))
    return fallback


async def mcp_execute_sql(session: ClientSession, sql: str) -> dict:
    """Stage 3: host runs SQL via Postgres MCP."""
    last_error = ""
    for key in ("sql", "query"):
        try:
            result = await session.call_tool("execute_sql", {key: sql})
            body = render_tool_result(result)
            ok = not getattr(result, "is_error", False) and not body.startswith("SQL error:")
            return {"ok": ok, "sql": sql, "result": body, "arg": key}
        except Exception as exc:
            last_error = str(exc)
            log.warning("execute_sql arg=%s failed: %s", key, exc)
    return {"ok": False, "sql": sql, "result": f"SQL error: {last_error}", "arg": None}


async def answer_from_rows(
    llm: httpx.AsyncClient,
    *,
    model: str,
    question: str,
    resolved: dict[str, Any],
    packs: list[dict],
    results: list[dict],
) -> str:
    """Stage 4: final natural-language answer from SQL rows + widget hints."""
    widgets = []
    for pack in packs:
        compact = compact_pack_for_planner(pack)
        widgets.append(
            {
                "page": compact.get("name") or compact.get("alias"),
                "elements": compact.get("elements"),
            }
        )
    system = (
        "Ты аналитик Stimulsoft-дашборда. Хост уже выполнил SQL.\n"
        "По строкам результата и логике виджетов (meters/агрегации/фильтры/TopN) "
        "ответь на вопрос. Не вызывай инструменты. Не выдумывай числа. "
        "Ответь на языке вопроса."
    )
    user = (
        f"Вопрос: {question}\n"
        f"Фильтры: {json.dumps(filters_payload(resolved), ensure_ascii=False)}\n"
        f"Виджеты:\n{json.dumps(widgets, ensure_ascii=False, indent=2)}\n"
        f"Результат SQL:\n{json.dumps(results, ensure_ascii=False, indent=2)}"
    )
    text = await llm_text(llm, model=model, system=system, user=user, round_no="answer")
    if text:
        return text
    # last resort: dump SQL results
    chunks = [item.get("result") for item in results if item.get("result")]
    if chunks:
        return "Модель не сформулировала ответ. Данные SQL:\n" + "\n\n".join(chunks)
    raise RuntimeError("model returned an empty answer")


async def answer_question(
    *,
    session: ClientSession,
    llm: httpx.AsyncClient,
    model: str,
    question: str,
    resolved: dict[str, Any],
    catalog: dict,
) -> dict[str, Any]:
    """route pages → SQL from those pages (params filled) → MCP → answer."""
    breakdown = log_parameters(question, resolved)
    log.info(
        "Pipeline start pages_total=%s",
        len(catalog.get("pages") or []),
    )

    page_indexes = await route_pages(
        llm, model=model, catalog=catalog, question=question, resolved=resolved
    )
    selected = pages_detail(catalog, page_indexes)
    log.info("Selected pages=%s", preview(selected, 400))

    packs = [build_page_pack(catalog, index, resolved=resolved) for index in page_indexes]
    planned = queries_from_packs(packs)
    log.info(
        "SQL from pages: %s",
        preview(
            [
                {
                    "source": item.get("source"),
                    "page": item.get("page_name") or item.get("page"),
                    "sql": item.get("sql"),
                }
                for item in planned
            ],
            800,
        ),
    )
    if not planned:
        raise RuntimeError("нет SQL на выбранных pages")

    results: list[dict] = []
    for item in planned:
        log.info(
            "Execute page=%s source=%s: %s",
            item.get("page_name") or item.get("page"),
            item.get("source"),
            preview(item.get("sql"), 200),
        )
        executed = await mcp_execute_sql(session, item["sql"])
        results.append(
            {
                "source": item.get("source"),
                "page": item.get("page"),
                "page_name": item.get("page_name"),
                "sql": item.get("sql"),
                "ok": executed.get("ok"),
                "result": executed.get("result"),
            }
        )

    answer = await answer_from_rows(
        llm,
        model=model,
        question=question,
        resolved=resolved,
        packs=packs,
        results=results,
    )
    return {
        "answer": answer,
        "pages": page_indexes,
        "pages_detail": selected,
        "params": breakdown,
        "queries": planned,
        "sql_results": results,
    }


def mcp_database_uri() -> str:
    """From the MCP container reach Postgres published on the host."""
    url = database_url()
    for host in ("127.0.0.1", "localhost"):
        url = url.replace(f"@{host}:", "@host.docker.internal:")
        url = url.replace(f"//{host}:", "//host.docker.internal:")
    return url


def postgres_mcp() -> StdioServerParameters:
    """Postgres MCP Pro, as in the Habr / Claude Desktop setup."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Нужен Docker для crystaldba/postgres-mcp")
    uri = mcp_database_uri()
    log.info("MCP: docker run crystaldba/postgres-mcp restricted uri=%s", redact_url(uri))
    return StdioServerParameters(
        command=docker,
        args=[
            "run",
            "-i",
            "--rm",
            "-e",
            f"DATABASE_URI={uri}",
            "crystaldba/postgres-mcp",
            "--access-mode=restricted",
        ],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("Нужна LLM_MODEL")
    if not database_url():
        raise RuntimeError("Нужны DATABASE_URL или POSTGRES_USER/PASSWORD/DB")
    log.info("Старт host: llm=%s model=%s db=%s", llm_base_url(), model, redact_url(database_url()))
    catalog = ensure_catalog()
    templates = query_templates(catalog)
    defaults = default_parameters(catalog)
    page_index = build_page_index(catalog)
    log.info(
        "Каталог: report=%s pages=%s sources=%s defaults=%s",
        (catalog.get("report") or {}).get("name"),
        len(page_index),
        len(templates),
        defaults,
    )
    for entry in page_index:
        log.info(
            "page[%s] %s alias=%s elements=%s",
            entry.get("index"),
            entry.get("name"),
            entry.get("alias"),
            entry.get("element_count"),
        )
    headers = {}
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = postgres_mcp()
    log.info("Поднимаю Postgres MCP Pro…")
    async with httpx.AsyncClient(base_url=llm_base_url(), headers=headers, timeout=180) as llm:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tools = chat_tools(listed.tools)
                log.info("MCP готов, tools=%s", [tool["function"]["name"] for tool in tools])
                app.state.session = session
                app.state.llm = llm
                app.state.model = model
                app.state.tools = tools
                app.state.catalog = catalog
                app.state.defaults = defaults
                app.state.queries = templates
                app.state.page_index = page_index
                app.state.report = (catalog.get("report") or {}).get("name")
                app.state.lock = asyncio.Lock()
                log.info("Host готов принимать /ask (params→route pages→SQL from pages→MCP→answer)")
                yield
                log.info("Host останавливается, MCP закроется")


load_env()
setup_logging()
app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "report": app.state.report,
        "tools": [tool["function"]["name"] for tool in app.state.tools],
        "defaults": app.state.defaults,
        "pages": app.state.page_index,
        "queries": [item.get("source") for item in app.state.queries],
        "llm": llm_base_url(),
        "mcp": "docker run -i --rm crystaldba/postgres-mcp --access-mode=restricted",
    }


@app.post("/ask")
async def ask(body: Ask) -> dict:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")
    resolved = resolve_parameters(question, app.state.catalog)
    dump_filled_sql(filled_queries(app.state.catalog, resolved), question, resolved)
    log.info(
        "POST /ask question=%s variables=%s pages_total=%s",
        preview(question, 200),
        preview(filters_payload(resolved), 400),
        len(app.state.page_index or []),
    )
    started = time.perf_counter()
    async with app.state.lock:
        try:
            result = await answer_question(
                session=app.state.session,
                llm=app.state.llm,
                model=app.state.model,
                question=question,
                resolved=resolved,
                catalog=app.state.catalog,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            log.exception("Ошибка на /ask: %s", exc)
            raise HTTPException(status_code=502, detail=f"ask failed: {exc}") from exc
    answer = result.get("answer") or ""
    if not answer:
        log.error("Пустой ответ модели")
        raise HTTPException(status_code=502, detail="model returned an empty answer")
    elapsed = time.perf_counter() - started
    selected_pages = result.get("pages_detail") or pages_detail(app.state.catalog, result.get("pages"))
    params = result.get("params") or params_breakdown(resolved)
    dump_ask_trace(
        question=question,
        resolved=resolved,
        pages=selected_pages,
        queries=result.get("queries"),
        sql_results=result.get("sql_results"),
        answer=answer,
        elapsed_s=elapsed,
    )
    log.info(
        "Ответ за %.2fs pages=%s from_question=%s defaults=%s: %s",
        elapsed,
        preview(selected_pages, 200),
        preview(params.get("from_question"), 200),
        preview(params.get("defaults"), 200),
        preview(answer, 300),
    )
    return {
        "answer": answer,
        "params": params,
        "pages": result.get("pages"),
        "pages_detail": selected_pages,
        "queries": [
            {
                "source": item.get("source"),
                "page": item.get("page"),
                "page_name": item.get("page_name"),
                "sql": item.get("sql"),
                "sql_template": item.get("sql_template"),
            }
            for item in (result.get("queries") or [])
        ],
        "sql_results": [
            {
                "source": item.get("source"),
                "page_name": item.get("page_name"),
                "ok": item.get("ok"),
                "sql": item.get("sql"),
                "preview": preview(item.get("result"), 500),
            }
            for item in (result.get("sql_results") or [])
        ],
        "trace": str(resolve_path(os.environ.get("ASK_DUMP_PATH", "ask_trace.txt"))),
    }


def main() -> None:
    load_env()
    setup_logging()
    missing = []
    if not database_url():
        missing.append("POSTGRES_USER/PASSWORD/DB")
    if not os.environ.get("LLM_BASE_URL", "").strip():
        missing.append("LLM_BASE_URL")
    if not os.environ.get("LLM_MODEL", "").strip():
        missing.append("LLM_MODEL")
    if missing:
        raise SystemExit(f"Задай {', '.join(missing)}")
    if shutil.which("docker") is None:
        raise SystemExit("Нужен Docker для Postgres и crystaldba/postgres-mcp")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8090"))
    log.info("Слушаю http://%s:%s", host, port)
    uvicorn.run(
        "host:app",
        host=host,
        port=port,
    )


if __name__ == "__main__":
    main()
