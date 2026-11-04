#!/usr/bin/env python3
""" /ask — LLM выбирает page, catalog пишем в ask_trace.txt """

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from extract_mrt import catalog_from_report, load_mrt, write_page_packs

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("host")


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


def parse_page_index(text: str, cards: list[dict]) -> int:
    valid = {c["index"] for c in cards}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1]) if start >= 0 and end > start else {}
    for item in (data.get("pages") if isinstance(data, dict) else None) or []:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if idx in valid:
            return idx
    return cards[0]["index"]


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
        log.info("Ready pages=%s", [(c["index"], c["page"]) for c in app.state.cards])
        yield


load_env()
logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "model": app.state.model, "pages": app.state.cards}


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

    system = 'Выбери ОДНУ page. Верни ТОЛЬКО JSON: {"pages":[<index>]}'
    user = f"Вопрос:\n{q}\n\nPages:\n{json.dumps(app.state.cards, ensure_ascii=False, indent=2)}"

    trace = trace_reset()
    trace_step("1. QUESTION", q)
    trace_step("2. PAGES → LLM", app.state.cards)
    trace_step("2. PROMPT", {"system": system, "user": user})

    try:
        raw = await llm(app.state.llm, app.state.model, system, user)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "LLM timeout") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, str(exc)) from exc

    trace_step("3. LLM ROUTE", raw)

    idx = parse_page_index(raw, app.state.cards)
    catalog_file = path_of(os.environ.get("CATALOG_PAGES_DIR", "catalog_pages")) / f"{idx}.json"
    catalog = json.loads(catalog_file.read_text(encoding="utf-8"))
    page = (catalog.get("pages") or [{}])[0]
    name = page.get("alias") or page.get("name") or f"page_{idx}"

    trace_step(
        "4. PAGE SELECTED",
        {"page_index": idx, "page_name": name, "catalog_file": str(catalog_file)},
    )
    trace_step("5. PAGE JSON", page)
    trace_step("6. CATALOG JSON", catalog)
    trace_step(
        "7. RESULT",
        {
            "page_index": idx,
            "page_name": name,
            "page": page,
            "catalog": catalog,
            "route": raw,
            "trace": str(trace),
        },
    )

    log.info("ask page=%s index=%s trace=%s", name, idx, trace)
    return {
        "page_index": idx,
        "page_name": name,
        "page": page,
        "catalog": catalog,  # полный catalog_pages/{index}.json
        "route": raw,
        "trace": str(trace),
    }


def main() -> None:
    load_env()
    if not os.environ.get("LLM_MODEL", "").strip():
        raise SystemExit("Задай LLM_MODEL в .env")
    uvicorn.run("host:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8090")))


if __name__ == "__main__":
    main()
