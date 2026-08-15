# -*- coding: utf-8 -*-
"""DeepSeek news scoring for short-horizon long-only trades.

Default: SiliconFlow DeepSeek-V4-Flash (NEWS_TRADER_MODEL).
Escalate to V4-Pro for SEC high-signal filings, or when Flash
confidence lands in the edge band (default 0.65–0.75).
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .sec_edgar import HIGH_SIGNAL_ITEMS
from .universe import TECH_SET, canonical_symbol

DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
DEFAULT_PRO_MODEL = "deepseek-ai/DeepSeek-V4-Pro"
_6K_HIGH_CATS = frozenset(
    {"earnings", "contract", "management", "cyber", "product", "analyst"}
)
_PRO_SEM = threading.Semaphore(
    max(1, int(os.environ.get("NEWS_TRADER_PRO_WORKERS", "2")))
)


def score_model() -> str:
    return (
        os.environ.get("NEWS_TRADER_MODEL", "").strip()
        or os.environ.get("DEEPSEEK_MODEL", "").strip()
        or DEFAULT_MODEL
    )


def pro_model() -> str:
    return (
        os.environ.get("NEWS_TRADER_PRO_MODEL", "").strip() or DEFAULT_PRO_MODEL
    )


def escalate_pro() -> bool:
    return os.environ.get("NEWS_TRADER_ESCALATE_PRO", "1").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def edge_band() -> tuple[float, float]:
    lo = float(os.environ.get("NEWS_TRADER_EDGE_CONF_LO", "0.65"))
    hi = float(os.environ.get("NEWS_TRADER_EDGE_CONF_HI", "0.75"))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def score_timeout_sec(model: str | None = None) -> float:
    m = (model or score_model()).lower()
    if "flash" not in m:
        return max(8.0, float(os.environ.get("NEWS_TRADER_PRO_TIMEOUT_SEC", "60")))
    return max(5.0, float(os.environ.get("NEWS_TRADER_SCORE_TIMEOUT_SEC", "30")))


def is_sec_high_signal(item: dict[str, Any]) -> bool:
    """8-K items in HIGH_SIGNAL_ITEMS, or 6-K with a real catalyst tag."""
    codes = [str(c).strip() for c in (item.get("sec_items") or [])]
    if any(c in HIGH_SIGNAL_ITEMS for c in codes):
        return True
    form = str(item.get("sec_form") or "").upper()
    feed = str(item.get("feed") or "")
    if feed == "sec_edgar" and form.startswith("6-K"):
        cat = str(item.get("catalyst_type") or "").lower()
        return cat in _6K_HIGH_CATS
    return False


def _is_edge_conf(score: dict[str, Any]) -> bool:
    lo, hi = edge_band()
    conf = float(score.get("confidence") or 0)
    return lo <= conf <= hi


SYSTEM = """You are a US equities short-horizon trading analyst.
Decide if news justifies a SHORT-TERM LONG buy for the next 6-48 hours.

Rules:
- BUY only for clearly bullish catalysts on one tech stock
  (contract win, earnings beat, major upgrade, product breakthrough).
- SKIP vague roundups, ETF chatter, historical performance pieces,
  lawsuits, downgrades, or already-priced generic AI buzz.
- Prefer one ticker from the provided tech candidates.
- confidence MUST be your estimated probability the long is profitable
  within hold_hours (calibrated, not rhetorical certainty). Use
  0.55-0.65 for weak edges, 0.68-0.80 for clear catalysts, >0.85 rarely.
- take_profit_pct / stop_loss_pct are your input; runtime HARD-CLAMPS
  them into an ATR band (≈0.8–1.2× 1.5ATR, global floor/cap). Propose
  levels inside that band — extremes like 1%/12% will be clamped.
