#!/usr/bin/env python3
"""Ask pipeline: .mrt catalog → LLM → local Postgres MCP → answer.

Postgres and MCP live in docker compose. This host and the LLM run on the machine.

  docker compose up -d db
  .venv/bin/python host.py   # spawns: docker compose run --rm -i mcp
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
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


class Ask(BaseModel):
    question: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


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
            catalog = catalog_from_report(load_mrt(mrt_path))
            catalog_path.write_text(
                json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return catalog
    if not catalog_path.is_file():
        raise RuntimeError("Нужен MRT_PATH или существующий CATALOG_PATH")
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
        "3) Вызови инструмент query с одним SQL-запросом для PostgreSQL. "
        "SQL из каталога может быть MySQL — перепиши под PostgreSQL. "
        "Сервер выполняет query только в read-only транзакции.\n"
        "4) После строк из query примени логику дашборда из каталога: "
        "выражения виджетов (Sum, Count и т.д.), фильтры шаблона, "
        "форматы чисел (decimals, currency, percentage, abbreviation). "
        "Это отображаемая логика, не обязательно SQL ROUND.\n"
        "5) Верни итоговый ответ на языке вопроса. Не выдумывай числа. "
        "Если query упал — скажи об ошибке.\n\n"
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
        return f"Unknown tool: {name}"
    try:
        result = await session.call_tool(name, arguments)
    except Exception as exc:
        return f"SQL error: {exc}"
    return render_tool_result(result)


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
    for _ in range(MAX_TOOL_ROUNDS + 1):
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
        if not pending:
            return message_text(message)
        messages.append(assistant)
        for call_id, name, raw_arguments in pending:
            try:
                arguments = parse_arguments(raw_arguments)
            except (ValueError, json.JSONDecodeError) as exc:
                text = f"Bad tool arguments: {exc}"
            else:
                text = await run_tool(session, name, arguments, known)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
    return "Модель не закончила ответ: слишком много вызовов query."


def postgres_mcp() -> StdioServerParameters:
    """Local MCP from compose: same network as db, stdio to this host."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Нужен Docker: Postgres MCP поднимается из docker-compose")
    return StdioServerParameters(
        command=docker,
        args=["compose", "run", "--rm", "-i", "mcp"],
        cwd=str(ROOT),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("Нужна LLM_MODEL")
    if not database_url():
        raise RuntimeError("Нужны POSTGRES_* в .env (для справки/health; MCP ходит в db из compose)")
    catalog = ensure_catalog()
    prompt = system_prompt(catalog)
    headers = {}
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = postgres_mcp()
    async with httpx.AsyncClient(base_url=llm_base_url(), headers=headers, timeout=180) as llm:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                app.state.session = session
                app.state.llm = llm
                app.state.model = model
                app.state.tools = chat_tools(listed.tools)
                app.state.prompt = prompt
                app.state.defaults = default_parameters(catalog)
                app.state.report = (catalog.get("report") or {}).get("name")
                app.state.lock = asyncio.Lock()
                yield


load_env()
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
        "mcp": "docker compose run mcp",
    }


@app.post("/ask")
async def ask(body: Ask) -> dict:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")
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
            raise HTTPException(status_code=502, detail=f"LLM: {exc}") from exc
    if not answer:
        raise HTTPException(status_code=502, detail="model returned an empty answer")
    return {"answer": answer}


def main() -> None:
    load_env()
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
        raise SystemExit("Нужен Docker для сервисов db и mcp")
    uvicorn.run(
        "host:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8090")),
    )


if __name__ == "__main__":
    main()
