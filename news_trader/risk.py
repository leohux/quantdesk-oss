# -*- coding: utf-8 -*-
"""ATR-aware exits and news-lag price helpers for news_trader."""
from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


ATR_MULT = float(os.environ.get("NEWS_TRADER_ATR_MULT", "1.5"))
ATR_LOOKBACK = int(os.environ.get("NEWS_TRADER_ATR_LOOKBACK", "20"))
# Band around ATR_MULT×ATR: AI SL/TP are clamped into these (hard), not replaced.
ATR_SL_BAND_LO = float(os.environ.get("NEWS_TRADER_ATR_SL_BAND_LO", "0.80"))
ATR_SL_BAND_HI = float(os.environ.get("NEWS_TRADER_ATR_SL_BAND_HI", "1.20"))
SL_FLOOR_PCT = float(os.environ.get("NEWS_TRADER_SL_FLOOR_PCT", "2.5"))
SL_CAP_PCT = float(os.environ.get("NEWS_TRADER_SL_CAP_PCT", "7.0"))
TP_RR = float(os.environ.get("NEWS_TRADER_TP_RR", "1.6"))  # default if AI omits TP
TP_RR_MIN = float(os.environ.get("NEWS_TRADER_TP_RR_MIN", "1.20"))
TP_RR_MAX = float(os.environ.get("NEWS_TRADER_TP_RR_MAX", "2.00"))
TP_FLOOR_PCT = float(os.environ.get("NEWS_TRADER_TP_FLOOR_PCT", "4.0"))
TP_CAP_PCT = float(os.environ.get("NEWS_TRADER_TP_CAP_PCT", "12.0"))
# Skip buy if price already ran this far vs news/prev-close reference (long-only).
MAX_ENTRY_VS_NEWS_PCT = float(os.environ.get("NEWS_TRADER_MAX_ENTRY_VS_NEWS_PCT", "2.0"))
# Skip / drop pending if news is older than this at decision time.
MAX_NEWS_AGE_SEC = float(os.environ.get("NEWS_TRADER_MAX_NEWS_AGE_SEC", "14400"))


def _keys() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
    return key, secret


