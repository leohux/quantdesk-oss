# -*- coding: utf-8 -*-
"""Cross-source event dedupe for news_trader.

Same corporate event arrives as SEC 8-K, Benzinga article, later wires —
ids differ. Match on overlapping tech ticker + time window + catalyst
(or token similarity) so SCORE_LIMIT / entries are not burned thrice.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .risk import news_catalyst_type
from .universe import TECH_SET, canonical_symbol

WINDOW_SEC = float(os.environ.get("NEWS_TRADER_XDUPE_WINDOW_SEC", "14400"))  # 4h
JACCARD_MIN = float(os.environ.get("NEWS_TRADER_XDUPE_JACCARD", "0.35"))
# Only treat these as event-aligned across sources (avoid "other"/"other" collisions)
ALIGN_CATS = frozenset(
    {"earnings", "contract", "management", "cyber", "analyst", "product"}
)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]{3,}", (text or "").lower())
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "sec",
        "item",
        "items",
        "form",
        "filed",
        "company",
        "inc",
        "ltd",
        "news",
        "says",
        "after",
    }
    return {w for w in words if w not in stop and not w.isdigit()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _syms(item: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        canonical_symbol(s)
        for s in (item.get("symbols") or [])
        if canonical_symbol(s) in TECH_SET
    )


def _cat(item: dict[str, Any]) -> str:
    pre = item.get("catalyst_type")
    if pre:
        return str(pre)
    return news_catalyst_type(item)


@dataclass
class _Event:
    nid: str
    feed: str
    ts: datetime
    syms: frozenset[str]
    cat: str
    tokens: set[str]


class CrossSourceDeduper:
    def __init__(self, *, window_sec: float = WINDOW_SEC) -> None:
        self.window = window_sec
        self._lock = threading.Lock()
        self._events: list[_Event] = []

    def _prune(self, now: datetime) -> None:
        cut = now.timestamp() - self.window * 2
        self._events = [e for e in self._events if e.ts.timestamp() >= cut]

    def is_duplicate(self, item: dict[str, Any]) -> tuple[bool, str | None]:
        """Return (is_dupe, matched_id)."""
        syms = _syms(item)
        if not syms:
            return False, None
        ts = _parse_ts(item.get("created_at")) or datetime.now(timezone.utc)
        cat = _cat(item)
        toks = _tokens(f"{item.get('headline') or ''} {item.get('summary') or ''}")
        nid = str(item.get("id") or "")
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune(now)
            for e in self._events:
                if e.nid == nid:
                    return True, e.nid
                if not (syms & e.syms):
                    continue
                dt = abs((ts - e.ts).total_seconds())
                if dt > self.window:
                    continue
                # Structured catalyst alignment (SEC item codes help here)
                if cat in ALIGN_CATS and e.cat in ALIGN_CATS and cat == e.cat:
                    return True, e.nid
                if _jaccard(toks, e.tokens) >= JACCARD_MIN:
                    return True, e.nid
        return False, None

    def remember(self, item: dict[str, Any]) -> None:
        syms = _syms(item)
        if not syms:
            return
        ts = _parse_ts(item.get("created_at")) or datetime.now(timezone.utc)
        ev = _Event(
            nid=str(item.get("id") or ""),
            feed=str(item.get("feed") or item.get("source") or ""),
            ts=ts,
            syms=syms,
            cat=_cat(item),
            tokens=_tokens(f"{item.get('headline') or ''} {item.get('summary') or ''}"),
        )
        with self._lock:
            self._events.append(ev)
            self._prune(datetime.now(timezone.utc))


XDUPE = CrossSourceDeduper()
