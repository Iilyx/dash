#!/usr/bin/env python3
"""FastAPI ask (reasoning low):

1) MRT → catalog + catalog_pages
2) POST /ask → LLM API выбирает page
3) вернуть название page
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

from extract_mrt import (
    build_page_index,
    catalog_from_report,
    load_mrt,
    write_page_packs,
)

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


def page_label(entry: dict) -> str:
    alias = entry.get("alias")
    name = entry.get("name")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    if isinstance(name, str) and name.strip():
        return name.strip()
    return f"page_{entry.get('index', entry.get('i', 0))}"


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


def slim_pages(catalog: dict) -> list[dict]:
    """Короткий index для LLM — reasoning low."""
    out: list[dict] = []
    for entry in build_page_index(catalog):
        titles = entry.get("titles") or []
        if not isinstance(titles, list):
            titles = []
        item = {
            "i": entry.get("index"),
            "page": page_label(entry),
            "name": entry.get("name"),
            "alias": entry.get("alias"),
            "titles": [str(t) for t in titles[:5] if t] or None,
        }
        out.append({k: v for k, v in item.items() if v not in (None, "", [])})
    return out


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


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
    """Достаём текст из content и fallback-полей локальных серверов."""
    for key in (
        "content",
        "text",
        "reasoning_content",
        "reasoning",
        "output_text",
        "refusal",
    ):
        text = _as_text(message.get(key))
        if text:
            return text
    return ""


def resolve_page_ref(catalog: dict, ref: Any) -> dict:
    index = build_page_index(catalog)
    by_i = {int(e["index"]): e for e in index if "index" in e}
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        entry = by_i.get(int(ref))
        if entry:
            return entry
        raise KeyError(ref)
    needle = str(ref or "").strip().lower()
    for entry in index:
        for key in ("alias", "name"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip().lower() == needle:
                return entry
        if page_label(entry).lower() == needle:
            return entry
    raise KeyError(ref)


async def llm_chat(llm: httpx.AsyncClient, *, model: str, messages: list[dict]) -> str:
    # Без stop: у llama.cpp stop="\n\n" часто даёт пустой content.
    # Без отдельного system: часть шаблонов его глотает → empty.
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }
    log.info(
        "LLM → %s model=%s chars=%s",
        llm_base_url(),
        model,
        sum(len(str(m.get("content") or "")) for m in messages),
    )
    started = time.perf_counter()
    response = await llm.post("chat/completions", json=payload)
    response.raise_for_status()
    body = response.json()
    try:
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"bad LLM response: {body!r}"[:800]) from exc
    text = message_text(message if isinstance(message, dict) else {})
    # иногда текст лежит на choice, не в message
    if not text and isinstance(choice, dict):
        text = _as_text(choice.get("text"))
    usage = body.get("usage") or {}
    elapsed = time.perf_counter() - started
    log.info(
        "LLM ← %.2fs finish=%s usage=%s answer=%s",
        elapsed,
        choice.get("finish_reason") if isinstance(choice, dict) else None,
        usage,
        (text[:200] + "…") if len(text) > 200 else text,
    )
    if not text:
        log.error("LLM empty raw choice=%s", json.dumps(choice, ensure_ascii=False)[:1000])
        raise RuntimeError(
            "LLM empty content; raw="
            + json.dumps(choice, ensure_ascii=False)[:500]
        )
    return text


async def select_page_via_llm(
    llm: httpx.AsyncClient,
    *,
    model: str,
    catalog: dict,
    question: str,
) -> dict[str, Any]:
    index = build_page_index(catalog)
    if not index:
        raise RuntimeError("нет pages")
    if len(index) == 1:
        entry = index[0]
        return {
            "index": int(entry["index"]),
            "name": entry.get("name"),
            "alias": entry.get("alias"),
            "page": page_label(entry),
            "reason": "single_page",
        }

    pages = slim_pages(catalog)
    # один user-message — стабильнее на llama.cpp, чем system+user
    prompt = (
        "Pick one dashboard page for the user question. "
        'Reply ONLY with JSON: {"page":"<page name or index>"}.\n'
        f"Question: {question}\n"
        f"Pages: {json.dumps(pages, ensure_ascii=False, separators=(',', ':'))}"
    )
    raw = await llm_chat(
        llm,
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = extract_json_object(raw) or {}
    ref = parsed.get("page", parsed.get("pages"))
    if isinstance(ref, list) and ref:
        ref = ref[0]
    if ref is None:
        raise RuntimeError(f"LLM did not return page: {raw[:300]}")
    entry = resolve_page_ref(catalog, ref)
    return {
        "index": int(entry["index"]),
        "name": entry.get("name"),
        "alias": entry.get("alias"),
        "page": page_label(entry),
        "reason": "llm",
        "raw": raw,
    }


def write_page_txt(selected: dict[str, Any], question: str) -> Path:
    path = resolve_path(os.environ.get("PAGE_DUMP_PATH", "page.txt"))
    path.write_text(
        f"{selected.get('page')}\t{selected.get('index')}\t{selected.get('name') or ''}\t"
        f"{selected.get('alias') or ''}\t{question}\n",
        encoding="utf-8",
    )
    return path


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("Нужна LLM_MODEL в .env")
    catalog = ensure_catalog()
    page_index = build_page_index(catalog)
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
        app.state.catalog = catalog
        app.state.page_index = page_index
        app.state.report = (catalog.get("report") or {}).get("name")
        log.info(
            "Ready llm=%s model=%s pages=%s",
            llm_base_url(),
            model,
            [page_label(e) for e in page_index],
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
    return {
        "ok": True,
        "report": app.state.report,
        "llm": llm_base_url(),
        "model": app.state.model,
        "pages": [
            {"index": e.get("index"), "page": page_label(e)}
            for e in app.state.page_index
        ],
    }


@app.post("/ask")
async def ask(body: Ask) -> dict:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")
    started = time.perf_counter()
    try:
        selected = await select_page_via_llm(
            app.state.llm,
            model=app.state.model,
            catalog=app.state.catalog,
            question=question,
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail=f"LLM timeout after {llm_timeout()}s",
        ) from exc
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        log.exception("ask failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    write_page_txt(selected, question)
    log.info(
        "ask %.2fs page=%s reason=%s",
        time.perf_counter() - started,
        selected["page"],
        selected.get("reason"),
    )
    return {"page": selected["page"]}


def main() -> None:
    load_env()
    setup_logging()
    missing = []
    if not os.environ.get("LLM_MODEL", "").strip():
        missing.append("LLM_MODEL")
    if missing:
        raise SystemExit(f"Задай {', '.join(missing)} в .env")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run("host:app", host=host, port=port)


if __name__ == "__main__":
    main()
