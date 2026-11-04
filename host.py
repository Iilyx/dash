#!/usr/bin/env python3
"""FastAPI ask:

1) LLM: определить страницу
2) пробросить всю page → Category / Filters → значения из вопроса или дефолты
3) по name из page найти sources → SQL
4) MCP execute_sql → ответ
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

from extract_mrt import catalog_from_report, load_mrt, write_page_packs

ROOT = Path(__file__).resolve().parent
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
PLACEHOLDER_BRACE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
PLACEHOLDER_AT = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
MAX_TOOL_CHARS = 24_000
LOG_PREVIEW = 400
log = logging.getLogger("host")


class Ask(BaseModel):
    question: str = Field(min_length=1)


def setup_logging() -> None:
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


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
    return path if path.is_absolute() else ROOT / path


def preview(value: Any, limit: int = LOG_PREVIEW) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def redact_url(url: str) -> str:
    return re.sub(r":([^:@/]+)@", r":***@", url)


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
    raw = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1").strip().rstrip("/")
    if not raw.endswith("/v1"):
        raw = f"{raw}/v1"
    return raw


def llm_timeout() -> float:
    return float(os.environ.get("LLM_TIMEOUT", "120").strip() or "120")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def llm_thinking_options() -> dict[str, Any]:
    effort = os.environ.get("LLM_REASONING_EFFORT", "low").strip().lower() or "low"
    if effort in {"none", "off", "false", "0"}:
        enable = False
        effort = "none"
    else:
        enable = _env_bool("LLM_ENABLE_THINKING", True)
        if effort not in {"low", "medium", "high", "xhigh", "minimal", "max"}:
            log.warning("Unknown LLM_REASONING_EFFORT=%s, using low", effort)
            effort = "low"

    opts: dict[str, Any] = {
        "enable_thinking": enable,
        "chat_template_kwargs": {"enable_thinking": enable},
    }
    if enable and effort != "none":
        opts["reasoning_effort"] = effort
    budget = os.environ.get("LLM_THINKING_BUDGET", "").strip()
    if enable and budget.isdigit():
        opts.pop("reasoning_effort", None)
        opts["thinking_budget"] = int(budget)
    return opts


def ensure_catalog() -> None:
    catalog_path = resolve_path(os.environ.get("CATALOG_PATH", "catalog.json"))
    pages_path = resolve_path(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages"))
    mrt_raw = os.environ.get("MRT_PATH", "SalesOfProducts.mrt").strip()
    mrt_path = resolve_path(mrt_raw)
    if mrt_path.is_file():
        log.info("Extract %s", mrt_path)
        catalog = catalog_from_report(load_mrt(mrt_path))
        catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        write_page_packs(catalog, pages_path)
        return
    if catalog_path.is_file():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        write_page_packs(catalog, pages_path)
        return
    raise RuntimeError("Нужен MRT_PATH или catalog.json")


def _add_text(bucket: list[str], value: Any) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text and text not in bucket:
            bucket.append(text)


def collect_texts(node: Any, bucket: list[str]) -> None:
    if isinstance(node, dict):
        for key in ("alias", "title", "text", "label", "name", "description", "expression"):
            if key in node:
                _add_text(bucket, node.get(key))
        for value in node.values():
            collect_texts(value, bucket)
    elif isinstance(node, list):
        for item in node:
            collect_texts(item, bucket)


def page_label(page: dict, index: int) -> str:
    alias = page.get("alias")
    name = page.get("name")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    if isinstance(name, str) and name.strip():
        return name.strip()
    return f"page_{index}"


def load_page_cards() -> list[dict[str, Any]]:
    pages_dir = resolve_path(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages"))
    cards: list[dict[str, Any]] = []
    paths = sorted(
        pages_dir.glob("*.json"),
        key=lambda p: int(p.stem) if p.stem.isdigit() else p.stem,
    )
    for path in paths:
        if not path.stem.isdigit():
            continue
        index = int(path.stem)
        catalog = json.loads(path.read_text(encoding="utf-8"))
        pages = catalog.get("pages") or []
        page = pages[0] if pages and isinstance(pages[0], dict) else {}
        texts: list[str] = []
        collect_texts(page, texts)
        ru = [t for t in texts if CYRILLIC.search(t)]
        other = [t for t in texts if t not in ru]
        cards.append(
            {
                "index": index,
                "page": page_label(page, index),
                "name": page.get("name"),
                "alias": page.get("alias"),
                "catalog": str(path),
                "ru_text": (ru + other)[:40],
            }
        )
    if not cards:
        raise RuntimeError(f"нет page-файлов в {pages_dir}")
    return cards


def load_page_catalog(index: int) -> dict:
    path = resolve_path(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages")) / f"{index}.json"
    if not path.is_file():
        raise KeyError(f"нет catalog_pages/{index}.json")
    return json.loads(path.read_text(encoding="utf-8"))


# --- params / SQL fill -------------------------------------------------------


def braces_in_sql(sql: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in PLACEHOLDER_BRACE.findall(sql):
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def ats_in_sql(sql: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in PLACEHOLDER_AT.findall(sql):
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def parameters_by_name(catalog: dict) -> dict[str, dict]:
    """Дефолты из sources[].parameters (@Name) и variables[] ({Name})."""
    result: dict[str, dict] = {}
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict):
            continue
        for param in source.get("parameters") or []:
            if not isinstance(param, dict) or not param.get("name"):
                continue
            name = str(param["name"])
            result[name] = {
                "name": name,
                "token": f"@{name}",
                "alias": param.get("alias") or name,
                "description": param.get("description"),
                "default": param.get("expression"),
                "type": param.get("type"),
                "kind": "at",
            }
    for variable in catalog.get("variables") or []:
        if not isinstance(variable, dict) or not variable.get("name"):
            continue
        name = str(variable["name"])
        if name in result:
            continue
        result[name] = {
            "name": name,
            "token": f"{{{name}}}",
            "alias": variable.get("alias"),
            "description": variable.get("description"),
            "default": variable.get("value"),
            "type": variable.get("type"),
            "kind": "brace",
        }
    return result


def _is_category_token(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return "categor" in text.lower()


def collect_category_filters(page: dict, catalog: dict) -> list[dict[str, Any]]:
    """Вся page + связанные sources: всё, где Category / Filters."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        key = f"{item.get('kind')}:{item.get('name')}:{item.get('where')}"
        if key in seen:
            return
        seen.add(key)
        found.append(item)

    # 1) элементы page: role=filter или любой узел с Category
    def walk_page(node: Any, path: str = "page") -> None:
        if isinstance(node, dict):
            role = str(node.get("role") or "").lower()
            name = node.get("name")
            title = node.get("title")
            blob = json.dumps(node, ensure_ascii=False)
            is_filter = role == "filter" or "filter" in path.lower()
            has_category = _is_category_token(blob)
            if is_filter and has_category:
                add(
                    {
                        "kind": "page_filter",
                        "name": name,
                        "title": title,
                        "role": role or "filter",
                        "where": path,
                        "param": "Category",
                        "default": None,
                    }
                )
            elif has_category and any(
                _is_category_token(str(node.get(k) or ""))
                for k in ("name", "title", "alias", "expression", "text", "label")
            ):
                add(
                    {
                        "kind": "page_category",
                        "name": name,
                        "title": title,
                        "role": role or None,
                        "where": path,
                        "param": "Category",
                        "default": None,
                    }
                )
            for key, value in node.items():
                walk_page(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk_page(item, f"{path}[{i}]")

    walk_page(page)

    # 2) sources: parameters / @Category в SQL (особенно для sources, связанных с page)
    page_names = {n.lower() for n in source_names_in_page(page)}
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict):
            continue
        src_name = str(source.get("name") or "")
        sql = str(source.get("sql") or "")
        on_page = not page_names or src_name.lower() in page_names or _is_category_token(src_name)
        if not on_page and not _is_category_token(sql):
            # всё равно берём param Category из любого source — это фильтр отчёта
            pass
        for param in source.get("parameters") or []:
            if not isinstance(param, dict) or not param.get("name"):
                continue
            pname = str(param["name"])
            if not _is_category_token(pname) and not _is_category_token(sql):
                continue
            add(
                {
                    "kind": "source_param",
                    "name": pname,
                    "source": src_name,
                    "where": f"sources.{src_name}.parameters",
                    "param": pname,
                    "token": f"@{pname}",
                    "default": param.get("expression"),
                }
            )
        for pname in ats_in_sql(sql):
            if not _is_category_token(pname):
                continue
            add(
                {
                    "kind": "sql_at",
                    "name": pname,
                    "source": src_name,
                    "where": f"sources.{src_name}.sql",
                    "param": pname,
                    "token": f"@{pname}",
                    "default": None,
                }
            )

    # 3) relations с categories
    for rel in catalog.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        blob = json.dumps(rel, ensure_ascii=False)
        if not _is_category_token(blob):
            continue
        add(
            {
                "kind": "relation",
                "name": rel.get("name"),
                "where": "relations",
                "param": "Category",
                "parent": rel.get("parent"),
                "child": rel.get("child"),
                "default": None,
            }
        )

    return found


