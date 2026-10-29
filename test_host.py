"""Unit checks for catalog defaults and prompt wiring (no LLM/MCP)."""

from __future__ import annotations

import json
from pathlib import Path

from host import default_parameters, system_prompt, user_message


ROOT = Path(__file__).resolve().parent


def test_default_parameters_from_sample_catalog() -> None:
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    defaults = default_parameters(catalog)
    assert defaults == [
        {
            "kind": "source_parameter",
            "source": "products",
            "name": "Category",
            "sql_placeholder": "@Category",
            "value": "8",
        }
    ]


def test_system_prompt_mentions_pipeline_and_defaults() -> None:
    catalog = {"report": {"name": "Demo"}, "sources": []}
    prompt = system_prompt(catalog)
    assert "Пайплайн ответа" in prompt
    assert "Дефолтные параметры" in prompt
    assert "query" in prompt


def test_user_overrides_beat_defaults() -> None:
    text = user_message("сколько", {"Category": 3})
    assert "Category" in text
    assert "3" in text
    assert "важнее дефолтов" in text
