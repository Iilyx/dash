#!/usr/bin/env python3
"""Minimal ask host:

1) MRT → catalog.json + catalog_pages/
2) POST /ask → LLM выбирает pages по index
3) выбранные pages пишутся в page.txt
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
LOG_PREVIEW = 400

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


def preview(value: Any, limit: int = LOG_PREVIEW) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


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


def llm_base_url() -> str:
    raw = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    if not raw:
        raise RuntimeError("Нужна LLM_BASE_URL, например http://127.0.0.1:8080/v1")
    if not raw.endswith("/v1"):
        raw = f"{raw}/v1"
    return raw


def llm_timeout() -> float:
    raw = os.environ.get("LLM_TIMEOUT", "300").strip() or "300"
    return float(raw)


def page_dump_path() -> Path:
    return resolve_path(os.environ.get("PAGE_DUMP_PATH", "page.txt"))


def ensure_catalog() -> dict:
    catalog_path = resolve_path(os.environ.get("CATALOG_PATH", "catalog.json"))
    pages_path = resolve_path(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages"))
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
            write_page_packs(catalog, pages_path)
            log.info(
                "Каталог готов → %s pages=%s",
                catalog_path,
                len(catalog.get("pages") or []),
            )
            return catalog
    if not catalog_path.is_file():
        raise RuntimeError("Нужен MRT_PATH или существующий CATALOG_PATH")
    log.info("Читаю готовый каталог %s", catalog_path)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    write_page_packs(catalog, pages_path)
    return catalog


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


def message_text(message: dict) -> str:
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
    return "\n".join(parts).strip()


def resolve_page_ref(catalog: dict, ref: Any) -> int:
    pages = [page for page in (catalog.get("pages") or []) if isinstance(page, dict)]
    if isinstance(ref, bool):
        raise KeyError(f"bad page ref: {ref}")
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        index = int(ref)
        if 0 <= index < len(pages):
            return index
        raise KeyError(f"page index out of range: {ref}")
    text_ref = str(ref or "").strip().lower()
    if not text_ref:
        raise KeyError("empty page ref")
    for index, page in enumerate(pages):
        for key in ("name", "alias"):
            value = page.get(key)
            if isinstance(value, str) and value.strip().lower() == text_ref:
                return index
    raise KeyError(f"page not found: {ref}")


def pages_detail(catalog: dict, indexes: list[int]) -> list[dict]:
    index = build_page_index(catalog)
    by_i = {int(entry["index"]): entry for entry in index if "index" in entry}
    details: list[dict] = []
    for i in indexes:
        entry = by_i.get(i) or {"index": i}
        details.append(
            {
                "index": i,
                "name": entry.get("name"),
                "alias": entry.get("alias"),
                "titles": entry.get("titles"),
                "element_count": entry.get("element_count"),
                "element_names": entry.get("element_names"),
            }
        )
    return details


def write_page_txt(question: str, details: list[dict], raw_llm: str) -> Path:
    path = page_dump_path()
    lines = [
        "=== QUESTION ===",
        question,
        "",
        "=== PAGES ===",
        json.dumps(details, ensure_ascii=False, indent=2),
        "",
        "=== LLM RAW ===",
        raw_llm.strip(),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("page.txt → %s (%s pages)", path, len(details))
    return path


async def llm_chat(llm: httpx.AsyncClient, *, model: str, messages: list[dict]) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }
    log.info(
        "LLM → %s model=%s messages=%s",
        llm_base_url(),
        model,
        len(messages),
    )
    started = time.perf_counter()
    response = await llm.post("chat/completions", json=payload)
    response.raise_for_status()
    body = response.json()
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        log.error("LLM bad payload: %s", preview(body, 800))
        raise RuntimeError("LLM response has no choices") from exc
    text = message_text(message)
    log.info("LLM ← %.2fs answer=%s", time.perf_counter() - started, preview(text, 300))
    if not text:
        raise RuntimeError("LLM returned empty content")
    return text


async def select_pages(
    llm: httpx.AsyncClient,
    *,
    model: str,
    catalog: dict,
    question: str,
) -> tuple[list[dict], str]:
    pages = [page for page in (catalog.get("pages") or []) if isinstance(page, dict)]
    if not pages:
        return [], ""
    if len(pages) == 1:
        details = pages_detail(catalog, [0])
        raw = '{"pages":[0]}'
        return details, raw

    index = build_page_index(catalog)
    system = (
        "Ты роутер Stimulsoft-дашборда. По вопросу выбери какие pages нужны.\n"
        "Верни ТОЛЬКО JSON вида {\"pages\":[0,2]} — индексы из списка ниже.\n"
        "Если не уверен — верни все индексы. Никакого текста кроме JSON."
    )
    user = (
        f"Вопрос: {question}\n\n"
        f"Pages index:\n{json.dumps(index, ensure_ascii=False, indent=2)}"
    )
    raw = await llm_chat(
        llm,
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    parsed = extract_json_object(raw) or {}
    selected: list[int] = []
    for item in parsed.get("pages") or []:
        try:
            selected.append(resolve_page_ref(catalog, item))
        except KeyError:
            continue
    ordered: list[int] = []
    for index_i in selected:
        if index_i not in ordered:
            ordered.append(index_i)
    if not ordered:
        log.warning("Router пуст — беру все %s pages", len(pages))
        ordered = list(range(len(pages)))
    return pages_detail(catalog, ordered), raw


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise RuntimeError("Нужна LLM_MODEL")
    log.info("Старт host: llm=%s model=%s timeout=%ss", llm_base_url(), model, llm_timeout())
    catalog = ensure_catalog()
    page_index = build_page_index(catalog)
    log.info(
        "Каталог: report=%s pages=%s",
        (catalog.get("report") or {}).get("name"),
        len(page_index),
    )
    for entry in page_index:
        log.info(
            "page[%s] %s alias=%s elements=%s",
            entry.get("index"),
            entry.get("name"),
            entry.get("alias"),
            entry.get("element_count"),
        )
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
        log.info("Host готов: POST /ask → LLM pages → page.txt")
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
        "pages": app.state.page_index,
        "llm": llm_base_url(),
        "model": app.state.model,
        "page_txt": str(page_dump_path()),
    }


@app.post("/ask")
async def ask(body: Ask) -> dict:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")
    started = time.perf_counter()
    try:
        details, raw = await select_pages(
            app.state.llm,
            model=app.state.model,
            catalog=app.state.catalog,
            question=question,
        )
    except httpx.TimeoutException as exc:
        log.exception("LLM timeout")
        raise HTTPException(
            status_code=504,
            detail=f"LLM timeout after {llm_timeout()}s — увеличь LLM_TIMEOUT или ускорь модель",
        ) from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        log.exception("Ошибка /ask: %s", exc)
        raise HTTPException(status_code=502, detail=f"ask failed: {exc}") from exc

    path = write_page_txt(question, details, raw)
    elapsed = time.perf_counter() - started
    log.info("Ask done in %.2fs pages=%s", elapsed, preview(details, 300))
    return {
        "question": question,
        "pages": [item.get("index") for item in details],
        "pages_detail": details,
        "page_txt": str(path),
        "elapsed_s": round(elapsed, 2),
        "llm_raw": raw,
    }


def main() -> None:
    load_env()
    setup_logging()
    missing = []
    if not os.environ.get("LLM_BASE_URL", "").strip():
        missing.append("LLM_BASE_URL")
    if not os.environ.get("LLM_MODEL", "").strip():
        missing.append("LLM_MODEL")
    if missing:
        raise SystemExit(f"Задай {', '.join(missing)}")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8090"))
    log.info("Слушаю http://%s:%s", host, port)
    uvicorn.run("host:app", host=host, port=port)


if __name__ == "__main__":
    main()
