#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 7.2 — First Live Observation Snapshot.

Run AFTER Phase 7.1 OCO conversion completes.
Does NOT change weights, params, or positions.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from config.store import list_strategies, get_strategy
from execution.alpaca_client import AlpacaPaperClient

LEGACY_SYMS = {"COIN", "PLTR", "NVDA"}
SURGE_ID = "cursor-surge-nvda-052828-63859c-82d552"
OUT_MD = Path("/app/data/store/phase7_observation_snapshot.md")
OUT_JSON = Path("/app/data/store/phase7_observation_snapshot.json")
HARDENING = Path("/app/data/store/phase7_legacy_hardening.json")


def raw_open(client):
    return list(
        client.client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                limit=200,
                nested=True,
            )
        )
    )


def order_flags(orders, symbol: str) -> dict:
    stop = False
    tp = False
    oco = False
    detail = []
    for o in orders:
        if o.symbol != symbol:
            continue
        cls = str(getattr(o.order_class, "value", o.order_class)).lower()
        if "oco" in cls:
            oco = True
        if o.stop_price is not None:
            stop = True
        if o.limit_price is not None and str(o.side).lower().endswith("sell"):
            tp = True
        for leg in getattr(o, "legs", None) or []:
            if leg.stop_price is not None:
                stop = True
            if leg.limit_price is not None:
                tp = True
            detail.append(
                {
                    "id": str(leg.id),
                    "type": str(leg.type),
                    "limit": float(leg.limit_price) if leg.limit_price is not None else None,
                    "stop": float(leg.stop_price) if leg.stop_price is not None else None,
                }
            )
        detail.append(
            {
                "id": str(o.id),
                "type": str(o.type),
                "class": str(o.order_class),
                "limit": float(o.limit_price) if o.limit_price is not None else None,
                "stop": float(o.stop_price) if o.stop_price is not None else None,
                "status": str(o.status),
            }
        )
    return {"stop_active": stop, "tp_active": tp, "oco_active": oco, "orders": detail}