def category_params_meta(filters: list[dict], catalog: dict) -> dict[str, dict]:
    """Мета параметров Category-фильтров: token + default из sources.parameters."""
    meta = parameters_by_name(catalog)
    needed: dict[str, dict] = {}
    for item in filters:
        pname = str(item.get("param") or item.get("name") or "Category")
        if not _is_category_token(pname) and item.get("kind") not in {"source_param", "sql_at"}:
            pname = "Category"
        base = meta.get(pname) or {
            "name": pname,
            "token": item.get("token") or f"@{pname}",
            "alias": pname,
            "default": item.get("default"),
            "kind": "at",
        }
        if item.get("default") not in (None, "") and base.get("default") in (None, ""):
            base = {**base, "default": item.get("default")}
        needed[pname] = base
    # если нашли category-фильтры на page, но param ещё без default — взять из products.@Category
    if not needed:
        for name, info in meta.items():
            if _is_category_token(name):
                needed[name] = info
    return needed


def coerce_param_value(raw: str) -> Any:
    text = raw.strip().strip("\"'`")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+[.,]\d+", text):
        return float(text.replace(",", "."))
    return text


def extract_from_question(question: str, variables: dict[str, dict]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name, meta in variables.items():
        labels = [name]
        alias = meta.get("alias")
        if isinstance(alias, str) and alias.strip() and alias.strip().lower() != name.lower():
            labels.append(alias.strip())
        patterns: list[str] = [
            rf"(?i)\{{{re.escape(name)}\}}\s*[:=]?\s*([^\s,;]+)",
            rf"(?i)@{re.escape(name)}\s*[:=]?\s*([^\s,;]+)",
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


def resolve_parameters(question: str, variables: dict[str, dict]) -> dict[str, Any]:
    """Для каждого Category-фильтра: значение из вопроса, иначе default."""
    extracted = extract_from_question(question, variables)
    resolved: dict[str, Any] = {}
    for name, meta in variables.items():
        token = meta.get("token") or f"@{name}"
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


def filters_payload(resolved: dict[str, Any]) -> dict:
    return {
        meta["token"]: {"value": meta["value"], "from": meta["source"]}
        for meta in resolved.values()
    }


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
    filled = sql
    # длинные имена первыми, чтобы @CategoryID не ломал @Category
    for name, meta in sorted(resolved.items(), key=lambda kv: -len(kv[0])):
        lit = sql_literal(meta["value"])
        token = meta.get("token") or f"@{name}"
        filled = filled.replace(token, lit)
        filled = filled.replace(f"@{name}", lit)
        filled = filled.replace(f"{{{name}}}", lit)
    return filled


def to_postgres_sql(sql: str) -> str:
    return re.sub(r"`([^`]+)`", r'"\1"', sql)


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


def source_names_in_page(page: dict) -> set[str]:
    names: set[str] = set()
    blobs: list[str] = []
    _walk_strings(page, blobs)
    for blob in blobs:
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_]", blob):
            names.add(match.group(1).lower())
        for match in re.finditer(
            r"(?:Sum|Count|Avg|Min|Max)\(\s*([A-Za-z_][A-Za-z0-9_]*)\.",
            blob,
            re.I,
        ):
            names.add(match.group(1).lower())
    for element in page.get("elements") or []:
        if not isinstance(element, dict):
            continue
        ds = element.get("data_source")
        if isinstance(ds, str) and ds.strip():
            names.add(ds.strip().lower())
    return names


def match_sources_by_page_names(
    catalog: dict,
    page: dict,
    resolved: dict[str, Any],
) -> list[dict]:
    """Имена из page (products.X / Sum(products.Y)) → SQL из catalog.sources по name."""
    wanted = source_names_in_page(page)
    by_name: dict[str, dict] = {}
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict) or not source.get("sql"):
            continue
        name = str(source.get("name") or "")
        by_name[name.lower()] = source
        alias = source.get("alias")
        if isinstance(alias, str) and alias.strip():
            by_name.setdefault(alias.strip().lower(), source)

    selected: list[dict] = []
    seen: set[str] = set()
    for key in sorted(wanted):
        source = by_name.get(key)
        if not source:
            continue
        name = str(source.get("name") or key)
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        sql = str(source["sql"]).strip()
        selected.append(
            {
                "name": name,
                "alias": source.get("alias"),
                "matched_as": key,
                "sql": sql,
                "sql_filled": to_postgres_sql(fill_sql(sql, resolved)),
                "parameters": source.get("parameters") or [],
            }
        )

    # если page ссылается на products с @Category — уже в selected;
    # если Category-source нужен отдельно (categories) и упомянут relation/filter — добавить
    if not selected:
        raise RuntimeError(
            f"на page нет имён sources (нашёл refs={sorted(wanted)}, "
            f"известны={sorted({str(s.get('name') or '').lower() for s in (catalog.get('sources') or [])})})"
        )
    return selected


