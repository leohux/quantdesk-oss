# -*- coding: utf-8 -*-
"""Thread-safe news inbox: WS (or REST bootstrap) → local queue → batch score.

Hard gates (before SCORE_LIMIT ever sees an item):
  1) id dedupe (buffer + process-lifetime) — REST/WS overlap safe
  2) TECH_SET intersection — filter=* never dilutes score slots

Successful push wakes the main loop so scoring is event-driven, not only
INTERVAL-polled (overnight/premarket catalysts used to wait up to 10–30 min).
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable

from .universe import TECH_SET, canonical_symbol

_INBOX_MAX = int(os.environ.get("NEWS_TRADER_INBOX_MAX", "500"))
# Cap lifetime id memory so long-running process does not grow unbounded
_KNOWN_MAX = int(os.environ.get("NEWS_TRADER_KNOWN_IDS_MAX", "20000"))

# Main loop waits on this; WS/SEC set it when a universe item lands.
_WAKE = threading.Event()


def wake_scoring() -> None:
    _WAKE.set()


def wait_scoring_wake(timeout: float) -> bool:
    """Block up to timeout; True if new news woke us early."""
    fired = _WAKE.wait(timeout=max(0.05, float(timeout)))
    if fired:
        _WAKE.clear()
    return fired


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def in_tech_universe(symbols: list[str] | None) -> bool:
    if not symbols:
        return False
    return any(canonical_symbol(s) in TECH_SET for s in symbols)


_EXTRA_KEEP = (
    "sec_form",
    "sec_accession",
    "sec_cik",
    "sec_items",
    "catalyst_type",
)


def normalize_news(raw: dict[str, Any], *, feed: str) -> dict[str, Any] | None:
    """Map Alpaca/SEC payload into the loop's item shape."""
    nid = str(raw.get("id") or "")
    headline = (raw.get("headline") or "").strip()
    if not nid or not headline:
        return None
    symbols = [canonical_symbol(s) for s in (raw.get("symbols") or [])]
    symbols = list(dict.fromkeys(s for s in symbols if s))  # dedupe, keep order
    if not in_tech_universe(symbols):
        return None
    received_at = raw.get("received_at") or _now_iso()
    out: dict[str, Any] = {
        "id": nid,
        "headline": headline,
        "summary": (raw.get("summary") or "").strip(),
        "symbols": symbols,
        "created_at": raw.get("created_at"),
        "url": raw.get("url"),
        "source": raw.get("source"),
        "received_at": received_at,
        "feed": feed or raw.get("feed") or "unknown",
    }
    for k in _EXTRA_KEEP:
        if raw.get(k) is not None:
            out[k] = raw[k]
    return out


class NewsInbox:
    """Universe-filtered, id-deduped buffer of news awaiting scoring."""

    def __init__(self, *, max_size: int = 500) -> None:
        self._max = max(50, int(max_size))
        self._lock = threading.Lock()
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Survives eviction so REST fallback + WS replay cannot re-buffer
        self._known_ids: OrderedDict[str, None] = OrderedDict()
        self._pushed = 0
        self._dropped = 0
        self._dupes = 0
        self._rejected_universe = 0

    def push(self, item: dict[str, Any]) -> bool:
        nid = str(item.get("id") or "")
        if not nid:
            return False
        if not in_tech_universe(item.get("symbols") or []):
            with self._lock:
                self._rejected_universe += 1
            return False
        with self._lock:
            if nid in self._known_ids or nid in self._items:
                self._dupes += 1
                return False
            self._items[nid] = item
            self._known_ids[nid] = None
            self._pushed += 1
            while len(self._known_ids) > _KNOWN_MAX:
                self._known_ids.popitem(last=False)
            while len(self._items) > self._max:
                self._items.popitem(last=False)
                self._dropped += 1
            wake_scoring()
            return True

    def push_raw(self, raw: dict[str, Any], *, feed: str) -> bool:
        item = normalize_news(raw, feed=feed)
        if item is None:
            # normalize already drops non-tech / malformed
            with self._lock:
                # Only count universe rejects when payload looked like news
                if raw.get("id") and raw.get("headline"):
                    syms = raw.get("symbols") or []
                    if syms and not in_tech_universe(syms):
                        self._rejected_universe += 1
            return False
        return self.push(item)

    def pending(
        self,
        is_seen: Callable[[str], bool],
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Unseen tech-universe items only (universe already enforced on push)."""
        with self._lock:
            out = [
                dict(v)
                for k, v in self._items.items()
                if v.get("headline")
                and in_tech_universe(v.get("symbols") or [])
                and not is_seen(str(k))
            ]
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        if limit is not None:
            return out[:limit]
        return out

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "buffered": len(self._items),
                "pushed": self._pushed,
                "dropped": self._dropped,
                "dupes": self._dupes,
                "rejected_universe": self._rejected_universe,
                "known_ids": len(self._known_ids),
            }


# Process-wide inbox used by WS thread + scoring loop
INBOX = NewsInbox(max_size=_INBOX_MAX)
