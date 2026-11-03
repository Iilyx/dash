#!/usr/bin/env python3
"""Ask pipeline:

1) on start: MRT_PATH → catalog.json + catalog_pages/ (pack per page)
2) /ask: LLM sees index of ALL pages; load_page pulls packs when needed
3) {Name} from question, else default; MCP execute_sql → answer
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


def coerce_param_value(raw: str) -> Any:
    text = raw.strip().strip("\"'`")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+[.,]\d+", text):
        return float(text.replace(",", "."))
    return text


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
            }
        elif default not in (None, ""):
            resolved[name] = {
                "value": default,
                "source": "default",
                "token": token,
                "alias": meta.get("alias"),
            }
    return resolved


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


def system_prompt(catalog: dict) -> str:
    """Compact prompt: full page index only. Page bodies loaded via load_page."""
    index = build_page_index(catalog)
    variables = list(variables_by_name(catalog).values())
    report = catalog.get("report") or {}
    return (
        "Ты отвечаешь на вопросы по Stimulsoft-дашборду/отчёту.\n"
        "Полный catalog в контекст не помещается. Поэтому:\n"
        "- ниже INDEX всех pages (ничего не отфильтровано заранее);\n"
        "- list_pages — ещё раз получить индекс;\n"
        "- load_page — тянуть полный pack нужной страницы "
        "(elements/meters/filters/TopN/interaction + sources/sql_filled). "
        "Можно грузить несколько pages или все подряд, если не уверен;\n"
        "- затем execute_sql по sql_filled из загруженных packs "
        "(MySQL→PostgreSQL: `order details` → \"order details\").\n"
        "Фильтры {Name} уже в сообщении пользователя (вопрос или default).\n"
        "Не выдумывай числа. Ответь на языке вопроса.\n\n"
        f"report: {json.dumps(report, ensure_ascii=False)}\n\n"
        f"variables (defaults): {json.dumps(variables, ensure_ascii=False)}\n\n"
        f"pages index (все):\n{json.dumps(index, ensure_ascii=False, indent=2)}"
    )


def user_message(question: str, resolved: dict[str, Any]) -> str:
    filters = {
        meta["token"]: {"value": meta["value"], "from": meta["source"]}
        for meta in resolved.values()
    }
    return (
        f"{question}\n\n"
        "Фильтры (из вопроса пользователя или default variables):\n"
        f"{json.dumps(filters, ensure_ascii=False, indent=2)}\n\n"
        "Сначала list_pages / просмотри index, затем load_page для нужных pages "
        "(сколько надо — хоть все), потом execute_sql."
    )


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


async def answer_question(
    *,
    session: ClientSession,
    llm: httpx.AsyncClient,
    model: str,
    tools: list[dict],
    prompt: str,
    question: str,
    resolved: dict[str, Any],
    catalog: dict,
) -> str:
    known = {tool["function"]["name"] for tool in tools}
    messages: list[dict] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_message(question, resolved)},
    ]
    used_tools = False
    empty_retries = 0
    log.info(
        "Вопрос: %s | variables=%s | pages=%s | tools=%s",
        preview(question, 200),
        preview({meta["token"]: meta["value"] for meta in resolved.values()}, 300),
        len(catalog.get("pages") or []),
        sorted(known),
    )
    # more rounds: list_pages + several load_page + execute_sql
    for round_no in range(1, MAX_TOOL_ROUNDS + 8):
        result = await llm_chat(
            llm,
            model=model,
            messages=messages,
            tools=tools,
            round_no=round_no,
            allow_tools=True,
        )
        pending = result["pending"]
        text = result["text"]
        if not pending:
            if text:
                return text
            if used_tools and empty_retries < 1:
                empty_retries += 1
                log.warning("После инструментов модель молчит — прошу ответить")
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Инструменты уже отработали. По их результату ответь на исходный вопрос "
                            "кратко и по делу. Не вызывай инструменты. Не оставляй ответ пустым."
                        ),
                    }
                )
                forced = await llm_chat(
                    llm,
                    model=model,
                    messages=messages,
                    tools=None,
                    round_no=f"{round_no}-final",
                    allow_tools=False,
                )
                if forced["text"]:
                    return forced["text"]
                for item in reversed(messages):
                    if item.get("role") == "tool" and item.get("content"):
                        log.warning("Отдаю сырой результат tool как ответ")
                        return f"Модель не сформулировала ответ. Данные:\n{item['content']}"
            raise RuntimeError("model returned an empty answer")
        used_tools = True
        messages.append(result["assistant"])
        for call_id, name, raw_arguments in pending:
            try:
                arguments = parse_arguments(raw_arguments)
            except (ValueError, json.JSONDecodeError) as exc:
                log.warning("Плохие args для %s: %s", name, exc)
                tool_text = f"Bad tool arguments: {exc}"
            else:
                tool_text = await run_tool(
                    session,
                    name,
                    arguments,
                    known,
                    catalog=catalog,
                    resolved=resolved,
                )
            messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_text})
    log.error("Слишком много раундов для вопроса: %s", preview(question, 200))
    return "Модель не закончила ответ: слишком много вызовов инструментов."


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
    prompt = system_prompt(catalog)
    log.info(
        "Промпт: report=%s pages=%s sources=%s defaults=%s prompt_chars=%s",
        (catalog.get("report") or {}).get("name"),
        len(page_index),
        len(templates),
        defaults,
        len(prompt),
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
                tools = page_tools() + chat_tools(listed.tools)
                log.info("MCP готов, tools=%s", [tool["function"]["name"] for tool in tools])
                app.state.session = session
                app.state.llm = llm
                app.state.model = model
                app.state.tools = tools
                app.state.prompt = prompt
                app.state.catalog = catalog
                app.state.defaults = defaults
                app.state.queries = templates
                app.state.page_index = page_index
                app.state.report = (catalog.get("report") or {}).get("name")
                app.state.lock = asyncio.Lock()
                log.info("Host готов принимать /ask")
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
    # debug dump of all filled SQL; LLM still loads pages on demand
    queries = filled_queries(app.state.catalog, resolved)
    dump_path = dump_filled_sql(queries, question, resolved)
    log.info(
        "POST /ask question=%s variables=%s pages=%s sql_dump=%s",
        preview(question, 200),
        preview({meta["token"]: meta for meta in resolved.values()}, 400),
        len(app.state.page_index or []),
        dump_path,
    )
    started = time.perf_counter()
    async with app.state.lock:
        try:
            answer = await answer_question(
                session=app.state.session,
                llm=app.state.llm,
                model=app.state.model,
                tools=app.state.tools,
                prompt=app.state.prompt,
                question=question,
                resolved=resolved,
                catalog=app.state.catalog,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            log.exception("Ошибка на /ask: %s", exc)
            raise HTTPException(status_code=502, detail=f"LLM: {exc}") from exc
    if not answer:
        log.error("Пустой ответ модели")
        raise HTTPException(status_code=502, detail="model returned an empty answer")
    log.info("Ответ за %.2fs: %s", time.perf_counter() - started, preview(answer, 300))
    return {
        "answer": answer,
        "variables": {
            meta["token"]: {"value": meta["value"], "from": meta["source"]}
            for meta in resolved.values()
        },
        "pages_indexed": len(app.state.page_index or []),
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
