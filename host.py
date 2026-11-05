#!/usr/bin/env python3
""" /ask:
1) LLM → page → catalog_pages/{N}.json
2) LLM ← pages: name из sources
3) SQL sources → {Name} → variables; variable.dialog.source → ещё sources
4) SQL dialog.source → MCP → dialog_results
5) Фильтр ← вопрос / default / значение из dialog_results → sql_filled
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

from extract_mrt import catalog_from_report, load_mrt, write_page_packs

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("host")

BRACE_NAME = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
FILTER_CATEGORIES = {"фильтр", "фильтры", "filter", "filters"}
MAX_TOOL_CHARS = 24_000

ROUTE_SYSTEM = 'Выбери ОДНУ page. Верни ТОЛЬКО JSON: {"pages":[<index>]}'

SOURCES_SYSTEM = """Задача: какие DataSources реально используются на странице.

Дано:
- pages — JSON страницы (elements / expressions / meters / text / data_source)
- available_source_names — допустимые name из catalog.sources

Правила:
1) Смотри ТОЛЬКО pages. Ищи ссылки SourceName.Field, Sum(SourceName.X), data_source и т.п.
2) В ответ клади ТОЛЬКО те name из available_source_names, на которые в pages есть явная ссылка.
3) НЕ копируй весь available_source_names. Нет ссылки в pages — не включай.
4) Регистр не важен (Products ≡ products). Ничего не выдумывай.
5) Верни ТОЛЬКО JSON: {"source_names":["name", "..."]}
"""

RESOLVE_SYSTEM = """Задача: подставить значения переменных / фильтров для SQL.

Дано:
- question — вопрос пользователя
- braces — имена {Name} из SQL sources
- variables / filters — variables (filters = category Фильтр)
- dialog_results — уже выполненный SQL dialog.source: [{name, sql_filled, ok, result}, ...]
  result — строки опций фильтра (ключи/значения из БД)

Правила приоритета для каждого name из braces и filters:
1) Если в question есть явное значение фильтра — возьми его.
   Если у фильтра есть dialog_results: сопоставь вопрос со строками result
   (по подписи/имени/ключу) и подставь реальное значение из result
   (обычно ключ / id / код колонки keys_column, не выдуманный текст).
2) Иначе если подходит default variable.value — и он есть в dialog_results
   (когда dialog есть) — используй default (или соответствующий ключ из result).
3) Иначе variable.value (default), даже без dialog.
4) Если не фильтр — variable.value или null.
5) Ничего не выдумывай сверх question, variable.value и dialog_results.
6) Верни ТОЛЬКО JSON:
{"resolved": {"Name": "value или null", "...": "..."},
 "from": {"Name": "question"|"dialog"|"default"|"none", "...": "..."}}
