from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from strategies.engine import TEMPLATES

STORE_DIR = Path(__file__).resolve().parents[1] / "data" / "store"
CODE_DIR = STORE_DIR / "strategy_code"
STORE_DIR.mkdir(parents=True, exist_ok=True)
CODE_DIR.mkdir(parents=True, exist_ok=True)

STRATEGIES_FILE = STORE_DIR / "strategies.json"
STRATEGIES_ARCHIVE_FILE = STORE_DIR / "strategies_mined_archive.json"
SETTINGS_FILE = STORE_DIR / "app_settings.json"

_CACHE_LOCK = threading.RLock()
_STRAT_CACHE: list[dict[str, Any]] | None = None
_STRAT_MTIME: float | None = None
_STRAT_INDEX: dict[str, dict[str, Any]] | None = None

DEFAULT_STRATEGIES = [
    {
        "id": "ma-cross",
        "name": "双均线交叉",
        "description": "快慢均线交叉，适用于流动性较好的美股。",
        "type": "ma_cross",
        "enabled": True,
        "status": "running",
        "params": {
            "fast": 20,
            "slow": 60,
            "risk_per_trade_pct": 2,
            "max_position_pct": 10,
            "symbols": ["AAPL", "MSFT", "NVDA", "QQQ"],
        },
        "metrics": {"sharpe": None, "total_return_pct": None},
    },
    {
        "id": "rsi-reversion",
        "name": "RSI 均值回归",
        "description": "RSI 超卖买入，超买卖出。",
        "type": "rsi",
        "enabled": False,
        "status": "stopped",
        "params": {
            "period": 14,
            "oversold": 30,
            "overbought": 70,
            "risk_per_trade_pct": 2,
            "max_position_pct": 10,
            "symbols": ["AAPL", "MSFT", "NVDA", "QQQ"],
        },
        "metrics": {"sharpe": None, "total_return_pct": None},
    },
    {
        "id": "momentum",
        "name": "动量突破",
        "description": "突破 N 日高点入场，跌破均线离场。",
        "type": "momentum",
        "enabled": False,
        "status": "stopped",
        "params": {
            "lookback": 20,
            "exit_ma": 50,
            "risk_per_trade_pct": 2,
            "max_position_pct": 10,
            "symbols": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"],
        },
        "metrics": {"sharpe": None, "total_return_pct": None},
    },
    {
        "id": "bollinger",
        "name": "布林带回归",
        "description": "跌破下轨买入，回到中轨卖出。",
        "type": "bollinger",
        "enabled": False,
        "status": "stopped",
        "params": {
            "window": 20,
            "num_std": 2,
            "risk_per_trade_pct": 2,
            "max_position_pct": 10,
            "symbols": ["SPY", "QQQ", "AAPL", "MSFT"],
        },
        "metrics": {"sharpe": None, "total_return_pct": None},
    },
]

DEFAULT_SETTINGS = {
    "alpaca_api_key": "",
    "alpaca_secret_key": "",
    "alpaca_mode": "paper",
    "polygon_api_key": "",
    "telegram_bot_token": "",
    "webhook_url": "",
    "postgres_url": "",
    "duckdb_path": "./data/quant.duckdb",
    "risk_per_trade_pct": 2,
    "max_position_pct": 10,
    "access_token": "",
}


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        path.write_text(json.dumps(default, separators=(",", ":")), encoding="utf-8")
        return json.loads(json.dumps(default))
    # Retry: miner may rewrite strategies.json while readers parse it
    last_err: Exception | None = None
    for i in range(8):
        try:
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                raise json.JSONDecodeError("empty", raw, 0)
            return json.loads(raw)
        except (json.JSONDecodeError, OSError) as e:
            last_err = e
            time.sleep(0.05 * (i + 1))
    if last_err:
        raise last_err
    return default