# --- LLM helpers -------------------------------------------------------------


# Шаг 1 — только выбор страницы. Дальше хост идёт по плану сам.
ROUTE_PROMPT = """Шаг 1 из пайплайна: определи ОДНУ страницу Stimulsoft-дашборда под вопрос.

Дан список pages: index, page, ru_text (видимый/русский текст).
Смотри на ru_text. Выбери наиболее подходящую page.

Верни ТОЛЬКО JSON, без пояснений:
{"pages":[<index>]}
Если совсем не уверен — всё равно выбери одну лучшую.
"""


ANSWER_PROMPT = """Хост уже выбрал страницу, подставил Category-фильтры и выполнил SQL через MCP.
По строкам результата ответь на вопрос. Не вызывай инструменты. Не выдумывай числа.
Ответь на языке вопроса, коротко.
"""


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("value")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def message_text(message: dict) -> str:
    for key in ("content", "text", "reasoning_content", "reasoning", "output_text"):
        text = _as_text(message.get(key))
        if text:
            return text
    return ""


async def llm_chat(llm: httpx.AsyncClient, *, model: str, messages: list[dict], label: str = "chat") -> str:
    thinking = llm_thinking_options()
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": True,
        **thinking,
    }
    log.info(
        "LLM[%s] → chars=%s thinking=%s",
        label,
        sum(len(str(m.get("content") or "")) for m in messages),
        {
            "enable_thinking": thinking.get("enable_thinking"),
            "reasoning_effort": thinking.get("reasoning_effort"),
            "thinking_budget": thinking.get("thinking_budget"),
        },
    )
    started = time.perf_counter()
    answer_parts: list[str] = []
    think_parts: list[str] = []
    in_think = False
    print(f"\n=== LLM stream [{label}] ===", flush=True)
    async with llm.stream("POST", "chat/completions", json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0] or {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            reason = ""
            if isinstance(delta, dict):
                for key in ("reasoning_content", "reasoning"):
                    value = delta.get(key)
                    if isinstance(value, str) and value:
                        reason = value
                        break
            content = ""
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                content = delta["content"]
            if not content and not reason:
                msg = choice.get("message") or {}
                if isinstance(msg, dict):
                    reason = _as_text(msg.get("reasoning_content") or msg.get("reasoning"))
                    content = message_text(msg)
            if reason:
                if not in_think:
                    print("\n[thinking]\n", end="", flush=True)
                    in_think = True
                think_parts.append(reason)
                print(reason, end="", flush=True)
            if content:
                if in_think:
                    print("\n[/thinking]\n", end="", flush=True)
                    in_think = False
                answer_parts.append(content)
                print(content, end="", flush=True)
    if in_think:
        print("\n[/thinking]", flush=True)
    print(f"\n=== end stream [{label}] ===\n", flush=True)
    text = "".join(answer_parts).strip() or "".join(think_parts).strip()
    log.info(
        "LLM[%s] ← %.2fs answer=%s think=%s",
        label,
        time.perf_counter() - started,
        len("".join(answer_parts)),
        len("".join(think_parts)),
    )
    if not text:
        raise RuntimeError(f"LLM empty content ({label})")
    return text


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[^{}]*\}", text, re.S)
    if not match:
        # nested object fallback
        match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def parse_page_indexes(route_text: str, cards: list[dict]) -> list[int]:
    valid = {int(c["index"]) for c in cards}
    parsed = extract_json_object(route_text) or {}
    selected: list[int] = []
    for item in parsed.get("pages") or []:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if idx in valid and idx not in selected:
            selected.append(idx)
    if selected:
        return selected
    # fallback: "index": 0 / index 0
    for match in re.finditer(r"(?i)\bindex\b\s*[:=]?\s*(\d+)", route_text):
        idx = int(match.group(1))
        if idx in valid and idx not in selected:
            selected.append(idx)
    if selected:
        return selected
    log.warning("Router не распарсил pages — беру все")
    return [int(c["index"]) for c in cards]


