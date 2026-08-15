# -*- coding: utf-8 -*-
"""Path B: Cursor-agent proposals from inbox (no external LLM).

Drop candidates into:
  /app/data/store/alpha_miner/cursor_inbox.jsonl
One JSON object per line. Miner pops them into the backtest pool.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

INBOX = Path(
    __import__("os").environ.get(
        "ALPHA_MINER_CURSOR_INBOX",
        "/app/data/store/alpha_miner/cursor_inbox.jsonl",
    )
)
_LOCK = threading.Lock()


def _validate_item(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    code = (item.get("code") or "").strip()
    if "generate_signals" not in code:
        return None
    if "pct_change(-" in code or "shift(-" in code:
        return None
    if "import pandas" not in code:
        code = "import pandas as pd\n\n" + code
    sid = uuid.uuid4().hex[:6]
    name = str(item.get("name") or f"Cursor-Hybrid-{sid}")[:80]
    if not name.startswith("Cursor"):
        name = f"Cursor-Hybrid-{name}"
    symbols = item.get("symbols") or ["AAPL"]
    if isinstance(symbols, str):
        symbols = [symbols]
    symbols = [str(s).upper() for s in symbols][:1] or ["AAPL"]
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    return {
        "source": "B_cursor",
        "name": name,
        "type": str(item.get("type") or "hybrid")[:40],
        "description": str(item.get("description") or "Cursor hybrid")[:400],
        "params": params,
        "symbols": symbols,
        "code": code,
    }


def propose_with_cursor(n: int = 6, recent_names: list[str] | None = None) -> list[dict]:
    """Pop up to n candidates from the Cursor inbox file."""
    del recent_names  # reserved for future dedupe
    if n <= 0:
        return []
    with _LOCK:
        if not INBOX.exists():
            return []
        try:
            lines = INBOX.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        kept: list[str] = []
        out: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if len(out) >= n:
                kept.append(line)
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            cand = _validate_item(item)
            if cand:
                out.append(cand)
            else:
                # invalid — drop
                pass
        INBOX.parent.mkdir(parents=True, exist_ok=True)
        INBOX.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return out


# Back-compat alias so loop.py imports keep working if not fully patched
def propose_with_mimo(n: int = 2, recent_names: list[str] | None = None) -> list[dict]:
    return propose_with_cursor(n=n, recent_names=recent_names)