def _get_json(url: str) -> dict[str, Any]:
    key, secret = _keys()
    req = urllib.request.Request(url)
    req.add_header("APCA-API-KEY-ID", key)
    req.add_header("APCA-API-SECRET-KEY", secret)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def atr_pct(symbol: str, *, lookback: int = ATR_LOOKBACK) -> float | None:
    """20-day ATR as a percent of latest close. None if bars unavailable."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback + 25)
    qs = urllib.parse.urlencode(
        {
            "timeframe": "1Day",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": lookback + 5,
            "adjustment": "split",
            "feed": "iex",
        }
    )
    url = f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/bars?{qs}"
    try:
        body = _get_json(url)
    except Exception:
        return None
    bars = body.get("bars") or []
    if len(bars) < max(5, lookback // 2):
        return None
    trs: list[float] = []
    prev_close: float | None = None
    for b in bars:
        high = float(b["h"])
        low = float(b["l"])
        close = float(b["c"])
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    window = trs[-lookback:] if len(trs) >= lookback else trs
    if not window or not prev_close or prev_close <= 0:
        return None
    atr = sum(window) / len(window)
    return (atr / prev_close) * 100.0


def _clamp(x: float, lo: float, hi: float) -> float:
    if lo > hi:
        lo, hi = hi, lo
    return max(lo, min(hi, x))


def resolve_exits(
    symbol: str,
    *,
    ai_sl_pct: float | None = None,
    ai_tp_pct: float | None = None,
) -> dict[str, Any]:
    """Clamp AI SL/TP into an ATR-derived band (hard constraint).

    Path:
      atr_ref = ATR_MULT × ATR%
      SL = clamp(AI_SL or atr_ref, atr_ref×[0.8,1.2] ∩ [SL_FLOOR, SL_CAP])
      intended_rr = clamp(AI_TP/AI_SL or TP_RR, [RR_MIN, RR_MAX])
      TP = clamp(SL × intended_rr,
                 [max(atr×RR_MIN×0.8, SL×RR_MIN),
                  min(atr×RR_MAX×1.2, SL×RR_MAX)] ∩ [TP_FLOOR, TP_CAP])

    Critical: when ATR widens SL, TP scales via intended_rr — not via the
    AI's absolute TP%. Absolute AI TP used to sit below the RR floor after
    SL widen, so every trade pinned RR=1.2 and erased AI's payoff judgment.
    """
    atr = atr_pct(symbol)
    if atr is not None and math.isfinite(atr) and atr > 0:
        atr_ref = ATR_MULT * atr
        sl_lo = _clamp(atr_ref * ATR_SL_BAND_LO, SL_FLOOR_PCT, SL_CAP_PCT)
        sl_hi = _clamp(atr_ref * ATR_SL_BAND_HI, SL_FLOOR_PCT, SL_CAP_PCT)
        if sl_lo > sl_hi:
            sl_lo, sl_hi = sl_hi, sl_lo
        # Absolute TP rails scale with ATR×RR band (not a fixed 4% floor
        # that, after SL widen, collapses every trade onto RR_MIN).
        tp_abs_lo = _clamp(
            atr_ref * TP_RR_MIN * ATR_SL_BAND_LO, TP_FLOOR_PCT, TP_CAP_PCT
        )
        tp_abs_hi = _clamp(
            atr_ref * TP_RR_MAX * ATR_SL_BAND_HI, TP_FLOOR_PCT, TP_CAP_PCT
        )
        if tp_abs_lo > tp_abs_hi:
            tp_abs_lo, tp_abs_hi = tp_abs_hi, tp_abs_lo
        source = "atr_clamp"
    else:
        atr_ref = float(ai_sl_pct) if ai_sl_pct else 3.5
        atr_ref = _clamp(atr_ref, SL_FLOOR_PCT, SL_CAP_PCT)
        sl_lo, sl_hi = SL_FLOOR_PCT, SL_CAP_PCT
        tp_abs_lo, tp_abs_hi = TP_FLOOR_PCT, TP_CAP_PCT
        source = "global_clamp"
        atr = None

    raw_sl = float(ai_sl_pct) if ai_sl_pct else atr_ref
    sl = _clamp(raw_sl, sl_lo, sl_hi)

    # Preserve AI's RR; do not reuse absolute AI TP against a widened SL.
    if ai_sl_pct and float(ai_sl_pct) > 0 and ai_tp_pct:
        intended_rr = float(ai_tp_pct) / float(ai_sl_pct)
    else:
        intended_rr = TP_RR
    intended_rr = _clamp(intended_rr, TP_RR_MIN, TP_RR_MAX)

    tp_rr_lo = sl * TP_RR_MIN
    tp_rr_hi = sl * TP_RR_MAX
    tp_lo = max(tp_abs_lo, tp_rr_lo)
    tp_hi = min(tp_abs_hi, tp_rr_hi)
    if tp_lo > tp_hi:
        # Prefer RR geometry over absolute band when they conflict.
        tp_lo, tp_hi = tp_rr_lo, max(tp_rr_lo, tp_rr_hi)

    raw_tp = sl * intended_rr
    tp = _clamp(raw_tp, tp_lo, tp_hi)
    if tp < sl + 0.5:
        tp = min(TP_CAP_PCT, max(tp_lo, sl + 0.5))

    clamped_sl = abs(raw_sl - sl) > 1e-9
    # Flag TP clamp vs the RR-scaled raw (not the stale absolute AI TP).
    clamped_tp = abs(raw_tp - tp) > 1e-9
    return {
        "stop_loss_pct": round(sl, 2),
        "take_profit_pct": round(tp, 2),
        "atr_pct": round(atr, 3) if atr is not None else None,
        "atr_ref_pct": round(atr_ref, 3),
        "atr_mult": ATR_MULT,
        "sl_lo_pct": round(sl_lo, 2),
        "sl_hi_pct": round(sl_hi, 2),
        "tp_lo_pct": round(tp_lo, 2),
        "tp_hi_pct": round(tp_hi, 2),
        "tp_abs_lo_pct": round(tp_abs_lo, 2),
        "tp_abs_hi_pct": round(tp_abs_hi, 2),
        "intended_rr": round(intended_rr, 3),
        "exit_source": source,
        "clamped_sl": clamped_sl,
        "clamped_tp": clamped_tp,
        "ai_stop_loss_pct": ai_sl_pct,
        "ai_take_profit_pct": ai_tp_pct,
    }


def price_near_news(symbol: str, news_created_at: Any) -> dict[str, Any]:
    """Closest 1-min bar open around the news timestamp (entry-lag audit)."""
    t0 = _parse_ts(news_created_at)
    if t0 is None:
        return {"price_at_news": None, "lag_bar_ts": None}
    start = t0 - timedelta(minutes=2)
    end = t0 + timedelta(minutes=20)
    qs = urllib.parse.urlencode(
        {
            "timeframe": "1Min",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 30,
            "adjustment": "split",
            "feed": "iex",
        }
    )
    url = f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/bars?{qs}"
    try:
        body = _get_json(url)
    except Exception as exc:
        return {"price_at_news": None, "lag_bar_ts": None, "error": str(exc)[:200]}
    bars = body.get("bars") or []
    if not bars:
        return {"price_at_news": None, "lag_bar_ts": None}
    best = None
    best_dt = None
    best_abs = None
    for b in bars:
        bt = _parse_ts(b.get("t"))
        if bt is None:
            continue
        delta = abs((bt - t0).total_seconds())
        if best_abs is None or delta < best_abs:
            best_abs = delta
            best = float(b["o"])
            best_dt = b.get("t")
    return {
        "price_at_news": best,
        "lag_bar_ts": best_dt,
        "news_to_bar_sec": best_abs,
    }


def previous_close(symbol: str) -> float | None:
    """Most recent completed daily close (overnight / premarket chase reference)."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=14)
    qs = urllib.parse.urlencode(
        {
            "timeframe": "1Day",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 8,
            "adjustment": "split",
            "feed": "iex",
        }
    )
    url = f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/bars?{qs}"
    try:
        body = _get_json(url)
    except Exception:
        return None
    bars = body.get("bars") or []
    if not bars:
        return None
    try:
        # Skip today's in-progress daily bar so gap chase uses prior session close.
        try:
            from zoneinfo import ZoneInfo

            ny = ZoneInfo("America/New_York")
            today_ny = datetime.now(ny).date()
            last_t = _parse_ts(bars[-1].get("t"))
            last_ny = last_t.astimezone(ny).date() if last_t else None
        except Exception:
            today_ny = datetime.now(timezone.utc).date()
            last_t = _parse_ts(bars[-1].get("t"))
            last_ny = last_t.date() if last_t else None
        if last_ny is not None and last_ny >= today_ny and len(bars) >= 2:
            return float(bars[-2]["c"])
        return float(bars[-1]["c"])
    except Exception:
        return None


