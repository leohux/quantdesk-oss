# -*- coding: utf-8 -*-
"""Persist research reviews to data/store/research_reviews/."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()
STORE = Path(os.environ.get("RESEARCH_REVIEW_STORE", "/app/data/store/research_reviews"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_review(strategy_id: str, review: dict[str, Any]) -> Path:
    STORE.mkdir(parents=True, exist_ok=True)
    review = {**review, "strategy_id": strategy_id, "updated_at": _now()}
    path = STORE / f"{strategy_id}.json"
    registry = STORE / "registry.jsonl"
    # Compact registry line for scanning
    compact = {
        "ts": review.get("ts") or review.get("updated_at"),
        "strategy_id": strategy_id,
        "engine_version": review.get("engine_version"),
        "research_gate_pass": review.get("research_gate_pass"),
        "status": review.get("status"),
        "research_score": review.get("research_score"),
        "scores": review.get("scores"),
        "reason_codes": review.get("reason_codes"),
        "name": review.get("name"),
        "type": review.get("type"),
    }
    line = json.dumps(compact, ensure_ascii=False) + "\n"
    with _lock:
        path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        with registry.open("a", encoding="utf-8") as f:
            f.write(line)
    return path


def load_review(strategy_id: str) -> dict[str, Any] | None:
    path = STORE / f"{strategy_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_reviewed_ids() -> list[str]:
    if not STORE.exists():
        return []
    skip = {"yield_summary.json", "index.json"}
    return [p.stem for p in STORE.glob("*.json") if p.name not in skip]