- Your ENTIRE reply must be a single JSON object. No markdown. No analysis text.
JSON keys:
action ("buy"|"skip"), symbol (ticker|null), confidence (0-1),
thesis (short string), hold_hours (6-48), take_profit_pct, stop_loss_pct
"""


def _deepseek_chat(
    system: str,
    user: str,
    *,
    model: str,
    temperature: float = 0.1,
    max_tokens: int = 1200,
) -> str:
    api_key = (
        os.environ.get("DEEPSEEK_API_KEY", "").strip()
        or os.environ.get("SILICONFLOW_API_KEY", "").strip()
    )
    base = os.environ.get(
        "DEEPSEEK_BASE_URL", "https://api.siliconflow.cn/v1"
    ).rstrip("/")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY missing — news scorer requires SiliconFlow "
            "DeepSeek. Set DEEPSEEK_API_KEY or SILICONFLOW_API_KEY in .env"
        )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        # SiliconFlow DeepSeek/Qwen thinking mode is slow/busy for news scoring.
        "enable_thinking": False,
    }
    # SiliconFlow DeepSeek-V4-Flash hangs on response_format=json_object;
    # Pro handles it. Prompt already requires a pure JSON object.
    if "flash" not in model.lower():
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + api_key)
    with urllib.request.urlopen(
        req,
        timeout=score_timeout_sec(model),
        context=ssl.create_default_context(),
    ) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = body["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    # Some thinking-mode replies put text in reasoning_content
    reasoning = (msg.get("reasoning_content") or "").strip()
    return content if content else reasoning


def _parse_json(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty model response")
    text = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{[^{}]*\}", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    m2 = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m2:
        blob = m2.group(0)
        blob = re.sub(r",\s*}", "}", blob)
        blob = re.sub(r",\s*]", "]", blob)
        try:
            return json.loads(blob)
        except Exception:
            pass
    raise ValueError("no JSON: " + text[:400])


def _normalize(data: dict[str, Any], raw: str, *, model: str) -> dict[str, Any]:
    action = str(data.get("action") or "skip").lower().strip()
    if action in {"long", "buy_long", "enter", "bullish"}:
        action = "buy"
    symbol = data.get("symbol")
    if symbol:
        symbol = canonical_symbol(symbol)
        if symbol in {"null", "none", "nil"}:
            symbol = None
    if action != "buy" or not symbol or symbol not in TECH_SET:
        action = "skip"
        symbol = None
    conf = float(data.get("confidence") or 0)
    hold = int(data.get("hold_hours") or 24)
    hold = max(6, min(48, hold))
    tp = float(data.get("take_profit_pct") or 5.5)
    sl = float(data.get("stop_loss_pct") or 3.5)
    return {
        "action": action,
        "symbol": symbol,
        "confidence": max(0.0, min(1.0, conf)),
        "thesis": str(data.get("thesis") or "")[:400],
        "hold_hours": hold,
        "take_profit_pct": max(1.0, min(12.0, tp)),
        "stop_loss_pct": max(1.0, min(8.0, abs(sl))),
        "raw": raw[:500],
        "provider": "deepseek",
        "model": model,
        "route": "flash",
    }


def _heuristic_from_text(raw: str, candidates: list[str]) -> dict[str, Any] | None:
    """Last-resort parse when model refuses to emit clean JSON."""
    text = raw or ""
    low = text.lower()
    # Prefer explicit skip
    if re.search(r'"action"\s*:\s*"skip"', low) or "action\": \"skip" in low:
        return {
            "action": "skip",
            "symbol": None,
            "confidence": 0.2,
            "thesis": "heuristic-skip",
            "hold_hours": 24,
            "take_profit_pct": 5.5,
            "stop_loss_pct": 3.5,
        }
    m_act = re.search(r'"action"\s*:\s*"(buy|skip|long)"', text, re.I)
    m_sym = re.search(r'"symbol"\s*:\s*"([A-Z]{1,6})"', text)
    m_conf = re.search(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)', text)
    if m_act and m_act.group(1).lower() in {"buy", "long"} and m_sym:
        sym = m_sym.group(1).upper()
        if sym in TECH_SET and (not candidates or sym in candidates):
            return {
                "action": "buy",
                "symbol": sym,
                "confidence": float(m_conf.group(1)) if m_conf else 0.7,
                "thesis": "heuristic-buy",
                "hold_hours": 24,
                "take_profit_pct": 5.5,
                "stop_loss_pct": 3.5,
            }
    if "skip" in low and "buy" not in low.split("skip")[0][-40:]:
        return {
            "action": "skip",
            "symbol": None,
            "confidence": 0.15,
            "thesis": "heuristic-skip-text",
            "hold_hours": 24,
            "take_profit_pct": 5.5,
            "stop_loss_pct": 3.5,
        }
    return None


def _score_once_unlocked(
    candidates: list[str],
    user: str,
    *,
    model: str,
) -> dict[str, Any]:
    raw = _deepseek_chat(SYSTEM, user, model=model)
    data = None
    try:
        data = _parse_json(raw)
    except Exception:
        try:
            raw2 = _deepseek_chat(
                "You are a JSON formatter. Reply with exactly one JSON object.",
                "Fill this template using the analysis. Example: "
                '{"action":"skip","symbol":null,"confidence":0.2,"thesis":"x",'
                '"hold_hours":24,"take_profit_pct":5.5,"stop_loss_pct":3.5}\n\n'
                "Analysis:\n" + raw[:1200],
                model=model,
                temperature=0.0,
                max_tokens=300,
            )
            raw = raw2
            data = _parse_json(raw2)
        except Exception:
            data = _heuristic_from_text(raw, candidates)
            if data is None:
                raise
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("model did not return object")
    return _normalize(data, raw, model=model)


def _score_once(
    candidates: list[str],
    user: str,
    *,
    model: str,
) -> dict[str, Any]:
    if "flash" not in model.lower():
        with _PRO_SEM:
            return _score_once_unlocked(candidates, user, model=model)
    return _score_once_unlocked(candidates, user, model=model)


def score_news(item: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        canonical_symbol(s)
        for s in (item.get("symbols") or [])
        if canonical_symbol(s) in TECH_SET
    ]
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return {
            "action": "skip",
            "symbol": None,
            "confidence": 0.0,
            "thesis": "no tech universe ticker",
            "hold_hours": 24,
            "take_profit_pct": 5.5,
            "stop_loss_pct": 3.5,
            "provider": "none",
            "model": "",
            "route": "no_ticker",
        }
    sec_bits = ""
    if item.get("sec_items") or item.get("feed") == "sec_edgar":
        sec_bits = (
            f"Source: SEC EDGAR {item.get('sec_form') or 'filing'} "
            f"(primary filing, not a news rewrite)\n"
            f"SEC items: {item.get('sec_items') or '(none — 6-K free text)'}\n"
            f"Catalyst hint: {item.get('catalyst_type')}\n"
            f"Accession: {item.get('sec_accession')}\n"
        )
    user = (
        f"Headline: {item.get('headline')}\n"
        f"Summary: {item.get('summary') or '(none)'}\n"
        f"{sec_bits}"
        f"News symbols: {item.get('symbols')}\n"
        f"Tech candidates: {candidates}\n"
        f"Created: {item.get('created_at')}\n"
        "Output ONLY the JSON object now."
    )
    flash = score_model()
    pro = pro_model()
    use_pro = escalate_pro()

    if use_pro and is_sec_high_signal(item):
        try:
            out = _score_once(candidates, user, model=pro)
            out["route"] = "sec_pro"
            return out
        except Exception:
            out = _score_once(candidates, user, model=flash)
            out["route"] = "sec_pro_fallback_flash"
            return out

    out = _score_once(candidates, user, model=flash)
    out["route"] = "flash"
    if use_pro and _is_edge_conf(out):
        try:
            reviewed = _score_once(candidates, user, model=pro)
            reviewed["route"] = "edge_pro"
            reviewed["flash_action"] = out.get("action")
            reviewed["flash_confidence"] = out.get("confidence")
            reviewed["flash_model"] = out.get("model")
            return reviewed
        except Exception:
            out["route"] = "edge_pro_fail"
            return out
    return out


def score_news_batch(
    items: list[dict[str, Any]],
    *,
    workers: int = 4,
) -> list[tuple[dict[str, Any], dict[str, Any] | None, BaseException | None]]:
    """Score items in parallel; results stay in input order."""
    if not items:
        return []
    n = max(1, min(int(workers), len(items)))
    if n == 1 or len(items) == 1:
        out: list[
            tuple[dict[str, Any], dict[str, Any] | None, BaseException | None]
        ] = []
        for it in items:
            try:
                out.append((it, score_news(it), None))
            except BaseException as exc:
                out.append((it, None, exc))
        return out
    slots: list[
        tuple[dict[str, Any], dict[str, Any] | None, BaseException | None] | None
    ] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = {pool.submit(score_news, it): i for i, it in enumerate(items)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                slots[i] = (items[i], fut.result(), None)
            except BaseException as exc:
                slots[i] = (items[i], None, exc)
    return [s for s in slots if s is not None]