"""


class Ask(BaseModel):
    question: str = Field(min_length=1)


def load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def path_of(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def llm_url() -> str:
    base = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


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


def redact_url(url: str) -> str:
    return re.sub(r":([^:@/]+)@", r":***@", url)


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
    if not database_url():
        raise RuntimeError("Нужны DATABASE_URL или POSTGRES_USER/PASSWORD/DB")
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


async def execute_dialog_sources(
    session: ClientSession | None,
    *,
    dialog_names: list[str],
    all_sources: list[dict],
    resolved: dict[str, object],
) -> list[dict]:
    """Найти sources по dialog name и выполнить их SQL через MCP."""
    if not dialog_names:
        return []
    if session is None:
        raise HTTPException(503, "MCP Postgres не запущен (нужны DATABASE_URL/POSTGRES_* + Docker)")
    dialog_sources = sources_by_names(all_sources, dialog_names)
    queries = build_queries(dialog_sources, resolved)
    results: list[dict] = []
    for item in queries:
        sql = str(item.get("sql_filled") or item.get("sql") or "").strip()
        name = item.get("name") or "dialog"
        if not sql:
            results.append(
                {"name": name, "sql": item.get("sql"), "sql_filled": None, "ok": False, "result": "empty sql"}
            )
            continue
        log.info("MCP dialog source=%s sql=%s", name, sql[:200])
        executed = await mcp_execute_sql(session, sql)
        results.append(
            {
                "name": name,
                "sql": item.get("sql"),
                "sql_filled": sql,
                "ok": executed.get("ok"),
                "result": executed.get("result"),
            }
        )
    return results


def ensure_catalog() -> None:
    catalog_path = path_of(os.environ.get("CATALOG_PATH", "catalog.json"))
    pages_dir = path_of(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages"))
    mrt = path_of(os.environ.get("MRT_PATH", "SalesOfProducts.mrt"))
    if mrt.is_file():
        catalog = catalog_from_report(load_mrt(mrt))
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_page_packs(catalog, pages_dir)
    elif catalog_path.is_file():
        write_page_packs(json.loads(catalog_path.read_text(encoding="utf-8")), pages_dir)
    else:
        raise RuntimeError("Нужен MRT_PATH или catalog.json")


def page_cards() -> list[dict]:
    pages_dir = path_of(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages"))
    cards = []
    for path in sorted(pages_dir.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else p.stem):
        if not path.stem.isdigit():
            continue
        catalog = json.loads(path.read_text(encoding="utf-8"))
        page = (catalog.get("pages") or [{}])[0]
        cards.append(
            {
                "index": int(path.stem),
                "page": page.get("alias") or page.get("name") or path.stem,
                "ru_text": [
                    str(page.get(k))
                    for k in ("alias", "name", "title")
                    if isinstance(page.get(k), str) and page.get(k)
                ],
            }
        )
    if not cards:
        raise RuntimeError(f"нет page-файлов в {pages_dir}")
    return cards


async def llm(client: httpx.AsyncClient, model: str, system: str, user: str) -> str:
    enable = os.environ.get("LLM_ENABLE_THINKING", "true").lower() in {"1", "true", "yes"}
    thinking = {
        "enable_thinking": enable,
        "chat_template_kwargs": {"enable_thinking": enable},
    }
    if enable:
        thinking["reasoning_effort"] = os.environ.get("LLM_REASONING_EFFORT", "low")

    parts: list[str] = []
    think: list[str] = []
    in_think = False
    print("\n=== LLM stream ===", flush=True)
    async with client.stream(
        "POST",
        "chat/completions",
        json={
            "model": model,
            "temperature": 0,
            "stream": True,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **thinking,
        },
    ) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            delta = ((chunk.get("choices") or [{}])[0].get("delta")) or {}
            if not isinstance(delta, dict):
                continue
            reason = delta.get("reasoning_content") or delta.get("reasoning") or ""
            content = delta.get("content") or ""
            if isinstance(reason, str) and reason:
                if not in_think:
                    print("\n[thinking]\n", end="", flush=True)
                    in_think = True
                think.append(reason)
                print(reason, end="", flush=True)
            if isinstance(content, str) and content:
                if in_think:
                    print("\n[/thinking]\n", end="", flush=True)
                    in_think = False
                parts.append(content)
                print(content, end="", flush=True)
    if in_think:
        print("\n[/thinking]", flush=True)
    print("\n=== end stream ===\n", flush=True)
    text = "".join(parts).strip() or "".join(think).strip()
    if not text:
        raise RuntimeError("LLM empty content")
    return text


def parse_json_object(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1]) if start >= 0 and end > start else {}
    return data if isinstance(data, dict) else {}


def parse_page_index(text: str, cards: list[dict]) -> int:
    valid = {c["index"] for c in cards}
    data = parse_json_object(text)
    for item in data.get("pages") or []:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if idx in valid:
            return idx
    return cards[0]["index"]


def parse_source_names(text: str, sources: list[dict]) -> list[str]:
    """LLM JSON → только name, которые есть в catalog.sources (case-insensitive)."""
    data = parse_json_object(text)
    raw = data.get("source_names")
    if not isinstance(raw, list):
        raw = data.get("sources") if isinstance(data.get("sources"), list) else []
    by_key: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        name = source.get("name")
        if isinstance(name, str) and name.strip():
            by_key[name.strip().lower()] = name.strip()
        alias = source.get("alias")
        if isinstance(alias, str) and alias.strip() and isinstance(name, str) and name.strip():
            by_key.setdefault(alias.strip().lower(), name.strip())

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        candidate = item.get("name") if isinstance(item, dict) else item
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        canonical = by_key.get(candidate.strip().lower())
        if not canonical or canonical.lower() in seen:
            continue
        seen.add(canonical.lower())
        out.append(canonical)
    return out


def sources_by_names(sources: list[dict], names: list[str]) -> list[dict]:
    want = {n.lower() for n in names}
    out: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        name = source.get("name")
        if not isinstance(name, str):
            continue
        key = name.strip().lower()
        if key in want and key not in seen:
            seen.add(key)
            out.append(source)
    return out


def braces_from_sources(sources: list[dict]) -> list[str]:
    """{Name} из SQL уже выбранных sources — порядок появления, без дублей."""
    out: list[str] = []
    seen: set[str] = set()
    for source in sources:
        sql = source.get("sql") if isinstance(source, dict) else None
        if not isinstance(sql, str):
            continue
        for name in BRACE_NAME.findall(sql):
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
    return out


def variables_by_names(variables: list[dict], names: list[str]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for item in variables:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            by_key[name.strip().lower()] = item
    out: list[dict] = []
    seen: set[str] = set()
    for name in names:
        key = name.strip().lower()
        item = by_key.get(key)
        if not item or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def is_filter_variable(variable: dict) -> bool:
    category = variable.get("category")
    return isinstance(category, str) and category.strip().lower() in FILTER_CATEGORIES


def filter_variables(variables: list[dict]) -> list[dict]:
    """Все variables с category Фильтр / Фильтры — не только те, что встретились в SQL {}."""
    return [v for v in variables if isinstance(v, dict) and is_filter_variable(v)]


def merge_variables(*groups: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            key = name.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


def dialog_source_name(variable: dict) -> str | None:
    """variable.dialog.source или SourceName из keys_column = SourceName.Column."""
    dialog = variable.get("dialog")
    if not isinstance(dialog, dict):
        return None
    source = dialog.get("source")
    if isinstance(source, str) and source.strip():
        return source.strip()
    for key in ("keys_column", "values_column"):
        path = dialog.get(key)
        if isinstance(path, str) and "." in path:
            head = path.split(".", 1)[0].strip()
            if head:
                return head
    return None


def dialog_source_names(variables: list[dict]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for variable in variables:
        if not isinstance(variable, dict):
            continue
        name = dialog_source_name(variable)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return out


def merge_sources(existing: list[dict], all_sources: list[dict], extra_names: list[str]) -> tuple[list[dict], list[str]]:
    """Добавить sources по name (из variable.dialog.source), вернуть (sources, добавленные name)."""
    have = {
        str(s["name"]).strip().lower()
        for s in existing
        if isinstance(s.get("name"), str) and str(s["name"]).strip()
    }
    added: list[str] = []
    out = list(existing)
    by_key = {
        str(s["name"]).strip().lower(): s
        for s in all_sources
        if isinstance(s.get("name"), str) and str(s["name"]).strip()
    }
    for raw in extra_names:
        key = raw.strip().lower()
        if key in have:
            continue
        source = by_key.get(key)
        if not source:
            continue
        out.append(source)
        have.add(key)
        added.append(str(source["name"]).strip())
    return out, added


def default_resolved(variables: list[dict], names: list[str]) -> dict[str, object]:
    by_key = {
        str(v["name"]).strip().lower(): v
        for v in variables
        if isinstance(v.get("name"), str) and str(v["name"]).strip()
    }
    out: dict[str, object] = {}
    for name in names:
        item = by_key.get(name.lower())
        if item is None:
            out[name] = None
        else:
            out[name] = item.get("value")
    return out


def parse_resolved(text: str, names: list[str], fallback: dict[str, object]) -> dict[str, object]:
    data = parse_json_object(text)
    raw = data.get("resolved") if isinstance(data.get("resolved"), dict) else {}
    out: dict[str, object] = dict(fallback)
    by_name = {n.lower(): n for n in names}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        canonical = by_name.get(key.strip().lower())
        if not canonical:
            continue
        out[canonical] = value
    return out


def fill_sql(sql: str, resolved: dict[str, object]) -> str:
    by_key = {k.lower(): v for k, v in resolved.items()}

    def repl(match: re.Match[str]) -> str:
        if match.group(1).lower() not in by_key:
            return match.group(0)
        value = by_key[match.group(1).lower()]
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float)):
            return str(value)
        return "'" + str(value).replace("'", "''") + "'"

    return BRACE_NAME.sub(repl, sql)


def build_queries(sources: list[dict], resolved: dict[str, object]) -> list[dict]:
    queries: list[dict] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        sql = source.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            continue
        item = {
            "name": source.get("name"),
            "sql": sql,
            "sql_filled": fill_sql(sql, resolved),
        }
        queries.append({k: v for k, v in item.items() if v is not None})
    return queries


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("Нужна LLM_MODEL в .env")
    ensure_catalog()
    headers = {}
    key = os.environ.get("LLM_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    async with httpx.AsyncClient(
        base_url=llm_url(),
        headers=headers,
        timeout=float(os.environ.get("LLM_TIMEOUT", "120") or "120"),
    ) as client:
        app.state.llm = client
        app.state.model = model
        app.state.cards = page_cards()
        app.state.lock = asyncio.Lock()
        app.state.session = None
        app.state.tools = []

        if database_url():
            params = postgres_mcp()
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    app.state.session = session
                    app.state.tools = [t.name for t in listed.tools]
                    log.info(
                        "Ready pages=%s mcp=%s db=%s",
                        [(c["index"], c["page"]) for c in app.state.cards],
                        app.state.tools,
                        redact_url(database_url()),
                    )
                    yield
                    return

        log.info("Ready pages=%s (MCP off — нет DATABASE_URL/POSTGRES_*)", [(c["index"], c["page"]) for c in app.state.cards])
        yield


load_env()
logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "model": app.state.model,
        "pages": app.state.cards,
        "db": redact_url(database_url()) if database_url() else None,
        "mcp_tools": getattr(app.state, "tools", []),
    }


def trace_path() -> Path:
    return path_of(os.environ.get("ASK_DUMP_PATH", "ask_trace.txt"))


def trace_reset() -> Path:
    path = trace_path()
    path.write_text("", encoding="utf-8")
    return path


def trace_step(title: str, payload: object | None = None) -> None:
    path = trace_path()
    lines = [f"=== {title} ==="]
    if payload is not None:
        if isinstance(payload, str):
            lines.append(payload)
        else:
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    lines.append("")
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f">>> TRACE {title}", flush=True)


@app.post("/ask")
async def ask(body: Ask):
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "question is empty")

    route_user = f"Вопрос:\n{q}\n\nPages:\n{json.dumps(app.state.cards, ensure_ascii=False, indent=2)}"

    trace = trace_reset()
    trace_step("1. QUESTION", q)
    trace_step("2. PAGES → LLM", app.state.cards)
    trace_step("2. PROMPT ROUTE", {"system": ROUTE_SYSTEM, "user": route_user})

    try:
        route_raw = await llm(app.state.llm, app.state.model, ROUTE_SYSTEM, route_user)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "LLM timeout") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, str(exc)) from exc

    trace_step("3. LLM ROUTE", route_raw)

    idx = parse_page_index(route_raw, app.state.cards)
    catalog_file = path_of(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages")) / f"{idx}.json"
    catalog = json.loads(catalog_file.read_text(encoding="utf-8"))
    page = (catalog.get("pages") or [{}])[0]
    name = page.get("alias") or page.get("name") or f"page_{idx}"
    all_sources = [s for s in (catalog.get("sources") or []) if isinstance(s, dict)]

    trace_step(
        "4. PAGE SELECTED",
        {"page_index": idx, "page_name": name, "catalog_file": str(catalog_file)},
    )
    # в LLM — pages + список допустимых name; полный sources[] не даём (иначе копирует всё)
    available_names = [
        str(s["name"]).strip()
        for s in all_sources
        if isinstance(s.get("name"), str) and str(s["name"]).strip()
    ]
    sources_payload = {
        "pages": catalog.get("pages") or [],
        "available_source_names": available_names,
    }
    trace_step("5. PAGES + SOURCE NAMES → LLM", sources_payload)

    sources_user = (
        f"Вопрос пользователя:\n{q}\n\n"
        f"Input JSON:\n{json.dumps(sources_payload, ensure_ascii=False, indent=2)}"
    )
    trace_step("5. PROMPT SOURCES", {"system": SOURCES_SYSTEM, "user": sources_user})

    try:
        sources_raw = await llm(app.state.llm, app.state.model, SOURCES_SYSTEM, sources_user)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "LLM timeout (sources)") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, str(exc)) from exc

    source_names = parse_source_names(sources_raw, all_sources)
    sources = sources_by_names(all_sources, source_names)
    trace_step("6. LLM SOURCES", sources_raw)
    trace_step("6. SOURCE NAMES", {"source_names": source_names, "sources": sources})

    # SQL → {Name} → variables по name; filters = ВСЕ с category Фильтр из catalog
    all_variables = [v for v in (catalog.get("variables") or []) if isinstance(v, dict)]
    braces = braces_from_sources(sources)
    brace_variables = variables_by_names(all_variables, braces)
    filters = filter_variables(all_variables)
    variables = merge_variables(brace_variables, filters)
    resolve_names = list(
        dict.fromkeys(
            [
                *braces,
                *[
                    str(v["name"]).strip()
                    for v in filters
                    if isinstance(v.get("name"), str) and str(v["name"]).strip()
                ],
            ]
        )
    )
    trace_step(
        "7. BRACES → VARIABLES + FILTERS",
        {"braces": braces, "variables": variables, "filters": filters},
    )

    # variable.dialog.source (или keys_column SourceName.Col) → ещё sources по name
    dialog_names = dialog_source_names(variables)
    sources, dialog_added = merge_sources(sources, all_sources, dialog_names)
    for added in dialog_added:
        if added.lower() not in {n.lower() for n in source_names}:
            source_names.append(added)
    # у новых sources тоже могут быть {Name} → дотянуть variables (filters уже все)
    if dialog_added:
        braces = braces_from_sources(sources)
        brace_variables = variables_by_names(all_variables, braces)
        variables = merge_variables(brace_variables, filters)
        resolve_names = list(
            dict.fromkeys(
                [
                    *braces,
                    *[
                        str(v["name"]).strip()
                        for v in filters
                        if isinstance(v.get("name"), str) and str(v["name"]).strip()
                    ],
                ]
            )
        )
    trace_step(
        "7. VARIABLES → DIALOG SOURCES",
        {
            "dialog_source_names": dialog_names,
            "dialog_added": dialog_added,
            "source_names": source_names,
            "braces": braces,
            "variables": variables,
            "filters": filters,
        },
    )

    # defaults сначала — чтобы dialog SQL тоже мог подставить {Name}
    resolved = default_resolved(variables, resolve_names)

    # 1) dialog.source SQL → MCP (список опций фильтра)
    async with app.state.lock:
        dialog_results = await execute_dialog_sources(
            app.state.session,
            dialog_names=dialog_names,
            all_sources=all_sources,
            resolved=resolved,
        )
    trace_step("8. DIALOG SQL (MCP)", dialog_results)

    # 2) LLM: вопрос / default / значение из dialog_results
    resolve_raw = ""
    if filters or braces:
        resolve_payload = {
            "question": q,
            "braces": braces,
            "variables": variables,
            "filters": filters,
            "dialog_source_names": dialog_names,
            "dialog_results": dialog_results,
            "defaults": resolved,
        }
        resolve_user = f"Input JSON:\n{json.dumps(resolve_payload, ensure_ascii=False, indent=2)}"
        trace_step("8. PROMPT RESOLVE", {"system": RESOLVE_SYSTEM, "user": resolve_user})
        try:
            resolve_raw = await llm(app.state.llm, app.state.model, RESOLVE_SYSTEM, resolve_user)
        except httpx.TimeoutException as exc:
            raise HTTPException(504, "LLM timeout (resolve)") from exc
        except (httpx.HTTPError, RuntimeError) as exc:
            raise HTTPException(502, str(exc)) from exc
        resolved = parse_resolved(resolve_raw, resolve_names, resolved)
        trace_step("9. LLM RESOLVE", {"raw": resolve_raw, "resolved": resolved})

    # 3) основной SQL с финальными значениями
    queries = build_queries(sources, resolved)
    trace_step("9. RESOLVED + QUERIES", {"resolved": resolved, "queries": queries})
    trace_step(
        "10. RESULT",
        {
            "page_index": idx,
            "page_name": name,
            "source_names": source_names,
            "sources": sources,
            "braces": braces,
            "variables": variables,
            "filters": filters,
            "dialog_source_names": dialog_names,
            "dialog_results": dialog_results,
            "resolved": resolved,
            "queries": queries,
            "catalog": catalog,
            "route": route_raw,
            "sources_route": sources_raw,
            "resolve_route": resolve_raw,
            "trace": str(trace),
        },
    )

    log.info(
        "ask page=%s index=%s sources=%s braces=%s filters=%s dialog=%s dialog_ok=%s trace=%s",
        name,
        idx,
        source_names,
        braces,
        [f.get("name") for f in filters],
        dialog_names,
        [r.get("ok") for r in dialog_results],
        trace,
    )
    return {
        "page_index": idx,
        "page_name": name,
        "source_names": source_names,
        "sources": sources,
        "braces": braces,
        "variables": variables,
        "filters": filters,
        "dialog_source_names": dialog_names,
        "dialog_results": dialog_results,
        "resolved": resolved,
        "queries": queries,
        "catalog": catalog,
        "route": route_raw,
        "sources_route": sources_raw,
        "resolve_route": resolve_raw,
        "trace": str(trace),
    }


def main() -> None:
    load_env()
    if not os.environ.get("LLM_MODEL", "").strip():
        raise SystemExit("Задай LLM_MODEL в .env")
    if not database_url():
        log.warning("DATABASE_URL/POSTGRES_* не заданы — dialog SQL через MCP будет недоступен")
    uvicorn.run("host:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8090")))


if __name__ == "__main__":
    main()