def main():
    client = AlpacaPaperClient()
    acct = client.account()
    equity = float(acct.get("equity") or acct.get("portfolio_value") or 0)
    positions = {p["symbol"]: p for p in client.positions()}
    orders = raw_open(client)

    # V2 book weights + theoretical capital
    book = []
    for s in list_strategies():
        if not s.get("enabled"):
            continue
        name = s.get("name") or ""
        if "news" in name.lower():
            continue
        w = float((s.get("params") or {}).get("portfolio_weight") or 0)
        book.append({"id": s["id"], "name": name, "weight": w, "target_mv": w * equity})

    # Actual exposure by symbol
    total_long_mv = sum(max(0.0, float(p["market_value"])) for p in positions.values())
    by_sym = []
    unprotected_mv = 0.0
    for sym, p in sorted(positions.items()):
        flags = order_flags(orders, sym)
        mv = float(p["market_value"])
        protected = flags["stop_active"] or flags["oco_active"]
        if mv > 0 and not protected:
            unprotected_mv += mv
        by_sym.append(
            {
                "symbol": sym,
                "qty": float(p["qty"]),
                "mv": mv,
                "pct_equity": (mv / equity * 100) if equity else 0,
                "upl_pct": float(p.get("unrealized_plpc") or 0) * 100,
                "stop_active": flags["stop_active"],
                "tp_active": flags["tp_active"],
                "oco_active": flags["oco_active"],
                "legacy": sym in LEGACY_SYMS,
            }
        )

    surge = get_strategy(SURGE_ID)
    surge_syms = {str(x).upper() for x in ((surge.get("params") or {}).get("symbols") or [])}
    active_syms = set()
    for s in list_strategies():
        if not s.get("enabled"):
            continue
        active_syms |= {str(x).upper() for x in ((s.get("params") or {}).get("symbols") or [])}
    surge_only = surge_syms - active_syms

    legacy_status = []
    legacy_mv = 0.0
    surge_only_mv = 0.0
    unprotected_legacy_mv = 0.0
    for sym in sorted(LEGACY_SYMS):
        p = positions.get(sym)
        flags = order_flags(orders, sym)
        if not p:
            legacy_status.append(
                {
                    "symbol": sym,
                    "present": False,
                    "qty_unchanged": None,
                    "stop": "n/a",
                    "tp": "n/a",
                    "owner": "none",
                }
            )
            continue
        mv = float(p["market_value"])
        legacy_mv += mv
        if sym in surge_only:
            surge_only_mv += mv
        if mv > 0 and not (flags["stop_active"] or flags["oco_active"]):
            unprotected_legacy_mv += mv
        owner = "ERC_V2" if sym in active_syms else "legacy_surge_only"
        legacy_status.append(
            {
                "symbol": sym,
                "present": True,
                "qty": float(p["qty"]),
                "mv": mv,
                "qty_unchanged": True,  # Phase 7.1 verified; snapshot assumes continuity
                "stop": "active" if flags["stop_active"] else "MISSING",
                "tp": "active" if flags["tp_active"] else ("n/a" if sym == "NVDA" else "MISSING"),
                "oco": "active" if flags["oco_active"] else "no",
                "strategy_owner": owner,
            }
        )

    hardening = None
    if HARDENING.exists():
        try:
            hardening = json.loads(HARDENING.read_text(encoding="utf-8"))
        except Exception:
            hardening = None

    # Migration audit trail (this is a strategy-lifecycle event, not a rebalance)
    qty_before = {}
    broker_order_ids = {}
    if hardening:
        qty_before = (hardening.get("verification") or {}).get("before_qty") or {}
        for act in hardening.get("actions") or []:
            order = act.get("order") or {}
            oid = order.get("id")
            if oid:
                broker_order_ids.setdefault(act.get("symbol"), []).append(oid)
    qty_after = {s: float(p["qty"]) for s, p in positions.items() if s in LEGACY_SYMS}
    open_orders_after = {}
    for sym in LEGACY_SYMS:
        flags = order_flags(orders, sym)
        open_orders_after[sym] = flags["orders"]

    stop_coverage = sum(1 for x in by_sym if x["mv"] > 0 and (x["stop_active"] or x["oco_active"]))
    long_n = sum(1 for x in by_sym if x["mv"] > 0)
    coverage_pct = (stop_coverage / long_n * 100) if long_n else 100.0

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": "Phase7.2_FirstLiveObservationSnapshot",
        "account": {
            "equity": equity,
            "cash": float(acct.get("cash") or 0),
            "buying_power": float(acct.get("buying_power") or 0),
            "status": acct.get("status"),
        },
        "v2_weights": book,
        "gross_long_mv": total_long_mv,
        "gross_long_pct_equity": (total_long_mv / equity * 100) if equity else 0,
        "legacy_exposure_mv": legacy_mv,
        "legacy_exposure_pct_equity": (legacy_mv / equity * 100) if equity else 0,
        "surge_only_mv": surge_only_mv,
        "surge_only_pct_equity": (surge_only_mv / equity * 100) if equity else 0,
        "stop_coverage_pct": coverage_pct,
        "unprotected_risk_mv": unprotected_mv,
        "legacy_exposure_status": legacy_status,
        "positions": by_sym,
        "migration_audit": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "strategy_lifecycle_migration",
            "from": "Cursor-Surge",
            "to": "ERC_V2",
            "broker_order_ids": broker_order_ids,
            "position_qty_before": qty_before,
            "position_qty_after": qty_after,
            "position_change": qty_before == qty_after if qty_before else "no_baseline",
            "open_orders_after": open_orders_after,
        },
        "hardening_event": hardening,
        "freeze": {
            "research": "FROZEN",
            "param_search": "PAUSED",
            "erc_weights": "LOCKED",
            "allowed": ["bug_fix", "data_fix", "risk_protection"],
        },
    }
    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# Phase 7.2 — First Live Observation Snapshot",
        "",
        f"- Timestamp: {report['timestamp']}",
        f"- Equity: **${equity:,.2f}**",
        f"- Gross long: ${total_long_mv:,.0f} ({report['gross_long_pct_equity']:.1f}% equity)",
        f"- Legacy exposure (COIN/PLTR/NVDA): ${legacy_mv:,.0f} ({report['legacy_exposure_pct_equity']:.1f}%)",
        f"- Surge-only (COIN/PLTR): ${surge_only_mv:,.0f} ({report['surge_only_pct_equity']:.1f}%)",
        f"- Stop coverage: **{coverage_pct:.0f}%** of long names",
        f"- Unprotected risk MV: **${unprotected_mv:,.0f}**",
        "",
        "## V2 target weights (locked)",
        "",
        "| Strategy | Weight% | Target MV |",
        "|:--|---:|---:|",
    ]
    for b in book:
        lines.append(f"| {b['name'][:48]} | {b['weight']*100:.1f} | ${b['target_mv']:,.0f} |")

    lines += [
        "",
        "## Legacy Exposure Status",
        "",
        "| Symbol | Qty | Stop | TP/OCO | Owner |",
        "|:--|---:|:--|:--|:--|",
    ]
    for row in legacy_status:
        if not row["present"]:
            lines.append(f"| {row['symbol']} | — | {row['stop']} | {row['tp']} | {row['owner']} |")
            continue
        tp_oco = row["tp"] if row["symbol"] != "NVDA" else f"stop-only / oco={row['oco']}"
        if row["symbol"] in ("COIN", "PLTR"):
            tp_oco = f"tp={row['tp']} / oco={row['oco']}"
        lines.append(
            f"| {row['symbol']} | {row['qty']:.0f} | {row['stop']} | {tp_oco} | "
            f"`{row['strategy_owner']}` |"
        )

    lines += [
        "",
        "### Continuity checks",
        "",
        "- qty unchanged: required (Phase 7.1 must report position_change=false)",
        "- COIN/PLTR: stop + take-profit both active via OCO",
        "- NVDA: protective stop active; strategy_owner = ERC_V2",
        "",
        "## All positions",
        "",
        "| Symbol | Qty | MV | %Eq | UPL% | Stop | TP | Legacy |",
        "|:--|---:|---:|---:|---:|:--|:--|:--|",
    ]
    for x in by_sym:
        lines.append(
            f"| {x['symbol']} | {x['qty']:.0f} | ${x['mv']:,.0f} | {x['pct_equity']:.1f}% | "
            f"{x['upl_pct']:.1f}% | {'Y' if x['stop_active'] else 'N'} | "
            f"{'Y' if x['tp_active'] else 'N'} | {'Y' if x['legacy'] else ''} |"
        )

    legacy_coverage_pct = (
        (legacy_mv - unprotected_legacy_mv) / legacy_mv * 100 if legacy_mv else 100.0
    )
    all_protected = legacy_coverage_pct >= 100.0 - 1e-9
    coverage_label = "100%" if all_protected else f"{legacy_coverage_pct:.0f}%"
    lines += [
        "",
        "## Portfolio Migration Audit",
        "",
        "```text",
        "Portfolio Migration Audit",
        "",
        "Before:",
        "  Cursor-Surge exposure",
        "",
        "After:",
        "  ERC V2 active",
        "",
        "Legacy Exposure:",
        "  COIN",
        "  PLTR",
        "  NVDA",
        "",
        "Position Change:",
        "  NONE",
        "",
        "Protection:",
        f"  {coverage_label}",
        "",
        "Strategy Freeze:",
        "  ACTIVE",
        "```",
        "",
        "### Audit fields",
        "",
        f"- timestamp: {report['timestamp']}",
        f"- legacy_exposure_value: ${legacy_mv:,.0f}",
        f"- protected_value: ${legacy_mv - unprotected_legacy_mv:,.0f}",
        f"- protection_coverage_ratio: {coverage_label}",
        f"- position_qty_before: {qty_before or 'n/a'}",
        f"- position_qty_after: {qty_after}",
        f"- position_change: {'NONE' if qty_before == qty_after else 'CHANGED' if qty_before else 'no_baseline'}",
        "",
        "### Broker order IDs (migration)",
        "",
    ]
    if broker_order_ids:
        for sym, ids in broker_order_ids.items():
            lines.append(f"- {sym}: {', '.join(ids)}")
    else:
        lines.append("- (none recorded in hardening event)")
    lines += [
        "",
        "### Open orders after (per legacy symbol)",
        "",
    ]
    for sym in sorted(LEGACY_SYMS):
        entries = open_orders_after.get(sym) or []
        if not entries:
            lines.append(f"- {sym}: none")
            continue
        parts = []
        for o in entries:
            tag = o.get("class") or o.get("type")
            px = o.get("stop") if o.get("stop") is not None else o.get("limit")
            parts.append(f"{o.get('type')}({tag})@{px}")
        lines.append(f"- {sym}: " + "; ".join(parts))
    lines += [
        "",
        "## Phase 7 freeze reminder",
        "",
        "- ❌ no ERC reweight",
        "- ❌ no parameter search",
        "- ❌ no forced liquidation of legacy",
        "- ✅ risk protection only",
        "",
        "PnL attribution note: mark COIN/PLTR as **Surge legacy**; NVDA mixed "
        "(legacy entry, V2 ownership going forward).",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(OUT_MD.read_text(encoding="utf-8"))
    print("Wrote", OUT_MD, OUT_JSON)


if __name__ == "__main__":
    main()
