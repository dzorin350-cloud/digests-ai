"""Standalone mode: any OpenAI-compatible chat endpoint does the agent's job.

LLM_BASE_URL, LLM_API_KEY, LLM_MODEL (OpenAI, OpenRouter, Kimi/Moonshot, DeepSeek, Qwen, local vLLM/Ollama...).
The prompts are the same files the agent reads (digest prompt daily|review), so both modes behave alike.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from digestkit import paths


class LLMError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL"))


def chat(system: str, user: str, json_mode: bool, max_tokens: int = 6000) -> str:
    base, model = os.environ.get("LLM_BASE_URL", ""), os.environ.get("LLM_MODEL", "")
    if not base or not model:
        raise LLMError("standalone mode needs LLM_BASE_URL and LLM_MODEL in .env (or use agent mode)")
    body: dict[str, Any] = {"model": model, "temperature": 0.2, "max_tokens": max_tokens,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode and os.environ.get("LLM_JSON_MODE", "1") != "0":
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}"}
    with httpx.Client(timeout=float(os.environ.get("LLM_TIMEOUT", "180"))) as c:
        r = c.post(base.rstrip("/") + "/chat/completions", headers=headers, json=body)
        if r.status_code == 400 and "response_format" in body:  # endpoint without JSON mode
            body.pop("response_format")
            r = c.post(base.rstrip("/") + "/chat/completions", headers=headers, json=body)
        if r.status_code >= 400:
            raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
        content = r.json()["choices"][0]["message"].get("content") or ""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()


def parse_json(text: str) -> Any:
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError(f"no JSON in answer: {text[:200]}") from None
        return json.loads(m.group(0))


def prompt(name: str) -> str:
    return paths.prompt_path(name).read_text(encoding="utf-8")


def daily_decisions(pack: dict[str, Any]) -> dict[str, Any]:
    """One call per block (smaller context, works with small models), merged into one decisions object."""
    system = prompt("daily")
    items: list[dict[str, Any]] = []
    notes: dict[str, str] = {}
    leads = []
    for block in pack["blocks"]:
        if not block["candidates"] and not block["reminders"]:
            continue
        sub = {k: v for k, v in pack.items() if k != "blocks"}
        sub["blocks"] = [block]
        try:
            answer = parse_json(chat(system, "Pack:\n" + json.dumps(sub, ensure_ascii=False), json_mode=True))
        except (LLMError, json.JSONDecodeError):  # one retry: small models sometimes break JSON
            answer = parse_json(chat(system + "\n\nReturn ONLY the JSON object.", "Pack:\n" + json.dumps(sub, ensure_ascii=False),
                                     json_mode=True))
        if not isinstance(answer, dict):
            raise LLMError("the model did not return a JSON object")
        items += answer.get("items", [])
        notes.update(answer.get("block_notes", {}) or {})
        if answer.get("lead"):
            leads.append(str(answer["lead"]).strip())
    lead = ""
    if len(leads) == 1:
        lead = leads[0]
    elif leads:
        lead = chat("Сожми несколько вводных абзацев дайджеста в один абзац из 1–2 предложений. Только текст."
                    if pack.get("language") == "ru" else "Merge these digest intros into one 1-2 sentence paragraph. Text only.",
                    "\n\n".join(leads), json_mode=False, max_tokens=400)
    return {"pack_id": pack["pack_id"], "lead": lead, "items": items, "block_notes": notes}


def review_markdown(pack: dict[str, Any]) -> str:
    text = chat(prompt("review"), "Pack:\n" + json.dumps(pack, ensure_ascii=False), json_mode=False, max_tokens=8000)
    return re.sub(r"^\s*```(?:markdown|md)?\s*|\s*```\s*$", "", text)
