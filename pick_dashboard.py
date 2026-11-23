"""Выбор .mrt по вопросу: инструкция (.md) → LLM → имя mrt → дальше filters/open.

Структура:
  dashboards/
    instruction.md   ← инструкция/промпт (как выбирать)
    demo.mrt
    revenue.mrt
    …
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# правь system-промпт под себя
PICK_SYSTEM = """Ты выбираешь один файл дашборда (.mrt) по вопросу пользователя.

Тебе даны:
1) инструкция (из .md);
2) список доступных .mrt файлов.

Ответь ТОЛЬКО JSON без markdown:
{"mrt":"<точное имя файла из списка>","title":"<короткий заголовок>","reason":"<почему выбран>"}

Правила:
- mrt — только имя из списка (как есть, с .mrt);
- если ничего идеально не подходит — выбери ближайший и объясни в reason;
- description в ответ не пиши — сервер возьмёт секцию из instruction.md.
"""

FILTER_VALUES_SYSTEM = """Ты заполняешь фильтры дашборда по вопросу пользователя.

На вход — JSON filters как в API: name, alias, choices[{key,value,checked}], value (дефолт).
choices часто из SQL — это и есть допустимые значения.

Ответь ТОЛЬКО JSON без markdown:
{"<name>": <value>, ...}

