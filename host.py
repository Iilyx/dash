#!/usr/bin/env python3
"""Chat host: catalog in the prompt, SQL through the official Postgres MCP server.

The model is any OpenAI-compatible server. The host calls POST /v1/chat/completions
with httpx. On startup it runs the ready Postgres MCP server
@modelcontextprotocol/server-postgres. Docker installs that package. Locally,
if mcp-server-postgres is not on PATH, the host falls back to npx.

Environment:
    DATABASE_URL   postgresql://readonly:pass@localhost:5432/db
    LLM_BASE_URL   OpenAI-compatible root, for example http://127.0.0.1:11434/v1
    LLM_MODEL      model name on that server
    LLM_API_KEY    optional bearer token
    CATALOG_PATH   default catalog.json
    HOST           default 127.0.0.1, set 0.0.0.0 in Docker
    PORT           default 8080

POST /ask  {"question": "..."}  ->  {"answer": "..."}
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field

POSTGRES_MCP = "@modelcontextprotocol/server-postgres"
MAX_TOOL_ROUNDS = 4
MAX_TOOL_CHARS = 24_000


class Ask(BaseModel):
    question: str = Field(min_length=1)


def load_catalog(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def system_prompt(catalog: dict) -> str:
    body = json.dumps(catalog, ensure_ascii=False, indent=2)
    return (
        "Ты отвечаешь на вопросы по данным дашборда. "
        "В каталоге нет живых чисел, только источники, колонки, исходный SQL и выражения виджетов. "
        "Чтобы получить цифры, вызови инструмент query с одним SQL-запросом для PostgreSQL. "
        "Запрос из каталога может быть на другом диалекте: для query перепиши его под PostgreSQL. "
        "Сервер выполняет запрос в read-only транзакции. "
        "Не выдумывай числа. Если запрос не удался, скажи об этом. Отвечай на языке вопроса.\n\n"
        f"Каталог:\n{body}"
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
                "function": {"name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments or {})},
            }
        )
        pending.append((call_id, name, arguments))
    assistant = {
        "role": "assistant",
        "content": message.get("content"),
        "tool_calls": stored,
    }
    return assistant, pending


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
) -> str:
    known = {tool["function"]["name"] for tool in tools}
    messages: list[dict] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": question},
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


def postgres_server_parameters(database_url: str) -> StdioServerParameters:
    binary = shutil.which("mcp-server-postgres")
    if binary:
        return StdioServerParameters(command=binary, args=[database_url])
    npx = shutil.which("npx")
    if npx is None:
        raise RuntimeError(
            "Не найден mcp-server-postgres и npx. В Docker образ ставит "
            f"{POSTGRES_MCP}. Локально нужен Node.js."
        )
    return StdioServerParameters(
        command=npx,
        args=["-y", POSTGRES_MCP, database_url],
    )


def llm_base_url() -> str:
    raw = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    if not raw:
        raise RuntimeError("Нужна переменная LLM_BASE_URL, например http://127.0.0.1:11434/v1")
    return raw


@asynccontextmanager
async def lifespan(app: FastAPI):
    database_url = os.environ.get("DATABASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not database_url or not model:
        raise RuntimeError("Нужны переменные DATABASE_URL и LLM_MODEL")
    catalog_path = Path(os.environ.get("CATALOG_PATH", "catalog.json"))
    prompt = system_prompt(load_catalog(catalog_path))
    headers = {}
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = postgres_server_parameters(database_url)
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
                app.state.lock = asyncio.Lock()
                yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "tools": [tool["function"]["name"] for tool in app.state.tools]}


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
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=f"LLM: {exc}") from exc
    if not answer:
        raise HTTPException(status_code=502, detail="model returned an empty answer")
    return {"answer": answer}


def main() -> None:
    missing = [name for name in ("DATABASE_URL", "LLM_BASE_URL", "LLM_MODEL") if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Задай {', '.join(missing)}")
    if shutil.which("mcp-server-postgres") is None and shutil.which("npx") is None:
        raise SystemExit(
            "Не найден mcp-server-postgres и npx. В Docker образ ставит "
            f"{POSTGRES_MCP}. Локально нужен Node.js."
        )
    uvicorn.run(
        "host:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