# --- MCP ---------------------------------------------------------------------


def mcp_database_uri() -> str:
    url = database_url()
    for host in ("127.0.0.1", "localhost"):
        url = url.replace(f"@{host}:", "@host.docker.internal:")
        url = url.replace(f"//{host}:", "//host.docker.internal:")
    return url


def postgres_mcp() -> StdioServerParameters:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Нужен Docker для crystaldba/postgres-mcp")
    uri = mcp_database_uri()
    log.info("MCP: crystaldba/postgres-mcp restricted uri=%s", redact_url(uri))
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


async def mcp_execute_sql(session: ClientSession, sql: str) -> dict:
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


# --- pipeline ----------------------------------------------------------------


async def answer_from_rows(
    llm: httpx.AsyncClient,
    *,
    model: str,
    question: str,
    resolved: dict[str, Any],
    results: list[dict],
) -> str:
    user = (
        f"Вопрос: {question}\n"
        f"Фильтры: {json.dumps(filters_payload(resolved), ensure_ascii=False)}\n"
        f"Результат SQL:\n{json.dumps(results, ensure_ascii=False, indent=2)}"
    )
    text = await llm_chat(
        llm,
        model=model,
        messages=[
            {"role": "system", "content": ANSWER_PROMPT},
            {"role": "user", "content": user},
        ],
        label="answer",
    )
    if text:
        return text
    chunks = [item.get("result") for item in results if item.get("result")]
    if chunks:
        return "Модель не сформулировала ответ. Данные SQL:\n" + "\n\n".join(map(str, chunks))
    raise RuntimeError("model returned an empty answer")


