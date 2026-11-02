#!/usr/bin/env python3
"""Ask pipeline: .mrt catalog → LLM → Postgres MCP Pro → answer.

Like the Habr / Claude Desktop setup:
  docker compose up -d postgres
  docker run -i --rm -e DATABASE_URI=... crystaldba/postgres-mcp --access-mode=restricted

The host spawns that docker run over stdio. LLM stays on the machine.
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

log = logging.getLogger("host")


class Ask(BaseModel):
    question: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


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


def default_parameters(catalog: dict) -> list[dict]:
    defaults: list[dict] = []
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
            if not name or value in (None, ""):
                continue
            defaults.append(
                {
                    "kind": "source_parameter",
                    "source": source.get("name"),
                    "name": name,
                    "sql_placeholder": f"@{name}",
                    "value": value,
                }
            )
    for variable in catalog.get("variables") or []:
        if not isinstance(variable, dict):
            continue
        name = variable.get("name")
        value = variable.get("value")
        if not name or value in (None, ""):
            continue
        item = {"kind": "variable", "name": name, "value": value}
        dialog = variable.get("dialog")
        if isinstance(dialog, dict) and dialog.get("items"):
            item["allowed"] = dialog.get("items")
        defaults.append(item)
    return defaults


def system_prompt(catalog: dict) -> str:
    defaults = default_parameters(catalog)
    body = json.dumps(catalog, ensure_ascii=False, indent=2)
    defaults_body = json.dumps(defaults, ensure_ascii=False, indent=2)
    return (
        "Ты отвечаешь на вопросы по дашборду Stimulsoft.\n"
        "Пайплайн ответа:\n"
        "1) Найди в каталоге подходящий SQL/источник и связанные таблицы.\n"
        "2) Подставь параметры: если пользователь не задал другое значение, "
        "используй дефолты из раздела «Дефолтные параметры». "
        "Плейсхолдеры вида @Name замени на эти значения в PostgreSQL SQL.\n"
        "3) Для данных вызови инструмент execute_sql с одним SQL для PostgreSQL. "
        "При необходимости сначала list_schemas / list_objects / get_object_details. "
        "SQL из каталога может быть MySQL — перепиши под PostgreSQL. "
        "MCP в режиме restricted: только чтение.\n"
        "4) После строк из execute_sql примени логику дашборда из каталога: "
        "выражения виджетов (Sum, Count и т.д.), фильтры шаблона, "
        "форматы чисел (decimals, currency, percentage, abbreviation). "
        "Это отображаемая логика, не обязательно SQL ROUND.\n"
        "5) Верни итоговый ответ на языке вопроса. Не выдумывай числа. "
        "Если SQL упал — скажи об ошибке.\n\n"
        f"Дефолтные параметры:\n{defaults_body}\n\n"
        f"Каталог:\n{body}"
    )


def user_message(question: str, overrides: dict[str, Any]) -> str:
    if not overrides:
        return question
    body = json.dumps(overrides, ensure_ascii=False, indent=2)
    return (
        f"{question}\n\n"
        "Параметры из запроса. Они важнее дефолтов каталога:\n"
        f"{body}"
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
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if content is None:
        return ""
    return str(content).strip()


async def answer_question(
    *,
    session: ClientSession,
    llm: httpx.AsyncClient,
    model: str,
    tools: list[dict],
    prompt: str,
    question: str,
    parameters: dict[str, Any],
) -> str:
    known = {tool["function"]["name"] for tool in tools}
    messages: list[dict] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_message(question, parameters)},
    ]
    log.info(
        "Вопрос: %s | overrides=%s | tools=%s",
        preview(question, 200),
        preview(parameters or {}, 200),
        sorted(known),
    )
    for round_no in range(1, MAX_TOOL_ROUNDS + 2):
        log.info("LLM round %s → %s model=%s messages=%s", round_no, llm_base_url(), model, len(messages))
        started = time.perf_counter()
        response = await llm.post(
            "chat/completions",
            json={"model": model, "messages": messages, "tools": tools, "stream": False},
        )
        response.raise_for_status()
        payload = response.json()
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM response has no choices") from exc
        assistant, pending = pending_tool_calls(message)
        log.info(
            "LLM round %s за %.2fs tool_calls=%s answer=%s",
            round_no,
            time.perf_counter() - started,
            [name for _, name, _ in pending] or None,
            preview(message_text(message), 200) or None,
        )
        if not pending:
            return message_text(message)
        messages.append(assistant)
        for call_id, name, raw_arguments in pending:
            try:
                arguments = parse_arguments(raw_arguments)
            except (ValueError, json.JSONDecodeError) as exc:
                log.warning("Плохие args для %s: %s", name, exc)
                text = f"Bad tool arguments: {exc}"
            else:
                text = await run_tool(session, name, arguments, known)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
    log.error("Слишком много раундов MCP для вопроса: %s", preview(question, 200))
    return "Модель не закончила ответ: слишком много вызовов MCP."


def mcp_database_uri() -> str:
    """From the MCP container, localhost is remapped; use host.docker.internal."""
    url = database_url()
    for host in ("127.0.0.1", "localhost"):
        url = url.replace(f"@{host}:", "@host.docker.internal:")
        url = url.replace(f"//{host}:", "//host.docker.internal:")
    return url


def postgres_mcp() -> StdioServerParameters:
    """Same as the article / Claude Desktop config."""
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
    defaults = default_parameters(catalog)
    prompt = system_prompt(catalog)
    log.info(
        "Промпт: report=%s defaults=%s prompt_chars=%s",
        (catalog.get("report") or {}).get("name"),
        defaults,
        len(prompt),
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
                app.state.defaults = defaults
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
        "llm": llm_base_url(),
        "mcp": "docker run -i --rm crystaldba/postgres-mcp --access-mode=restricted",
    }


@app.post("/ask")
async def ask(body: Ask) -> dict:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")
    log.info("POST /ask question=%s parameters=%s", preview(question, 200), preview(body.parameters or {}, 200))
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
                parameters=body.parameters,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            log.exception("Ошибка на /ask: %s", exc)
            raise HTTPException(status_code=502, detail=f"LLM: {exc}") from exc
    if not answer:
        log.error("Пустой ответ модели")
        raise HTTPException(status_code=502, detail="model returned an empty answer")
    log.info("Ответ за %.2fs: %s", time.perf_counter() - started, preview(answer, 300))
    return {"answer": answer}


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
