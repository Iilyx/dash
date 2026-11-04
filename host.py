#!/usr/bin/env python3
"""FastAPI ask:

1) MRT → catalog_pages/N.json
2) по каждому page вытащить человекочитаемый/русский текст
3) LLM: какие pages подходят под вопрос → стрим в консоль + answer
"""

from __future__ import annotations

import json
import logging
import os
import re
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
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def llm_thinking_options() -> dict[str, Any]:
    """Параметры thinking для Qwen 3.5 (и совместимых серверов)."""
    effort = os.environ.get("LLM_REASONING_EFFORT", "low").strip().lower() or "low"
    # none / off → выключить thinking
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
        # Qwen: thinking_budget и reasoning_effort вместе иногда нельзя —
        # если задан budget, effort не шлём
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
    """Человекочитаемые подписи из page: title/text/alias/label/expression…"""
    if isinstance(node, dict):
        for key in (
            "alias",
            "title",
            "text",
            "label",
            "name",
            "description",
            "expression",
        ):
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
    """По каждому catalog_pages/N.json — карточка с русским/видимым текстом."""
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
        # приоритет: строки с кириллицей, потом остальные подписи
        ru = [t for t in texts if CYRILLIC.search(t)]
        other = [t for t in texts if t not in ru]
        visible = ru + other
        # убрать технические Ident-имена виджетов вроде Text1 если есть нормальные titles
        cards.append(
            {
                "index": index,
                "page": page_label(page, index),
                "name": page.get("name"),
                "alias": page.get("alias"),
                "catalog": str(path),
                "ru_text": visible[:40],
            }
        )
    if not cards:
        raise RuntimeError(f"нет page-файлов в {pages_dir}")
    return cards


SYSTEM_PROMPT = """Ты помогаешь выбрать страницы Stimulsoft-дашборда под вопрос пользователя.

Тебе дан список pages. У каждой есть:
- index — номер файла catalog_pages/{index}.json
- page — название
- ru_text — человекочитаемый текст со страницы (заголовки, подписи, русский текст и другие видимые строки)

Задача:
1) Пройдись по КАЖДОЙ page из списка.
2) Смотри в первую очередь на русский и понятный текст в ru_text.
3) Для каждой page коротко скажи: подходит под вопрос или нет (да подходит / не подходит) и почему в одном предложении.
4) В конце перечисли только подходящие: их page и index.

Отвечай по-русски, коротко.
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


def _delta_text(delta: dict) -> str:
    for key in ("content", "text", "reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


async def llm_chat(llm: httpx.AsyncClient, *, model: str, messages: list[dict]) -> str:
    thinking = llm_thinking_options()
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": True,
        **thinking,
    }
    log.info(
        "LLM → %s model=%s chars=%s stream=true thinking=%s",
        llm_base_url(),
        model,
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
    print("\n=== LLM stream ===", flush=True)
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
    print("\n=== end stream ===\n", flush=True)
    text = "".join(answer_parts).strip() or "".join(think_parts).strip()
    log.info(
        "LLM ← %.2fs answer_chars=%s think_chars=%s",
        time.perf_counter() - started,
        len("".join(answer_parts)),
        len("".join(think_parts)),
    )
    if not text:
        raise RuntimeError("LLM empty content (stream)")
    return text


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("Нужна LLM_MODEL в .env")
    ensure_catalog()
    cards = load_page_cards()
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
        app.state.page_cards = cards
        thinking = llm_thinking_options()
        log.info(
            "Ready llm=%s model=%s thinking=%s pages=%s",
            llm_base_url(),
            model,
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

    cards = app.state.page_cards
    user = (
        f"Вопрос пользователя:\n{question}\n\n"
        f"Список pages (пройдись по каждой):\n"
        f"{json.dumps(cards, ensure_ascii=False, indent=2)}"
    )
    started = time.perf_counter()
    try:
        answer = await llm_chat(
            app.state.llm,
            model=app.state.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"LLM timeout after {llm_timeout()}s") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        log.exception("ask failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    log.info("ask %.2fs pages_checked=%s", time.perf_counter() - started, len(cards))
    return {
        "pages_checked": len(cards),
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