def chase_ref_price(symbol: str, news_created_at: Any) -> dict[str, Any]:
    """Reference for chase gate: 1-min bar near news, else previous daily close."""
    near = price_near_news(symbol, news_created_at)
    if near.get("price_at_news"):
        return {**near, "ref_source": "news_bar"}
    px = previous_close(symbol)
    if px and px > 0:
        return {
            "price_at_news": px,
            "lag_bar_ts": None,
            "news_to_bar_sec": None,
            "ref_source": "prev_close",
        }
    return {
        "price_at_news": None,
        "lag_bar_ts": None,
        "ref_source": None,
        "error": near.get("error"),
    }


def entry_vs_ref_pct(entry_price: float, ref_price: float | None) -> float | None:
    if not ref_price or ref_price <= 0 or not entry_price or entry_price <= 0:
        return None
    return round((float(entry_price) / float(ref_price) - 1.0) * 100.0, 3)


def news_age_sec(news_created_at: Any, *, now: datetime | None = None) -> float | None:
    t0 = _parse_ts(news_created_at)
    if t0 is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - t0).total_seconds())


def chase_skip_reason(
    *,
    entry_price: float,
    news_created_at: Any,
    symbol: str,
    age_sec: float | None = None,
    max_entry_vs_news_pct: float | None = None,
    max_news_age_sec: float | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Return (skip_reason, audit) for late/chase entries. None reason = allow."""
    max_move = (
        MAX_ENTRY_VS_NEWS_PCT
        if max_entry_vs_news_pct is None
        else float(max_entry_vs_news_pct)
    )
    max_age = MAX_NEWS_AGE_SEC if max_news_age_sec is None else float(max_news_age_sec)
    age = age_sec if age_sec is not None else news_age_sec(news_created_at)
    ref = chase_ref_price(symbol, news_created_at)
    move = entry_vs_ref_pct(entry_price, ref.get("price_at_news"))
    audit = {
        "price_at_news": ref.get("price_at_news"),
        "entry_vs_news_pct": move,
        "ref_source": ref.get("ref_source"),
        "lag_bar_ts": ref.get("lag_bar_ts"),
        "news_age_sec": age,
    }
    if age is not None and age > max_age:
        return (
            f"stale news_age={age:.0f}s > {max_age:.0f}s",
            audit,
        )
    if move is not None and move > max_move:
        return (
            f"chase entry_vs_news={move:.2f}% > {max_move:.2f}% "
            f"(ref={ref.get('ref_source')})",
            audit,
        )
    return None, audit


def lag_breakdown(
    *,
    news_created_at: Any,
    received_at: Any,
    decision_at: datetime | None = None,
) -> dict[str, float | None]:
    """Split total age into ingestion vs decision lag.

    ingestion_lag: created_at → local received_at  (WS/REST arrival)
    decision_lag:  received_at → order/score time  (batch score + place)
    news_age_sec:  created_at → decision_at        (total)
    """
    decision_at = decision_at or datetime.now(timezone.utc)
    created = _parse_ts(news_created_at)
    received = _parse_ts(received_at)
    ingestion = None
    decision = None
    total = None
    if created is not None and received is not None:
        ingestion = max(0.0, (received - created).total_seconds())
    if received is not None:
        decision = max(0.0, (decision_at - received).total_seconds())
    if created is not None:
        total = max(0.0, (decision_at - created).total_seconds())
    return {
        "ingestion_lag_sec": ingestion,
        "decision_lag_sec": decision,
        "news_age_sec": total,
    }


EARNINGS_KEYS = (
    "earnings",
    "eps",
    "guidance",
    "beats",
    "misses",
    "revenue",
    "outlook",
    "quarterly results",
    "q1",
    "q2",
    "q3",
    "q4",
)


def news_catalyst_type(item: dict[str, Any]) -> str:
    # Prefer structured SEC item mapping when present
    pre = item.get("catalyst_type")
    if pre:
        return str(pre)
    items = item.get("sec_items") or []
    if items:
        for code, cat in (
            ("2.02", "earnings"),
            ("2.01", "contract"),
            ("1.01", "contract"),
            ("5.02", "management"),
            ("1.05", "cyber"),
        ):
            if code in items:
                return cat
    text = f"{item.get('headline') or ''} {item.get('summary') or ''}".lower()
    if any(k in text for k in EARNINGS_KEYS):
        return "earnings"
    if any(k in text for k in ("upgrade", "downgrade", "price target", "initiates")):
        return "analyst"
    if any(k in text for k in ("contract", "deal", "partnership", "wins", "award")):
        return "contract"
    if any(k in text for k in ("product", "launch", "chip", "gpu", "ai ")):
        return "product"
    return "other"


def prioritize_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SEC first, then non-earnings, then earnings last — SCORE_LIMIT burial guard.

    Earnings are de-prioritized for scoring slots (paper audit: they concentrate
    losses). Fresh non-earnings catalysts get the limited DeepSeek budget first.
    """

    def key(n: dict[str, Any]) -> tuple:
        feed = str(n.get("feed") or "")
        sec_rank = 0 if feed == "sec_edgar" else 1
        cat = news_catalyst_type(n)
        # Push earnings behind product/contract/analyst/other.
        earnings_rank = 1 if cat == "earnings" else 0
        t0 = _parse_ts(n.get("created_at"))
        ts = -(t0.timestamp()) if t0 else 0.0
        return (sec_rank, earnings_rank, ts)

    return sorted(items, key=key)
