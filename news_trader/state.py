# -*- coding: utf-8 -*-
"""Persistent state for news trader."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = {
            "seen_news_ids": [],
            "open_trades": {},  # symbol -> meta
            "pending_buys": {},  # symbol -> scored intent (await RTH)
            "history": [],
        }
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.data.setdefault("seen_news_ids", [])
        self.data.setdefault("open_trades", {})
        self.data.setdefault("pending_buys", {})
        self.data.setdefault("history", [])

    def save(self) -> None:
        # cap lists
        self.data["seen_news_ids"] = list(self.data.get("seen_news_ids") or [])[-2000:]
        self.data["history"] = list(self.data.get("history") or [])[-500:]
        pending = dict(self.data.get("pending_buys") or {})
        # Keep queue small; oldest keys drop if somehow unbounded
        if len(pending) > 40:
            keys = list(pending.keys())[:-40]
            for k in keys:
                pending.pop(k, None)
        self.data["pending_buys"] = pending
        # Atomic replace: a crash mid-write used to leave a half JSON that
        # load() then silently ignored, wiping open_trades.
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    def seen(self, nid: str) -> bool:
        return nid in set(self.data.get("seen_news_ids") or [])

    def mark_seen(self, nid: str) -> None:
        ids = self.data.setdefault("seen_news_ids", [])
        if nid not in ids:
            ids.append(nid)
