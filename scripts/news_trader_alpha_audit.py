# -*- coding: utf-8 -*-
"""Audit News AI paper trades: confidence, lag, earnings contribution, hold脳TP.

Reads STORE/signals.jsonl + STORE/trades.jsonl + STORE/outcomes.jsonl.

  python scripts/news_trader_alpha_audit.py
  NEWS_TRADER_STORE=${QUANTDESK_ROOT:-.}/data/store/news_trader \\
    python scripts/news_trader_alpha_audit.py

Flags (research discipline, two-sided like single-name concentration):
  earnings share of gross positive OR gross negative PnL >40% 鈫?WARN, >50% 鈫?HARD FLAG
  Also reports net PnL contribution separately.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path


STORE = Path(os.environ.get("NEWS_TRADER_STORE", "data/store/news_trader"))
EARN_WARN = float(os.environ.get("NEWS_TRADER_EARN_WARN", "0.40"))
EARN_HARD = float(os.environ.get("NEWS_TRADER_EARN_HARD", "0.50"))


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _bucket(conf: float) -> str:
    edges = [0.50, 0.60, 0.68, 0.75, 0.85, 1.01]
    labels = ["<0.50", "0.50-0.60", "0.60-0.68", "0.68-0.75", "0.75-0.85", "0.85-1.00"]
    for e, lab in zip(edges, labels):
        if conf < e:
            return lab
    return labels[-1]


def _hold_bucket(h: float) -> str:
    if h <= 12:
        return "6-12h"
    if h <= 24:
        return "12-24h"
    return "24-48h"


def _age_bucket(sec: float | None) -> str:
    if sec is None:
        return "unknown"
    m = sec / 60.0
    if m < 5:
        return "<5m"
    if m < 15:
        return "5-15m"
    if m < 60:
        return "15-60m"
    return ">60m"


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    i = int(round((len(ys) - 1) * q))
    return ys[max(0, min(len(ys) - 1, i))]


def _summ(xs: list[float]) -> str:
    if not xs:
        return "n=  0"
    wins = sum(1 for x in xs if x > 0)
    return (
        f"n={len(xs):3d}  win={wins/len(xs)*100:5.1f}%  "
        f"avg={sum(xs)/len(xs):+6.2f}%  sum={sum(xs):+7.2f}%"
    )


def _closed_rows(sells: list[dict], outcomes: list[dict]) -> list[dict]:
    """Unify sell trades + outcomes.jsonl into one row shape."""
    rows: list[dict] = []
    for t in sells:
        m = t.get("meta") or {}
        pnl = float(
            t.get("pnl_pct")
            if t.get("pnl_pct") is not None
            else m.get("exit_pnl_pct")
            or 0
        )
        rows.append(
            {
                "symbol": t.get("symbol") or m.get("symbol"),
                "pnl_pct": pnl,
                "reason": t.get("reason") or m.get("exit_reason") or "",
                "confidence": float(m.get("confidence") or 0),
                "catalyst_type": str(m.get("catalyst_type") or "unknown"),
                "news_age_sec": m.get("news_age_sec"),
                "hold_hours": float(m.get("hold_hours") or 24),
                "take_profit_pct": m.get("take_profit_pct"),
                "entry_vs_news_pct": m.get("entry_vs_news_pct"),
            }
        )
    # outcomes may duplicate sells; only add if no sells yet or denser fields
    if not sells:
        for o in outcomes:
            rows.append(
                {
                    "symbol": o.get("symbol"),
                    "pnl_pct": float(o.get("pnl_pct") or 0),
                    "reason": o.get("reason") or "",
                    "confidence": float(o.get("confidence") or 0),
                    "catalyst_type": str(o.get("catalyst_type") or "unknown"),
                    "news_age_sec": o.get("news_age_sec"),
                    "hold_hours": float(o.get("hold_hours") or 24),
                    "take_profit_pct": o.get("take_profit_pct"),
                    "entry_vs_news_pct": o.get("entry_vs_news_pct"),
                }
            )
    return rows


def main() -> int:
    signals = _load_jsonl(STORE / "signals.jsonl")
    trades = _load_jsonl(STORE / "trades.jsonl")
    outcomes = _load_jsonl(STORE / "outcomes.jsonl")
    print(f"store={STORE}")
    print(
        f"signals={len(signals)} trade_rows={len(trades)} "
        f"outcomes={len(outcomes)}"
    )

    by_bucket: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "buy": 0})
    for s in signals:
        sc = s.get("score") or {}
        conf = float(sc.get("confidence") or 0)
        b = _bucket(conf)
        by_bucket[b]["n"] += 1
        if sc.get("action") == "buy":
            by_bucket[b]["buy"] += 1
    print("\n== confidence buckets (scored news) ==")
    for b in ["<0.50", "0.50-0.60", "0.60-0.68", "0.68-0.75", "0.75-0.85", "0.85-1.00"]:
        d = by_bucket.get(b) or {"n": 0, "buy": 0}
        print(f"  {b:12s}  n={d['n']:4d}  buy_action={d['buy']:4d}")

    buys = [t for t in trades if t.get("side") == "buy"]
    sells = [t for t in trades if t.get("side") == "sell"]

    move_from_news = []
    for t in buys:
        m = t.get("meta") or {}
        pan = m.get("price_at_news")
        ep = m.get("entry_price")
        if pan and ep and float(pan) > 0:
            move_from_news.append((float(ep) / float(pan) - 1.0) * 100.0)

    ingests, decisions, totals = [], [], []
    for s in signals:
        if s.get("ingestion_lag_sec") is not None:
            ingests.append(float(s["ingestion_lag_sec"]))
        if s.get("decision_lag_sec") is not None:
            decisions.append(float(s["decision_lag_sec"]))
        if s.get("news_age_sec") is not None:
            totals.append(float(s["news_age_sec"]))
    if not ingests and not decisions:
        for t in buys:
            m = t.get("meta") or {}
            if m.get("ingestion_lag_sec") is not None:
                ingests.append(float(m["ingestion_lag_sec"]))
            if m.get("decision_lag_sec") is not None:
                decisions.append(float(m["decision_lag_sec"]))
            if m.get("news_age_sec") is not None:
                totals.append(float(m["news_age_sec"]))

    def _lag_line(name: str, xs: list[float]) -> None:
        if not xs:
            print(f"  {name}: (no samples)")
            return
        print(
            f"  {name}: n={len(xs)}  median={_pct(xs, 0.5):.0f}s  "
            f"p25={_pct(xs, 0.25):.0f}s  p75={_pct(xs, 0.75):.0f}s  "
            f"p90={_pct(xs, 0.9):.0f}s"
        )

    print("\n== lag breakdown (ingestion vs decision) ==")
    print("  ingestion = created_at 鈫?received_at (WS/REST arrival)")
    print("  decision  = received_at 鈫?score/order time (batch cadence)")
    _lag_line("ingestion_lag", ingests)
    _lag_line("decision_lag", decisions)
    _lag_line("news_age_total", totals)
    print("\n== entry vs price_at_news (positive = bought after run-up) ==")
    if move_from_news:
        print(
            f"  n={len(move_from_news)}  median={_pct(move_from_news, 0.5):.2f}%  "
            f"p75={_pct(move_from_news, 0.75):.2f}%  "
            f"mean={sum(move_from_news)/len(move_from_news):.2f}%"
        )
    else:
        print("  (no price_at_news samples yet)")

    closed = _closed_rows(sells, outcomes)
    print("\n== closed outcomes ==")
    if not closed:
        print("  (no sells/outcomes yet 鈥?re-run after exits accumulate)")
        print("\n== earnings contribution (discipline check) ==")
        print("  (no closed trades)")
        return 0

    conf_out: dict[str, list[float]] = defaultdict(list)
    cat_out: dict[str, list[float]] = defaultdict(list)
    sym_out: dict[str, list[float]] = defaultdict(list)
    hold_out: dict[str, list[dict]] = defaultdict(list)
    for r in closed:
        pnl = float(r["pnl_pct"])
        conf_out[_bucket(float(r["confidence"]))].append(pnl)
        cat_out[str(r["catalyst_type"])].append(pnl)
        sym_out[str(r.get("symbol") or "?")].append(pnl)
        hold_out[_hold_bucket(float(r["hold_hours"]))].append(r)

    print("  by confidence:")
    for b in ["0.68-0.75", "0.75-0.85", "0.85-1.00", "0.60-0.68", "0.50-0.60", "<0.50"]:
        if b in conf_out:
            print(f"    {b:12s}  {_summ(conf_out[b])}")
    print("  by catalyst:")
    for cat, xs in sorted(cat_out.items(), key=lambda kv: -sum(kv[1])):
        print(f"    {cat:12s}  {_summ(xs)}")
    print("  by symbol (top contributors):")
    for sym, xs in sorted(sym_out.items(), key=lambda kv: -sum(kv[1]))[:15]:
        print(f"    {sym:8s}  {_summ(xs)}")

    # --- earnings contribution (two-sided, like single-name concentration) ---
    print("\n== earnings contribution (discipline check, two-sided) ==")
    earn = [r for r in closed if r["catalyst_type"] == "earnings"]
    non = [r for r in closed if r["catalyst_type"] != "earnings"]
    n = len(closed)
    trade_share = len(earn) / n if n else 0.0
    pos_all = sum(max(0.0, float(r["pnl_pct"])) for r in closed)
    pos_earn = sum(max(0.0, float(r["pnl_pct"])) for r in earn)
    neg_all = sum(min(0.0, float(r["pnl_pct"])) for r in closed)  # 鈮?0
    neg_earn = sum(min(0.0, float(r["pnl_pct"])) for r in earn)
    pos_contrib = (pos_earn / pos_all) if pos_all > 1e-9 else 0.0
    # Share of gross losses (magnitude): earnings_loss / all_loss
    neg_mag_all = abs(neg_all)
    neg_mag_earn = abs(neg_earn)
    neg_contrib = (neg_mag_earn / neg_mag_all) if neg_mag_all > 1e-9 else 0.0
    sum_all = sum(float(r["pnl_pct"]) for r in closed)
    sum_earn = sum(float(r["pnl_pct"]) for r in earn)
    net_contrib = (sum_earn / sum_all) if abs(sum_all) > 1e-9 else 0.0
    print(f"  closed={n}  earnings_trades={len(earn)}  trade_share={trade_share*100:.1f}%")
    print(
        f"  pos_contrib:  earnings {pos_earn:+.2f}% / all {pos_all:+.2f}%  "
        f"= {pos_contrib*100:.1f}%"
    )
    print(
        f"  neg_contrib:  earnings {neg_earn:+.2f}% / all {neg_all:+.2f}%  "
        f"= {neg_contrib*100:.1f}% of gross losses"
    )
    print(
        f"  net_contrib:  earnings {sum_earn:+.2f}% / all {sum_all:+.2f}%  "
        f"= {net_contrib*100:.1f}%  (other {sum_all-sum_earn:+.2f}%)"
    )
    hot = max(pos_contrib, neg_contrib)
    side = "pos" if pos_contrib >= neg_contrib else "neg"
    if hot >= EARN_HARD:
        print(
            f"  HARD FLAG: earnings {side} contribution "
            f"{hot*100:.1f}% >= {EARN_HARD*100:.0f}% 鈥?"
            f"concentration risk (winners and/or losers); "
            f"check PEAD vs news alpha"
        )
    elif hot >= EARN_WARN:
        print(
            f"  WARN: earnings {side} contribution "
            f"{hot*100:.1f}% >= {EARN_WARN*100:.0f}% 鈥?"
            f"two-sided concentration watch"
        )
    else:
        print(
            f"  OK: max(pos,neg) contrib {hot*100:.1f}% "
            f"< {EARN_WARN*100:.0f}% warn line"
        )
    if non:
        print(f"  non-earnings: {_summ([float(r['pnl_pct']) for r in non])}")
    if earn:
        print(f"  earnings:     {_summ([float(r['pnl_pct']) for r in earn])}")

    # --- hold_hours 脳 TP hit (for later TP scaling decision) ---
    print("\n== hold_hours 脳 TP hit rate (scale TP later if declining) ==")
    for hb in ["6-12h", "12-24h", "24-48h"]:
        rs = hold_out.get(hb) or []
        if not rs:
            print(f"  {hb:8s}  (no samples)")
            continue
        tp_hits = sum(1 for r in rs if "take_profit" in str(r.get("reason") or ""))
        sl_hits = sum(1 for r in rs if "stop_loss" in str(r.get("reason") or ""))
        time_hits = sum(1 for r in rs if "time_exit" in str(r.get("reason") or ""))
        pnls = [float(r["pnl_pct"]) for r in rs]
        print(
            f"  {hb:8s}  {_summ(pnls)}  "
            f"tp_hit={tp_hits/len(rs)*100:4.1f}%  "
            f"sl_hit={sl_hits/len(rs)*100:4.1f}%  "
            f"time_hit={time_hits/len(rs)*100:4.1f}%"
        )

    # --- confidence incremental vs catalyst / news_age ---
    print("\n== confidence incremental info (vs catalyst / news_age) ==")
    print("  within catalyst (does higher conf still separate?):")
    for cat in sorted(cat_out.keys()):
        rows_c = [r for r in closed if r["catalyst_type"] == cat]
        if len(rows_c) < 4:
            print(f"    {cat:12s}  n={len(rows_c)} (need 鈮?)")
            continue
        hi = [float(r["pnl_pct"]) for r in rows_c if float(r["confidence"]) >= 0.75]
        lo = [float(r["pnl_pct"]) for r in rows_c if float(r["confidence"]) < 0.75]
        if not hi or not lo:
            print(f"    {cat:12s}  n={len(rows_c)} (need both conf sides)")
            continue
        print(
            f"    {cat:12s}  conf鈮?.75 {_summ(hi)}  |  "
            f"conf<0.75 {_summ(lo)}"
        )
    print("  by news_age at entry:")
    age_out: dict[str, list[float]] = defaultdict(list)
    for r in closed:
        age = r.get("news_age_sec")
        age_f = float(age) if age is not None else None
        age_out[_age_bucket(age_f)].append(float(r["pnl_pct"]))
    for ab in ["<5m", "5-15m", "15-60m", ">60m", "unknown"]:
        if ab in age_out:
            print(f"    {ab:8s}  {_summ(age_out[ab])}")

    # crude: within same age bucket, conf high vs low
    print("  within news_age (conf incremental?):")
    for ab in ["<5m", "5-15m", "15-60m", ">60m"]:
        rows_a = [
            r
            for r in closed
            if _age_bucket(
                float(r["news_age_sec"]) if r.get("news_age_sec") is not None else None
            )
            == ab
        ]
        if len(rows_a) < 4:
            continue
        hi = [float(r["pnl_pct"]) for r in rows_a if float(r["confidence"]) >= 0.75]
        lo = [float(r["pnl_pct"]) for r in rows_a if float(r["confidence"]) < 0.75]
        if hi and lo:
            print(
                f"    {ab:8s}  conf鈮?.75 {_summ(hi)}  |  "
                f"conf<0.75 {_summ(lo)}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
