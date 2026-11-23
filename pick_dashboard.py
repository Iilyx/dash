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
{"mrt":"<точное имя файла из списка>","title":"<короткий заголовок>","description":"<1-2 предложения что за дашборд>","reason":"<почему выбран>"}

Правила:
- mrt — только имя из списка (как есть, с .mrt);
- если ничего идеально не подходит — выбери ближайший и объясни в reason;
- description — для человека, понятным языком.
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
    description = str(picked.get("description") or "").strip()
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