Правила:
- ключи объекта — только filters[].name;
- если есть choices — верни РОВНО choices[].key выбранного пункта (не выдумывай);
- по смыслу вопроса выбери подходящий пункт («Брест» → key «Брестская»);
- фильтра нет в вопросе — ключ не включай (сервер оставит value/дефолт);
- без choices — свободное значение из вопроса.
"""


def dashboards_dir() -> Path:
    raw = (os.environ.get("DASHBOARDS_DIR") or "dashboards").strip() or "dashboards"
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


DASHBOARDS_DIR = dashboards_dir()


def list_mrt_files(dir_path: Path | None = None) -> list[dict[str, object]]:
    """Доступные .mrt в папке dashboards/."""
    base = dir_path or dashboards_dir()
    if not base.is_dir():
        return []
    out: list[dict[str, object]] = []
    for mrt in sorted(base.glob("*.mrt")):
        out.append(
            {
                "mrt": mrt.name,
                "mrt_path": str(mrt.resolve()),
                "id": mrt.stem,
            }
        )
    return out


def load_instruction(dir_path: Path | None = None) -> str:
    """Инструкция для LLM: instruction.md, иначе все *.md склеенные."""
    base = dir_path or dashboards_dir()
    if not base.is_dir():
        return ""
    preferred = base / "instruction.md"
    if preferred.is_file():
        return preferred.read_text(encoding="utf-8").strip()
    parts: list[str] = []
    for md in sorted(base.glob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        parts.append(md.read_text(encoding="utf-8").strip())
    return "\n\n---\n\n".join(parts).strip()


def list_dashboards(dir_path: Path | None = None) -> list[dict[str, object]]:
    """Каталог для /dashboards: mrt + есть ли instruction."""
    base = dir_path or dashboards_dir()
    instruction = load_instruction(base)
    items = list_mrt_files(base)
    return [
        {
            **item,
            "has_instruction": bool(instruction),
        }
        for item in items
    ]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _llm_config() -> dict[str, object]:
    max_tokens_raw = os.environ.get("LLM_MAX_TOKENS", "").strip()
    max_tokens: int | None
    try:
        max_tokens = int(max_tokens_raw) if max_tokens_raw else None
    except ValueError:
        max_tokens = None
    return {
        "base_url": (
            os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1").strip().rstrip("/")
        ),
        "api_key": os.environ.get("LLM_API_KEY", "").strip(),
        "model": os.environ.get("LLM_MODEL", "").strip(),
        "timeout": float(os.environ.get("LLM_TIMEOUT", "60") or "60"),
        "max_tokens": max_tokens,
        # размышление / thinking (Qwen, vLLM, …). None = не трогаем body
        "reasoning": (
            _env_bool("LLM_REASONING", False)
            if os.environ.get("LLM_REASONING", "").strip() != ""
            else None
        ),
        "reasoning_effort": (
            os.environ.get("LLM_REASONING_EFFORT", "medium").strip() or "medium"
        ),
    }


def _strip_thinking(text: str) -> str:
    """Убрать блоки рассуждения, оставить финальный ответ."""
    s = text or ""
    s = re.sub(r"<think>[\s\S]*?</think>", "", s, flags=re.I)
    s = re.sub(r"<thinking>[\s\S]*?</thinking>", "", s, flags=re.I)
    return s.strip()


def _chat(messages: list[dict[str, str]], *, temperature: float = 0.1) -> str:
    cfg = _llm_config()
    model = str(cfg["model"] or "")
    if not model:
        raise RuntimeError("Задай LLM_MODEL в .env")
    url = f"{cfg['base_url']}/chat/completions"
    body: dict[str, object] = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
    }
    if cfg["max_tokens"] is not None:
        body["max_tokens"] = int(cfg["max_tokens"])
    # вкл/выкл размышление — только если LLM_REASONING задан в .env
    if cfg["reasoning"] is not None:
        reasoning_on = bool(cfg["reasoning"])
        body["chat_template_kwargs"] = {"enable_thinking": reasoning_on}
        body["reasoning_effort"] = (
            str(cfg["reasoning_effort"]) if reasoning_on else "none"
        )
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = str(cfg["api_key"] or "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(cfg["timeout"])) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc
    parsed = json.loads(raw)
    try:
        msg = parsed["choices"][0]["message"]
        content = msg.get("content")
        if content in (None, "") and msg.get("reasoning_content"):
            # некоторые бэкенды кладут ответ только в reasoning — не используем
            content = msg.get("content") or ""
        return _strip_thinking(str(content or ""))
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"bad LLM response: {raw[:400]}") from exc


def _parse_json_object(text: str) -> dict[str, object]:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        raise RuntimeError(f"LLM не вернул JSON: {s[:300]}")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise RuntimeError("LLM JSON не объект")
    return obj


def _normalize_mrt_name(name: str) -> str:
    n = (name or "").strip().replace("\\", "/").split("/")[-1]
    if n and not n.lower().endswith(".mrt"):
        n = n + ".mrt"
    return n


def _find_mrt(
    mrt_name: str,
    *,
    dir_path: Path | None = None,
) -> dict[str, object]:
    """Найти .mrt в dashboards/ по имени (demo / demo.mrt)."""
    base = dir_path or dashboards_dir()
    mrt_list = list_mrt_files(base)
    if not mrt_list:
        raise RuntimeError(f"Нет *.mrt в {base}")
    want = _normalize_mrt_name(mrt_name)
    match = next((x for x in mrt_list if str(x["mrt"]) == want), None)
    if match is None:
        want_l = want.lower()
        match = next(
            (
                x
                for x in mrt_list
                if str(x["mrt"]).lower() == want_l
                or str(x["id"]).lower() == want_l.removesuffix(".mrt")
            ),
            None,
        )
    if match is None:
        names = [str(x["mrt"]) for x in mrt_list]
        raise RuntimeError(f"Нет mrt={want!r}. Доступны: {', '.join(names)}")
    return match


def resolve_dashboard(
    mrt_name: str,
    *,
    dir_path: Path | None = None,
) -> dict[str, object]:
    """Дашборд уже выбран: без LLM, description из instruction.md."""
    base = dir_path or dashboards_dir()
    match = _find_mrt(mrt_name, dir_path=base)
    instruction = load_instruction(base)
    description = _description_from_instruction(instruction, str(match["mrt"]))
    return {
        "id": match["id"],
        "mrt": match["mrt"],
        "mrt_path": match["mrt_path"],
        "title": str(match["id"]),
        "description": description,
        "reason": "mrt задан клиентом",
        "instruction_used": bool(instruction),
        "available_mrt": [str(x["mrt"]) for x in list_mrt_files(base)],
        "picked": False,
    }


def pick_dashboard(question: str, *, dir_path: Path | None = None) -> dict[str, object]:
    """Инструкция + список .mrt → LLM → выбранный mrt. Дальше filters/open с этим файлом."""
    base = dir_path or dashboards_dir()
    mrt_list = list_mrt_files(base)
    if not mrt_list:
        raise RuntimeError(f"Нет *.mrt в {base}")

    instruction = load_instruction(base)
    if not instruction:
        raise RuntimeError(
            f"Нет инструкции: положи {base / 'instruction.md'} (или любой *.md)"
        )

    q = (question or "").strip()
    if not q:
        raise RuntimeError("question пустой")

    names = [str(x["mrt"]) for x in mrt_list]
    user = (
        f"Инструкция:\n{instruction}\n\n"
        f"Доступные .mrt:\n{json.dumps(names, ensure_ascii=False)}\n\n"
        f"Вопрос пользователя:\n{q}"
    )
    content = _chat(
        [
            {"role": "system", "content": PICK_SYSTEM},
            {"role": "user", "content": user},
        ]
    )
    picked = _parse_json_object(content)
    want = _normalize_mrt_name(str(picked.get("mrt") or ""))
    try:
        match = _find_mrt(want, dir_path=base)
    except RuntimeError:
        match = mrt_list[0]
        picked["reason"] = (
            f"LLM вернул неизвестный mrt={want!r}; взят {match['mrt']}. "
            + str(picked.get("reason") or "")
        ).strip()

    title = str(picked.get("title") or match["id"]).strip()
    # description = вся секция instruction.md про этот .mrt (не LLM)
    description = _description_from_instruction(instruction, str(match["mrt"]))
    return {
        "id": match["id"],
        "mrt": match["mrt"],
        "mrt_path": match["mrt_path"],
        "title": title,
        "description": description,
        "reason": str(picked.get("reason") or "").strip(),
        "instruction_used": True,
        "available_mrt": names,
        "llm_raw": picked,
        "picked": True,
    }


def _description_from_instruction(instruction: str, mrt_name: str) -> str:
    """Вся секция instruction.md про этот .mrt (заголовок + тело). Иначе весь файл."""
    stem = Path(mrt_name).stem
    text = (instruction or "").strip()
    if not text:
        return ""
    # ## demo.mrt / ## demo
    pat = re.compile(
        rf"(?im)^##\s+({re.escape(mrt_name)}|{re.escape(stem)})\s*$"
    )
    m = pat.search(text)
    if not m:
        return text
    start = m.start()
    nxt = re.search(r"(?m)^##\s+", text[m.end() :])
    end = m.end() + nxt.start() if nxt else len(text)
    return text[start:end].strip()


def build_result(
    filters: list[dict],
    extracted: dict[str, object] | None = None,
) -> dict[str, object]:
    """name→value: filters[].value, поверх ответ LLM как есть."""
    by_name: dict[str, dict] = {}
    alias_map: dict[str, str] = {}
    for f in filters:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        by_name[name] = f
        alias = str(f.get("alias") or "").strip()
        if alias:
            alias_map[alias.lower()] = name
        alias_map[name.lower()] = name

    result: dict[str, object] = {
        name: f.get("value") for name, f in by_name.items()
    }
    if not extracted:
        return result

    for raw_key, val in extracted.items():
        key = str(raw_key or "").strip()
        if not key or val is None or val == "":
            continue
        name = key if key in by_name else alias_map.get(key.lower())
        if name:
            result[str(name)] = val
    return result


def extract_filter_values(
    question: str,
    filters: list[dict],
) -> dict[str, object]:
    """LLM работает с filters как есть → {name: value}."""
    q = (question or "").strip()
    if not q or not filters:
        return {}

    # отдаём LLM тот же shape, что в /filters (name/alias/choices/value)
    compact: list[dict[str, object]] = []
    for f in filters:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        item: dict[str, object] = {
            "name": f.get("name"),
            "alias": f.get("alias") or f.get("name"),
            "value": f.get("value"),
            "choices": f.get("choices") or [],
        }
        compact.append(item)

    if not compact:
        return {}

    user = (
        f"filters:\n{json.dumps(compact, ensure_ascii=False)}\n\n"
        f"question:\n{q}"
    )
    content = _chat(
        [
            {"role": "system", "content": FILTER_VALUES_SYSTEM},
            {"role": "user", "content": user},
        ]
    )
    picked = _parse_json_object(content)
    out: dict[str, object] = {}
    for k, v in picked.items():
        if isinstance(v, (dict, list)):
            continue
        out[str(k)] = v
    return out
