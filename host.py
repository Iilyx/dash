#!/usr/bin/env python3
"""Ask pipeline: .mrt catalog → Postgres MCP Pro → answer.

Dictionary.Variables → placeholders {Name} in SQL.
Bare identifiers (year, CategoryID, …) are DB columns, not variables.
MCP is crystaldba/postgres-mcp via docker run -i --rm (restricted).
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
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def braces_in_sql(sql: str) -> list[str]:
    """Names inside {…} only. Bare year / CategoryID are columns, not variables."""
    seen: set[str] = set()
    ordered: list[str] = []
    for name in PLACEHOLDER_BRACE.findall(sql):
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def dashboard_variables(catalog: dict) -> list[dict]:
    """Only Dictionary.Variables. In SQL each is written as {Name}, never as bare Name."""
    result: list[dict] = []
    for variable in catalog.get("variables") or []:
        if not isinstance(variable, dict):
            continue
        name = variable.get("name")
        if not name:
            continue
        name = str(name)
        item = {
            "name": name,
            "alias": variable.get("alias"),
            "token": f"{{{name}}}",
            "default": variable.get("value"),
            "description": variable.get("description"),
            "type": variable.get("type"),
            "from": variable.get("from"),
            "to": variable.get("to"),
            "dialog": variable.get("dialog"),
        }
        result.append({key: value for key, value in item.items() if value not in (None, "", [])})
    return result


def variable_defaults(catalog: dict) -> dict[str, Any]:
    return {
        item["token"]: item.get("default")
        for item in dashboard_variables(catalog)
        if item.get("token")
    }


def query_templates(catalog: dict) -> list[dict]:
    """SQL templates; only {Name} tokens that exist in Dictionary.Variables."""
    by_name = {item["name"]: item for item in dashboard_variables(catalog)}
    templates: list[dict] = []
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict):
            continue
        sql = source.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            continue
        linked = []
        for name in braces_in_sql(sql):
            meta = by_name.get(name)
            if not meta:
                # {foo} in SQL but no Variable — leave as-is, do not invent
                linked.append({"name": name, "token": f"{{{name}}}", "unknown": True})
                continue
            linked.append(
                {
                    "name": meta["name"],
                    "token": meta["token"],
                    "alias": meta.get("alias"),
                    "default": meta.get("default"),
                }
            )
        templates.append(
            {
                "source": source.get("name"),
                "sql": sql.strip(),
                "variables": linked,
            }
        )
    return templates


def default_parameters(catalog: dict) -> list[dict]:
    return [
        {
            "token": item["token"],
            "name": item["name"],
            "alias": item.get("alias"),
            "value": item.get("default"),
        }
        for item in dashboard_variables(catalog)
        if item.get("default") not in (None, "")
    ]


def known_variables(catalog: dict) -> dict[str, dict]:
    """Dictionary Name -> meta. SQL token is always {Name}."""
    return {item["name"]: item for item in dashboard_variables(catalog) if item.get("name")}


def coerce_param_value(raw: str) -> Any:
    text = raw.strip().strip("\"'`")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+[.,]\d+", text):
        return float(text.replace(",", "."))
    return text


def extract_from_question(question: str, variables: dict[str, dict]) -> dict[str, Any]:
    """Values for Dictionary Variables ({year}, …) from the user question."""
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
        if lower == "category" or (isinstance(alias, str) and "категор" in alias.lower()):
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
    """Resolve each Dictionary Variable; SQL gets {Name} filled, columns stay untouched."""
    variables = known_variables(catalog)
    extracted = extract_from_question(question, variables)
    resolved: dict[str, Any] = {}
    for name, meta in variables.items():
        token = meta["token"]
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
    """Replace only {Name} tokens. Never touch bare column names like year."""
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


def system_prompt(catalog: dict) -> str:
    templates = query_templates(catalog)
    defaults = default_parameters(catalog)
    templates_body = json.dumps(templates, ensure_ascii=False, indent=2)
    defaults_body = json.dumps(defaults, ensure_ascii=False, indent=2)
    catalog_body = json.dumps(catalog, ensure_ascii=False, indent=2)
    variables_body = json.dumps(dashboard_variables(catalog), ensure_ascii=False, indent=2)
    return (
        "Ты отвечаешь на вопросы по дашборду Stimulsoft.\n"
        "Важно:\n"
        "- Dictionary.Variables — переменные. В SQL они только в фигурных скобках: {year}, {Category}.\n"
        "- Голое имя без скобок (year, CategoryID, products.CategoryID) — это поле/колонка БД, "
        "не переменная. Его нельзя подменять и нельзя путать с {year}.\n"
        "Хост уже подставил значения Variables в {Name} (из вопроса или default mrt).\n"
        "В сообщении пользователя есть:\n"
        "- «Переменные» — итоговые значения по токенам {Name}\n"
        "- «SQL с подстановкой» — бери sql_filled для execute_sql\n"
        "Не проси JSON parameters. Не оставляй {Name} в SQL. Колонки не трогай.\n"
        "Если sql_filled ещё MySQL — слегка поправь под PostgreSQL "
        "(например `order details` → \"order details\").\n"
        "Пайплайн:\n"
        "1) Выбери нужный sql_filled.\n"
        "2) Вызови execute_sql.\n"
        "3) По строкам примени логику виджетов (Sum/Count, форматы, фильтры).\n"
        "4) Ответь на языке вопроса. Не выдумывай числа.\n\n"
        f"Variables из mrt (токен = {{Name}}):\n{variables_body}\n\n"
        f"SQL-шаблоны:\n{templates_body}\n\n"
        f"Дефолты Variables:\n{defaults_body}\n\n"
        f"Полный каталог:\n{catalog_body}"
    )


def user_message(question: str, resolved: dict[str, Any], queries: list[dict]) -> str:
    variables = {
        meta["token"]: {"value": meta["value"], "from": meta["source"]}
        for meta in resolved.values()
    }
    ready = [
        {"source": item["source"], "sql_filled": item["sql_filled"]}
        for item in queries
    ]
    if not resolved and not ready:
        return question
    return (
        f"{question}\n\n"
        "Переменные (только {Name} из Variables, не колонки БД):\n"
        f"{json.dumps(variables, ensure_ascii=False, indent=2)}\n\n"
        "SQL с подстановкой:\n"
        f"{json.dumps(ready, ensure_ascii=False, indent=2)}"
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


async def run_tool(session: ClientSession, name: str, arguments: dict, known: set[str]) -> str:
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
    queries: list[dict],
) -> str:
    known = {tool["function"]["name"] for tool in tools}
    messages: list[dict] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_message(question, resolved, queries)},
    ]
    used_tools = False
    empty_retries = 0
    log.info(
        "Вопрос: %s | variables=%s | tools=%s",
        preview(question, 200),
        preview({meta["token"]: meta["value"] for meta in resolved.values()}, 300),
        sorted(known),
    )
    for item in queries:
        log.info("SQL filled %s: %s", item.get("source"), preview(item.get("sql_filled"), 200))
    for round_no in range(1, MAX_TOOL_ROUNDS + 3):
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
                log.warning("После MCP модель молчит — прошу ответить по результату SQL")
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
                        log.warning("Отдаю сырой результат MCP как ответ")
                        return f"Модель не сформулировала ответ. Данные из MCP:\n{item['content']}"
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
                tool_text = await run_tool(session, name, arguments, known)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_text})
    log.error("Слишком много раундов MCP для вопроса: %s", preview(question, 200))
    return "Модель не закончила ответ: слишком много вызовов MCP."


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
    prompt = system_prompt(catalog)
    log.info(
        "Промпт: report=%s queries=%s defaults=%s prompt_chars=%s",
        (catalog.get("report") or {}).get("name"),
        [item.get("source") for item in templates],
        defaults,
        len(prompt),
    )
    for item in templates:
        log.info(
            "SQL template %s variables=%s sql=%s",
            item.get("source"),
            [p["name"] for p in item.get("variables") or []],
            preview(item.get("sql"), 180),
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
                app.state.prompt = prompt
                app.state.catalog = catalog
                app.state.defaults = defaults
                app.state.queries = templates
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
        "queries": app.state.queries,
        "llm": llm_base_url(),
        "mcp": "docker run -i --rm crystaldba/postgres-mcp --access-mode=restricted",
    }


@app.post("/ask")
async def ask(body: Ask) -> dict:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")
    resolved = resolve_parameters(question, app.state.catalog)
    queries = filled_queries(app.state.catalog, resolved)
    log.info(
        "POST /ask question=%s variables=%s",
        preview(question, 200),
        preview({meta["token"]: meta for meta in resolved.values()}, 400),
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
                queries=queries,
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
