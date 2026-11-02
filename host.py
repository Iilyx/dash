#!/usr/bin/env python3
"""Ask pipeline: .mrt catalog → Postgres MCP Pro → answer.

SQL templates and @Name/{Name} variables come from the .mrt.
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
PLACEHOLDER_AT = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
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


def placeholders_in(sql: str) -> list[str]:
    names = PLACEHOLDER_AT.findall(sql) + PLACEHOLDER_BRACE.findall(sql)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def query_templates(catalog: dict) -> list[dict]:
    """SQL from .mrt sources + placeholders {@Name}/{Name} and their defaults."""
    defaults_by_name: dict[str, Any] = {}
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict):
            continue
        for param in source.get("parameters") or []:
            if not isinstance(param, dict):
                continue
            name = param.get("name")
            value = param.get("expression")
            if value is None:
                value = param.get("value")
            if name and value not in (None, ""):
                defaults_by_name[str(name)] = value
    for variable in catalog.get("variables") or []:
        if not isinstance(variable, dict):
            continue
        name = variable.get("name")
        value = variable.get("value")
        if name and value not in (None, ""):
            defaults_by_name[str(name)] = value

    templates: list[dict] = []
    for source in catalog.get("sources") or []:
        if not isinstance(source, dict):
            continue
        sql = source.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            continue
        names = placeholders_in(sql)
        # parameters declared on the source even if not in SQL text yet
        for param in source.get("parameters") or []:
            if isinstance(param, dict) and param.get("name"):
                name = str(param["name"])
                if name not in names:
                    names.append(name)
        templates.append(
            {
                "source": source.get("name"),
                "sql": sql.strip(),
                "placeholders": [
                    {
                        "name": name,
                        "in_sql": [f"@{name}", f"{{{name}}}"],
                        "default": defaults_by_name.get(name),
                    }
                    for name in names
                ],
            }
        )
    return templates


def default_parameters(catalog: dict) -> list[dict]:
    defaults: list[dict] = []
    for template in query_templates(catalog):
        for item in template.get("placeholders") or []:
            if item.get("default") in (None, ""):
                continue
            defaults.append(
                {
                    "source": template.get("source"),
                    "name": item["name"],
                    "sql_placeholder": f"@{item['name']}",
                    "brace_placeholder": f"{{{item['name']}}}",
                    "value": item["default"],
                }
            )
    return defaults


def known_variables(catalog: dict) -> dict[str, Any]:
    """name -> default (may be None if declared without default)."""
    names: dict[str, Any] = {}
    for template in query_templates(catalog):
        for item in template.get("placeholders") or []:
            name = item.get("name")
            if not name:
                continue
            if name not in names or names[name] in (None, ""):
                names[name] = item.get("default")
    return names


def coerce_param_value(raw: str) -> Any:
    text = raw.strip().strip("\"'`")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+[.,]\d+", text):
        return float(text.replace(",", "."))
    return text


def extract_from_question(question: str, names: dict[str, Any]) -> dict[str, Any]:
    """Pull variable values mentioned in the free-text question."""
    found: dict[str, Any] = {}
    for name in names:
        patterns = [
            rf"(?i)\b{re.escape(name)}\b\s*[:=]\s*([^\s,;]+)",
            rf"(?i)\b{re.escape(name)}\b\s+(?:is|равен|равна|=)?\s*([^\s,;]+)",
            rf"(?i)\b{re.escape(name)}\b\s+(-?\d+(?:[.,]\d+)?)",
        ]
        # common Russian wording around Category-like params
        if name.lower() == "category":
            patterns.append(r"(?i)\bкатегор\w*\s*(?:№|#|id)?\s*[:=]?\s*(-?\d+)")
            patterns.append(r"(?i)\bcategory\s*(?:id|№|#)?\s*[:=]?\s*(-?\d+)")
        for pattern in patterns:
            match = re.search(pattern, question)
            if not match:
                continue
            found[name] = coerce_param_value(match.group(1))
            break
    return found


def resolve_parameters(question: str, catalog: dict) -> dict[str, Any]:
    """Only question in /ask: extract params, else catalog defaults."""
    names = known_variables(catalog)
    extracted = extract_from_question(question, names)
    resolved: dict[str, Any] = {}
    for name, default in names.items():
        if name in extracted:
            resolved[name] = {"value": extracted[name], "source": "question"}
        elif default not in (None, ""):
            resolved[name] = {"value": default, "source": "default"}
    return resolved


def system_prompt(catalog: dict) -> str:
    templates = query_templates(catalog)
    defaults = default_parameters(catalog)
    templates_body = json.dumps(templates, ensure_ascii=False, indent=2)
    defaults_body = json.dumps(defaults, ensure_ascii=False, indent=2)
    catalog_body = json.dumps(catalog, ensure_ascii=False, indent=2)
    return (
        "Ты отвечаешь на вопросы по дашборду Stimulsoft.\n"
        "SQL уже лежит в .mrt / каталоге. Не выдумывай новые таблицы с нуля — "
        "бери шаблон из «SQL из mrt».\n"
        "Плейсхолдеры в SQL — переменные @Name и {Name}.\n"
        "В сообщении пользователя уже есть блок «Параметры»: их вытащили из вопроса, "
        "а если в вопросе не указаны — поставили дефолты из mrt. "
        "Подставь эти значения в SQL (и в @Name, и в {Name}). "
        "Не проси отдельный JSON parameters.\n"
        "Числа подставляй без кавычек, строки — в одинарных кавычках PostgreSQL.\n"
        "SQL из каталога может быть MySQL — для execute_sql перепиши под PostgreSQL "
        "(например `order details` → \"order details\").\n"
        "Пайплайн:\n"
        "1) Выбери подходящий SQL-шаблон из mrt.\n"
        "2) Подставь параметры из блока «Параметры».\n"
        "3) Вызови execute_sql с готовым SQL (при необходимости сначала list_schemas / "
        "list_objects / get_object_details).\n"
        "4) По строкам примени логику виджетов (Sum/Count, форматы, фильтры шаблона).\n"
        "5) Ответь на языке вопроса. Не выдумывай числа.\n\n"
        f"SQL из mrt:\n{templates_body}\n\n"
        f"Дефолтные параметры:\n{defaults_body}\n\n"
        f"Полный каталог:\n{catalog_body}"
    )


def user_message(question: str, resolved: dict[str, Any]) -> str:
    if not resolved:
        return question
    flat = {name: meta["value"] for name, meta in resolved.items()}
    origins = {name: meta["source"] for name, meta in resolved.items()}
    return (
        f"{question}\n\n"
        "Параметры (из вопроса или дефолт mrt):\n"
        f"{json.dumps(flat, ensure_ascii=False, indent=2)}\n"
        f"Откуда: {json.dumps(origins, ensure_ascii=False)}"
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
) -> str:
    known = {tool["function"]["name"] for tool in tools}
    messages: list[dict] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_message(question, resolved)},
    ]
    used_tools = False
    empty_retries = 0
    log.info(
        "Вопрос: %s | resolved=%s | tools=%s",
        preview(question, 200),
        preview(resolved, 300),
        sorted(known),
    )
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
            "SQL template %s placeholders=%s sql=%s",
            item.get("source"),
            [p["name"] for p in item.get("placeholders") or []],
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
    log.info(
        "POST /ask question=%s resolved=%s",
        preview(question, 200),
        preview(resolved, 300),
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
        "parameters": {name: meta["value"] for name, meta in resolved.items()},
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