def stage(title: str, payload: Any = None) -> None:
    """Явная стадия пайплайна → лог + консоль."""
    if payload is None:
        msg = title
    elif isinstance(payload, str):
        msg = f"{title}\n{payload}"
    else:
        msg = f"{title}\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    log.info("%s", msg if len(msg) < 4000 else msg[:4000] + "…")
    print(f"\n>>> {title}", flush=True)
    if payload is not None:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
        print(text if len(text) < 8000 else text[:8000] + "\n…", flush=True)


def dump_ask_trace(
    *,
    question: str,
    pages: list[dict],
    variables: dict,
    queries: list[dict],
    sql_results: list[dict],
    answer: str,
    elapsed_s: float,
) -> Path:
    path = resolve_path(os.environ.get("ASK_DUMP_PATH", "ask_trace.txt"))
    lines = [
        "=== ASK ===",
        f"question: {question}",
        "",
        "=== PAGES ===",
        json.dumps(pages, ensure_ascii=False, indent=2),
        "",
        "=== VARIABLES ===",
        json.dumps(variables, ensure_ascii=False, indent=2),
        "",
        "=== SQL ===",
    ]
    for item in queries:
        lines.append(f"-- source: {item.get('source')}")
        sql = (item.get("sql") or "").strip()
        if sql:
            lines.append(sql if sql.endswith(";") else sql + ";")
        lines.append("")
    lines.append("=== SQL RESULTS ===")
    for item in sql_results:
        lines.append(f"-- source: {item.get('source')} ok={item.get('ok')}")
        lines.append(str(item.get("result") or ""))
        lines.append("")
    lines.extend(
        [
            "=== ANSWER ===",
            answer or "",
            "",
            f"=== elapsed_s: {elapsed_s:.2f} ===",
            "",
        ]
    )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    log.info("Ask trace → %s", path)
    return path


