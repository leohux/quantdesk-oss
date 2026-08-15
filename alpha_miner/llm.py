# -*- coding: utf-8 -*-
"""Xiaomi MiMo OpenAI-compatible client (Token Plan)."""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.request
from typing import Any


def _cfg() -> dict[str, str]:
    return {
        "api_key": os.environ.get("MIMO_API_KEY", "").strip(),
        "base_url": os.environ.get(
            "MIMO_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1"
        ).rstrip("/"),
        "model": os.environ.get("MIMO_MODEL", "mimo-v2.5-pro"),
    }


def chat(
    system: str,
    user: str,
    *,
    temperature: float = 0.5,
    max_tokens: int = 2500,
) -> str:
    cfg = _cfg()
    if not cfg["api_key"]:
        raise RuntimeError("MIMO_API_KEY missing")
    url = cfg["base_url"] + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + cfg["api_key"])
    with urllib.request.urlopen(
        req, timeout=120, context=ssl.create_default_context()
    ) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = body["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("reasoning_content") or "").strip()
    return content


def parse_json(raw: str) -> Any:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    m = re.search(r"[\{\[].*[\}\]]", text, flags=re.DOTALL)
    if not m:
        raise ValueError("no JSON in model response: " + raw[:400])
    return json.loads(m.group(0))
