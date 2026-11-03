#!/usr/bin/env python3
"""FastAPI ask (просто):

1) MRT → catalog + catalog_pages/
2) берём первый page (catalog_pages/0.json)
3) POST /ask → LLM, в ответе то что модель вернула
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from extract_mrt import catalog_from_report, load_mrt, write_page_packs

ROOT = Path(__file__).resolve().parent
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


def llm_base_url() -> str:
    raw = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1").strip().rstrip("/")
    if not raw.endswith("/v1"):
        raw = f"{raw}/v1"
    return raw


def llm_timeout() -> float:
    return float(os.environ.get("LLM_TIMEOUT", "120").strip() or "120")


def ensure_catalog() -> dict:
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
        return catalog
    if catalog_path.is_file():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        write_page_packs(catalog, pages_path)
        return catalog
    raise RuntimeError("Нужен MRT_PATH или catalog.json")


def load_first_page_catalog() -> tuple[dict, Path]:
    pages_dir = resolve_path(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages"))
    path = pages_dir / "0.json"
    if not path.is_file():
        raise RuntimeError(f"нет {path}")
    return json.loads(path.read_text(encoding="utf-8")), path


def page_name(catalog: dict) -> str:
    pages = catalog.get("pages") or []
    if not pages or not isinstance(pages[0], dict):
        return "page_0"
    page = pages[0]
    alias = page.get("alias")
    name = page.get("name")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "page_0"


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


def _delta_text(delta: dict) -> str:
    for key in ("content", "text", "reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


async def llm_chat(llm: httpx.AsyncClient, *, model: str, messages: list[dict]) -> str:
    """Stream tokens to console, return full text for /ask JSON."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": True,
    }
    log.info(
        "LLM → %s model=%s chars=%s stream=true",
        llm_base_url(),
        model,
        sum(len(str(m.get("content") or "")) for m in messages),
    )
    started = time.perf_counter()
    parts: list[str] = []
    print("\n=== LLM stream ===", flush=True)
    async with llm.stream("POST", "chat/completions", json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith(":"):
                continue
            if not line.startswith("data:"):
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
            delta = choice.get("delta") or {}
            piece = _delta_text(delta if isinstance(delta, dict) else {})
            if not piece:
                # некоторые серверы шлют целиком message на чанк
                msg = choice.get("message") or {}
                piece = message_text(msg if isinstance(msg, dict) else {})
            if not piece:
                piece = _as_text(choice.get("text"))
            if piece:
                parts.append(piece)
                print(piece, end="", flush=True)
    print("\n=== end stream ===\n", flush=True)
    text = "".join(parts).strip()
    log.info("LLM ← %.2fs chars=%s", time.perf_counter() - started, len(text))
    if not text:
        raise RuntimeError("LLM empty content (stream)")
    return text


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("Нужна LLM_MODEL в .env")
    ensure_catalog()
    page_catalog, page_path = load_first_page_catalog()
    headers = {}
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(
        base_url=llm_base_url(),
        headers=headers,
        timeout=llm_timeout(),
    ) as llm:
        app.state.llm = llm
        app.state.model = model
        app.state.page_catalog = page_catalog
        app.state.page_path = str(page_path)
        app.state.page = page_name(page_catalog)
        log.info("Ready llm=%s model=%s page=%s file=%s", llm_base_url(), model, app.state.page, page_path)
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
    return {
        "ok": True,
        "llm": llm_base_url(),
        "model": app.state.model,
        "page": app.state.page,
        "catalog": app.state.page_path,
    }


@app.post("/ask")
async def ask(body: Ask) -> dict:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")

    prompt = (
        "You are looking at one Stimulsoft dashboard page catalog (JSON).\n"
        "Answer the user question using only this catalog. Be concise.\n\n"
        f"Page catalog:\n{json.dumps(app.state.page_catalog, ensure_ascii=False)}\n\n"
        f"Question: {question}"
    )
    started = time.perf_counter()
    try:
        answer = await llm_chat(
            app.state.llm,
            model=app.state.model,
            messages=[{"role": "user", "content": prompt}],
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"LLM timeout after {llm_timeout()}s") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        log.exception("ask failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    log.info("ask %.2fs page=%s", time.perf_counter() - started, app.state.page)
    return {
        "page": app.state.page,
        "catalog": app.state.page_path,
        "answer": answer,
    }


def main() -> None:
    load_env()
    setup_logging()
    if not os.environ.get("LLM_MODEL", "").strip():
        raise SystemExit("Задай LLM_MODEL в .env")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run("host:app", host=host, port=port)


if __name__ == "__main__":
    main()