async def run_ask(
    *,
    session: ClientSession,
    llm: httpx.AsyncClient,
    model: str,
    question: str,
    cards: list[dict],
) -> dict[str, Any]:
    """План:
    1) страница
    2) вся page → Category / Filters → значения из вопроса или дефолты
    3) имена на page → sources.sql → fill
    4) MCP execute_sql
    5) ответ
    """
    stage("QUESTION", question)

    # --- Шаг 1: страница ---
    stage("STEP 1/5 — определяю страницу")
    route_user = (
        f"Вопрос пользователя:\n{question}\n\n"
        f"Список pages:\n{json.dumps(cards, ensure_ascii=False, indent=2)}"
    )
    route_text = await llm_chat(
        llm,
        model=model,
        messages=[
            {"role": "system", "content": ROUTE_PROMPT},
            {"role": "user", "content": route_user},
        ],
        label="step1-page",
    )
    page_index = parse_page_indexes(route_text, cards)[0]
    catalog = load_page_catalog(page_index)
    page = (catalog.get("pages") or [{}])[0]
    if not isinstance(page, dict):
        page = {}
    page_name = page_label(page, page_index)
    stage(
        "STEP 1/5 — страница выбрана, пробрасываю всю page",
        {
            "index": page_index,
            "page": page_name,
            "catalog": f"catalog_pages/{page_index}.json",
            "elements": len(page.get("elements") or []),
        },
    )

    # --- Шаг 2: Category / Filters на page → значения ---
    stage("STEP 2/5 — ищу Category / Filters на всей page")
    category_filters = collect_category_filters(page, catalog)
    stage("STEP 2/5 — найденные Category/Filters", category_filters)
    params_meta = category_params_meta(category_filters, catalog)
    resolved = resolve_parameters(question, params_meta)
    variables = filters_payload(resolved)
    stage("STEP 2/5 — значения (question|default)", variables)

    # --- Шаг 3: page names → sources SQL ---
    stage("STEP 3/5 — имена на page → sources по name")
    page_refs = sorted(source_names_in_page(page))
    stage("STEP 3/5 — names из page", page_refs)
    sources = match_sources_by_page_names(catalog, page, resolved)
    filled = [
        {
            "source": s.get("name"),
            "matched_as": s.get("matched_as"),
            "sql_template": s.get("sql"),
            "sql_filled": s.get("sql_filled"),
        }
        for s in sources
    ]
    stage("STEP 3/5 — sql_filled", filled)

    # --- Шаг 4: MCP (без LLM-planner) ---
    stage("STEP 4/5 — MCP execute_sql по sources страницы")
    planned = [{"source": s.get("name"), "sql": s.get("sql_filled")} for s in sources]
    results: list[dict] = []
    for item in planned:
        stage(f"STEP 4/5 — MCP [{item.get('source')}]", item.get("sql"))
        executed = await mcp_execute_sql(session, item["sql"])
        row = {
            "source": item.get("source"),
            "sql": item.get("sql"),
            "ok": executed.get("ok"),
            "result": executed.get("result"),
        }
        results.append(row)
        stage(
            f"STEP 4/5 — result [{item.get('source')}] ok={executed.get('ok')}",
            preview(executed.get("result"), 2000),
        )

    # --- Шаг 5: ответ ---
    stage("STEP 5/5 — формирую ответ")
    answer = await answer_from_rows(
        llm, model=model, question=question, resolved=resolved, results=results
    )
    stage("STEP 5/5 — ответ", answer)

    pages_meta = [
        {
            "index": page_index,
            "page": page_name,
            "category_filters": category_filters,
            "variables": variables,
            "source_names_on_page": page_refs,
            "sources": [s.get("name") for s in sources],
        }
    ]
    return {
        "answer": answer,
        "pages": pages_meta,
        "variables": variables,
        "category_filters": category_filters,
        "queries": planned,
        "sql_results": results,
        "route": route_text,
        "steps": {
            "1_page": {"index": page_index, "page": page_name},
            "2_category_filters": category_filters,
            "2_variables": variables,
            "3_page_source_names": page_refs,
            "3_sql_filled": filled,
            "4_queries": planned,
            "5_answer": answer,
        },
    }


