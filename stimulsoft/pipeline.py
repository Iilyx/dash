#!/usr/bin/env python3
"""Processing pipeline: .mrt to catalog, then one chat completion.

The local model is OpenAI-compatible. This service is only the /ask endpoint
the viewer calls. It does not listen on the model port.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from extract_mrt import catalog_from_report, load_mrt

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent


class Filter(BaseModel):
    element: str = ""
    path: str = ""
    condition: str = ""
    value: str = ""
    value2: str = ""


class Ask(BaseModel):
    question: str = Field(min_length=1)
    filters: list[Filter] = Field(default_factory=list)


def load_env() -> None:
    for path in (REPO / ".env", ROOT / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def mrt_path() -> Path:
    raw = os.environ.get("MRT_PATH", "stimulsoft/static/dashboards/SalesOfProducts.mrt")
    path = Path(raw)
    if not path.is_absolute():
        path = REPO / path
    return path


def llm_base_url() -> str:
    raw = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080").strip().rstrip("/")
    if not raw:
        raise RuntimeError("Нужна LLM_BASE_URL, например http://127.0.0.1:8080")
    return raw


def completions_url() -> str:
    base = llm_base_url()
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def models_url() -> str:
    base = llm_base_url()
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def http_json(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"LLM {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM не отвечает на {llm_base_url()}: {exc.reason}") from exc


def model_name() -> str:
    configured = os.environ.get("LLM_MODEL", "").strip()
    if configured:
        return configured
    payload = http_json(models_url())
    items = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(items, list) and items:
        name = items[0].get("id") or items[0].get("name")
        if name:
            return str(name)
    raise RuntimeError("LLM_MODEL пустой, и сервер не вернул список моделей")


def system_prompt(catalog: dict) -> str:
    body = json.dumps(catalog, ensure_ascii=False, indent=2)
    return (
        "Ты отвечаешь по дашборду Stimulsoft. Ниже каталог, собранный из .mrt. "
        "В нём источники, SQL, колонки, параметры, связи, переменные с DialogInfo, "
        "выражения виджетов, формат чисел и фильтры, заданные в шаблоне. "
        "Живых чисел в каталоге нет. "
        "Если в вопросе есть текущие фильтры экрана, добавь те же ограничения. "
        "Формат виджета (знаки, валюта, проценты, сокращение) используй при показе числа. "
        "Не выдумывай цифры. Отвечай на языке вопроса.\n\n"
        f"Каталог:\n{body}"
    )


def user_message(question: str, filters: list[Filter]) -> str:
    lines = []
    for item in filters:
        parts = [piece for piece in (item.path, item.condition, item.value) if piece]
        if item.value2:
            parts.append(item.value2)
        if not parts:
            continue
        line = " ".join(parts)
        if item.element:
            line = f"{item.element}: {line}"
        lines.append(line)
    if not lines:
        return question
    body = "\n".join(f"- {line}" for line in lines)
    return f"{question}\n\nТекущие фильтры дашборда:\n{body}"


def complete(question: str, filters: list[Filter]) -> str:
    catalog = app.state.catalog
    payload = http_json(
        completions_url(),
        {
            "model": model_name(),
            "messages": [
                {"role": "system", "content": system_prompt(catalog)},
                {"role": "user", "content": user_message(question, filters)},
            ],
            "stream": False,
        },
    )
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM response has no choices") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("model returned an empty answer")
    return content.strip()


load_env()
app = FastAPI()
app.state.catalog = catalog_from_report(load_mrt(mrt_path()))


@app.get("/health")
def health() -> dict:
    report = app.state.catalog.get("report") or {}
    return {"ok": True, "report": report.get("name"), "llm": llm_base_url(), "mrt": str(mrt_path())}


@app.post("/ask")
def ask(body: Ask) -> dict:
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is empty")
    try:
        return {"answer": complete(question, body.filters)}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def main() -> None:
    uvicorn.run(
        app,
        host=os.environ.get("ASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("ASK_PORT", "8090")),
    )


if __name__ == "__main__":
    main()