def _write_json(path: Path, data: Any) -> None:
    """Atomic replace to avoid torn reads (intraday JSON errors)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _code_path(strategy_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", strategy_id)
    return CODE_DIR / f"{safe}.py"


def ensure_strategy_code(item: dict[str, Any]) -> str:
    path = _code_path(item["id"])
    if path.exists():
        return path.read_text(encoding="utf-8")
    code = TEMPLATES.get(item.get("type", ""), TEMPLATES["ma_cross"])
    path.write_text(code, encoding="utf-8")
    return code


def _invalidate_cache() -> None:
    global _STRAT_CACHE, _STRAT_MTIME, _STRAT_INDEX
    _STRAT_CACHE = None
    _STRAT_MTIME = None
    _STRAT_INDEX = None


def _load_strategies() -> list[dict[str, Any]]:
    """Load strategies metadata with mtime cache (no per-file code scan)."""
    global _STRAT_CACHE, _STRAT_MTIME, _STRAT_INDEX
    with _CACHE_LOCK:
        mtime = STRATEGIES_FILE.stat().st_mtime if STRATEGIES_FILE.exists() else None
        if _STRAT_CACHE is not None and mtime is not None and mtime == _STRAT_MTIME:
            return _STRAT_CACHE

        items = _read_json(STRATEGIES_FILE, DEFAULT_STRATEGIES)
        known = {x["id"] for x in items}
        changed = False
        for d in DEFAULT_STRATEGIES:
            if d["id"] not in known:
                items.append(d)
                changed = True
        if changed:
            _write_json(STRATEGIES_FILE, items)
            mtime = STRATEGIES_FILE.stat().st_mtime

        _STRAT_CACHE = items
        _STRAT_MTIME = mtime
        _STRAT_INDEX = {x["id"]: x for x in items}
        return items


def _load_archive() -> list[dict[str, Any]]:
    if not STRATEGIES_ARCHIVE_FILE.exists():
        return []
    try:
        return json.loads(STRATEGIES_ARCHIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _is_mined_strategy(item: dict[str, Any]) -> bool:
    sid = str(item.get("id", ""))
    name = str(item.get("name", ""))
    return (
        sid.startswith(("alpha-", "mimo-", "hybrid-"))
        or name.startswith(("Alpha-", "MiMo-", "Hybrid-"))
    )


def list_strategies(*, manual_only: bool = False, mined_only: bool = False) -> list[dict[str, Any]]:
    items = _load_strategies()
    if manual_only:
        return [x for x in items if not _is_mined_strategy(x)]
    if mined_only:
        # Hot mined + cold archive (archive only when explicitly browsing mined)
        mined = [x for x in items if _is_mined_strategy(x)]
        arch = _load_archive()
        seen = {x["id"] for x in mined}
        for x in arch:
            if x.get("id") not in seen:
                mined.append(x)
        return mined
    return items


def save_strategies(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with _CACHE_LOCK:
        _write_json(STRATEGIES_FILE, items)
        _invalidate_cache()
    return items


def get_strategy(strategy_id: str) -> dict[str, Any]:
    _load_strategies()
    with _CACHE_LOCK:
        item = (_STRAT_INDEX or {}).get(strategy_id)
    if item is None:
        # Fall back to cold archive (mined strategies moved off hot path)
        for x in _load_archive():
            if x.get("id") == strategy_id:
                item = x
                break
    if item is None:
        raise KeyError(strategy_id)
    out = dict(item)
    out["code"] = ensure_strategy_code(item)
    return out


def update_strategy(strategy_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    items = list(_load_strategies())
    for i, item in enumerate(items):
        if item["id"] == strategy_id:
            code = patch.pop("code", None)
            merged = {**item, **patch}
            if "params" in patch and isinstance(patch["params"], dict):
                merged["params"] = {**item.get("params", {}), **patch["params"]}
            if "enabled" in patch:
                merged["status"] = "running" if patch["enabled"] else "stopped"
            if code is not None:
                from strategies.engine import validate_strategy_code

                validate_strategy_code(code)
                _code_path(strategy_id).write_text(code, encoding="utf-8")
            items[i] = merged
            save_strategies(items)
            out = dict(merged)
            out["code"] = ensure_strategy_code(merged)
            return out
    # Allow re-enabling / patching an archived strategy (promotes back to hot)
    for item in _load_archive():
        if item.get("id") == strategy_id:
            code = patch.pop("code", None)
            merged = {**item, **patch}
            if "params" in patch and isinstance(patch["params"], dict):
                merged["params"] = {**item.get("params", {}), **patch["params"]}
            if "enabled" in patch:
                merged["status"] = "running" if patch["enabled"] else "stopped"
            if code is not None:
                from strategies.engine import validate_strategy_code

                validate_strategy_code(code)
                _code_path(strategy_id).write_text(code, encoding="utf-8")
            items = list(_load_strategies())
            items.append(merged)
            save_strategies(items)
            # Remove from archive
            arch = [x for x in _load_archive() if x.get("id") != strategy_id]
            _write_json(STRATEGIES_ARCHIVE_FILE, arch)
            out = dict(merged)
            out["code"] = ensure_strategy_code(merged)
            return out
    raise KeyError(strategy_id)


def create_strategy(
    name: str,
    strategy_type: str = "ma_cross",
    description: str = "",
    params: dict[str, Any] | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    sid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "strategy"
    sid = f"{sid}-{uuid.uuid4().hex[:6]}"
    item = {
        "id": sid,
        "name": name,
        "description": description or f"自定义 {strategy_type} 策略",
        "type": strategy_type,
        "enabled": False,
        "status": "stopped",
        "params": params
        or {
            "fast": 20,
            "slow": 60,
            "symbols": ["AAPL", "MSFT", "NVDA", "QQQ"],
            "risk_per_trade_pct": 2,
            "max_position_pct": 10,
        },
        "metrics": {"sharpe": None, "total_return_pct": None},
    }
    src = code or TEMPLATES.get(strategy_type, TEMPLATES["ma_cross"])
    from strategies.engine import validate_strategy_code

    validate_strategy_code(src)
    _code_path(sid).write_text(src, encoding="utf-8")
    items = list(_load_strategies())
    items.append(item)
    save_strategies(items)
    out = dict(item)
    out["code"] = src
    return out


def delete_strategy(strategy_id: str) -> None:
    items = [x for x in _load_strategies() if x["id"] != strategy_id]
    save_strategies(items)
    if STRATEGIES_ARCHIVE_FILE.exists():
        arch = [x for x in _load_archive() if x.get("id") != strategy_id]
        _write_json(STRATEGIES_ARCHIVE_FILE, arch)
    path = _code_path(strategy_id)
    if path.exists():
        path.unlink()


def archive_disabled_mined(*, keep_hot_max: int = 400) -> dict[str, int]:
    """Move disabled mined strategies from hot strategies.json into cold archive.

    Always keeps enabled strategies + non-mined manuals on the hot path.
    Caps remaining hot mined (disabled) to keep_hot_max newest by updated/created.
    """
    items = list(_load_strategies())
    keep: list[dict[str, Any]] = []
    to_archive: list[dict[str, Any]] = []

    enabled_or_manual: list[dict[str, Any]] = []
    disabled_mined: list[dict[str, Any]] = []
    for x in items:
        if x.get("enabled") or not _is_mined_strategy(x):
            enabled_or_manual.append(x)
        else:
            disabled_mined.append(x)

    def _ts(item: dict[str, Any]) -> str:
        return str(
            item.get("updated_at")
            or item.get("created_at")
            or item.get("metrics", {}).get("created_at")
            or ""
        )

    disabled_mined.sort(key=_ts, reverse=True)
    keep_disabled = disabled_mined[: max(0, keep_hot_max)]
    drop_disabled = disabled_mined[max(0, keep_hot_max) :]
    keep = enabled_or_manual + keep_disabled
    to_archive = drop_disabled

    if to_archive:
        arch = _load_archive()
        by_id = {x.get("id"): x for x in arch if x.get("id")}
        for x in to_archive:
            by_id[x["id"]] = x
        _write_json(STRATEGIES_ARCHIVE_FILE, list(by_id.values()))
        save_strategies(keep)

    return {
        "hot_before": len(items),
        "hot_after": len(keep),
        "archived": len(to_archive),
        "archive_total": len(_load_archive()),
    }


def get_app_settings() -> dict[str, Any]:
    data = _read_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    masked = dict(data)
    for key in ("alpaca_secret_key", "telegram_bot_token", "polygon_api_key", "access_token"):
        val = masked.get(key) or ""
        if val:
            masked[key] = ("*" * max(0, len(str(val)) - 4)) + str(val)[-4:]
            masked[f"{key}_set"] = True
        else:
            masked[f"{key}_set"] = False
    return masked


def save_app_settings(patch: dict[str, Any]) -> dict[str, Any]:
    current = _read_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    for key, value in patch.items():
        if key.endswith("_set"):
            continue
        if isinstance(value, str) and value.startswith("*"):
            continue
        if value is None:
            continue
        current[key] = value
    _write_json(SETTINGS_FILE, current)
    return get_app_settings()


def get_raw_settings() -> dict[str, Any]:
    return _read_json(SETTINGS_FILE, DEFAULT_SETTINGS)