# --- app ---------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("Нужна LLM_MODEL в .env")
    if not database_url():
        raise RuntimeError("Нужны DATABASE_URL или POSTGRES_USER/PASSWORD/DB")
    ensure_catalog()
    cards = load_page_cards()
    headers = {}
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = postgres_mcp()
    async with httpx.AsyncClient(
        base_url=llm_base_url(),
        headers=headers,
        timeout=llm_timeout(),
    ) as llm:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = [t.name for t in listed.tools]
                log.info("MCP tools=%s", tool_names)
                app.state.session = session
                app.state.llm = llm
                app.state.model = model
                app.state.page_cards = cards
                app.state.tools = tool_names
                app.state.lock = asyncio.Lock()
                thinking = llm_thinking_options()
                log.info(
                    "Ready llm=%s model=%s db=%s thinking=%s pages=%s",
                    llm_base_url(),
                    model,
                    redact_url(database_url()),
                    {
                        "enable_thinking": thinking.get("enable_thinking"),
                        "reasoning_effort": thinking.get("reasoning_effort"),
                        "thinking_budget": thinking.get("thinking_budget"),
                    },
                    [(c["index"], c["page"]) for c in cards],
                )
                yield


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
    thinking = llm_thinking_options()
    return {
        "ok": True,
        "llm": llm_base_url(),
        "model": app.state.model,
        "db": redact_url(database_url()),
        "mcp": "docker run -i --rm crystaldba/postgres-mcp --access-mode=restricted",
        "tools": getattr(app.state, "tools", []),
        "thinking": {
            "enable_thinking": thinking.get("enable_thinking"),
            "reasoning_effort": thinking.get("reasoning_effort"),
            "thinking_budget": thinking.get("thinking_budget"),
        },
        "pages": [
            {"index": c["index"], "page": c["page"], "catalog": c["catalog"]}
            for c in app.state.page_cards
        ],
    }


@app.post("/ask")
async def ask(body: Ask) -> dict:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")

    started = time.perf_counter()
    async with app.state.lock:
        try:
            result = await run_ask(
                session=app.state.session,
                llm=app.state.llm,
                model=app.state.model,
                question=question,
                cards=app.state.page_cards,
            )
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail=f"LLM timeout after {llm_timeout()}s") from exc
        except (httpx.HTTPError, RuntimeError, KeyError) as exc:
            log.exception("ask failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    elapsed = time.perf_counter() - started
    trace = dump_ask_trace(
        question=question,
        pages=result.get("pages") or [],
        variables=result.get("variables") or {},
        queries=result.get("queries") or [],
        sql_results=result.get("sql_results") or [],
        answer=result.get("answer") or "",
        elapsed_s=elapsed,
    )
    log.info("ask %.2fs pages=%s trace=%s", elapsed, result.get("pages"), trace)
    return {
        "answer": result.get("answer"),
        "pages": result.get("pages"),
        "variables": result.get("variables"),
        "queries": result.get("queries"),
        "steps": result.get("steps"),
        "sql_results": [
            {
                "source": item.get("source"),
                "ok": item.get("ok"),
                "sql": item.get("sql"),
                "preview": preview(item.get("result"), 500),
            }
            for item in (result.get("sql_results") or [])
        ],
        "trace": str(trace),
    }


def main() -> None:
    load_env()
    setup_logging()
    if not os.environ.get("LLM_MODEL", "").strip():
        raise SystemExit("Задай LLM_MODEL в .env")
    if not database_url():
        raise SystemExit("Задай DATABASE_URL или POSTGRES_* в .env")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run("host:app", host=host, port=port)


if __name__ == "__main__":
    main()
