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

FILTER_VALUES_SYSTEM = """Ты извлекаешь значения фильтров дашборда из вопроса пользователя.

Тебе дан список фильтров: name, alias, choices (допустимые key/value), value (дефолт).

Ответь ТОЛЬКО JSON-объектом без markdown:
{"<name>":"<value>", ...}

Правила:
- ключи — только name из списка фильтров;
- если в вопросе явно указано значение — поставь его (для choices бери key или value из списка);
- если в вопросе фильтра нет — НЕ включай этот ключ (дефолт подставит сервер);
- не выдумывай имена фильтров и значения вне choices (если choices не пустой).
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


def _llm_config() -> dict[str, object]:
    return {
        "base_url": (
            os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1").strip().rstrip("/")
        ),
        "api_key": os.environ.get("LLM_API_KEY", "").strip(),
        "model": os.environ.get("LLM_MODEL", "").strip(),
        "timeout": float(os.environ.get("LLM_TIMEOUT", "60") or "60"),
    }


def _chat(messages: list[dict[str, str]], *, temperature: float = 0.1) -> str:
    cfg = _llm_config()
    model = str(cfg["model"] or "")
    if not model:
        raise RuntimeError("Задай LLM_MODEL в .env")
    url = f"{cfg['base_url']}/chat/completions"
    body = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
    }
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
        return str(parsed["choices"][0]["message"]["content"])
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
    match = next((x for x in mrt_list if str(x["mrt"]) == want), None)
    if match is None:
        # иногда модель вернёт stem без расширения / другой регистр
        want_l = want.lower()
        match = next(
            (x for x in mrt_list if str(x["mrt"]).lower() == want_l
             or str(x["id"]).lower() == want_l.removesuffix(".mrt")),
            None,
        )
    if match is None:
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


def _default_filter_value(filt: dict) -> object:
    """Дефолт из filters: value, иначе checked choice, иначе первый choice."""
    if "value" in filt and filt["value"] not in (None, ""):
        return filt["value"]
    choices = filt.get("choices") or []
    if isinstance(choices, list):
        for ch in choices:
            if isinstance(ch, dict) and ch.get("checked"):
                return ch.get("key") if ch.get("key") not in (None, "") else ch.get("value")
        if choices and isinstance(choices[0], dict):
            ch0 = choices[0]
            if ch0.get("key") not in (None, ""):
                return ch0["key"]
            if ch0.get("value") not in (None, ""):
                return ch0["value"]
    return filt.get("value")


def build_result(
    filters: list[dict],
    extracted: dict[str, object] | None = None,
) -> dict[str, object]:
    """name→value: дефолты из filters, поверх — вычлененные из вопроса."""
    by_name: dict[str, dict] = {}
    for f in filters:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        if name:
            by_name[name] = f

    result: dict[str, object] = {}
    for name, f in by_name.items():
        result[name] = _default_filter_value(f)

    if not extracted:
        return result

    # alias → name
    alias_map: dict[str, str] = {}
    for name, f in by_name.items():
        alias = str(f.get("alias") or "").strip()
        if alias:
            alias_map[alias.lower()] = name
        alias_map[name.lower()] = name

    for raw_key, val in extracted.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        name = by_name[key]["name"] if key in by_name else alias_map.get(key.lower())
        if not name:
            continue
        if val is None or val == "":
            continue
        result[str(name)] = val
    return result


def extract_filter_values(
    question: str,
    filters: list[dict],
) -> dict[str, object]:
    """LLM: из вопроса → {name: value} только по известным фильтрам."""
    q = (question or "").strip()
    if not q or not filters:
        return {}

    compact: list[dict[str, object]] = []
    for f in filters:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        item: dict[str, object] = {
            "name": f.get("name"),
            "alias": f.get("alias") or f.get("name"),
            "value": f.get("value"),
        }
        choices = f.get("choices") or []
        if isinstance(choices, list) and choices:
            # не раздуваем промпт: до 40 пунктов
            slim = []
            for ch in choices[:40]:
                if not isinstance(ch, dict):
                    continue
                slim.append(
                    {
                        k: ch[k]
                        for k in ("key", "value", "checked")
                        if k in ch and ch[k] not in (None, "")
                    }
                )
            item["choices"] = slim
            if len(choices) > 40:
                item["choices_truncated"] = True
        compact.append(item)

    if not compact:
        return {}

    user = (
        f"Фильтры дашборда:\n{json.dumps(compact, ensure_ascii=False)}\n\n"
        f"Вопрос пользователя:\n{q}"
    )
    content = _chat(
        [
            {"role": "system", "content": FILTER_VALUES_SYSTEM},
            {"role": "user", "content": user},
        ]
    )
    picked = _parse_json_object(content)
    # только скаляры / простые значения
    out: dict[str, object] = {}
    for k, v in picked.items():
        if isinstance(v, (dict, list)):
            continue
        out[str(k)] = v
    return out
