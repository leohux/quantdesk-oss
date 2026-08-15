#!/usr/bin/env python3
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from execution.alpaca_client import AlpacaPaperClient

c = AlpacaPaperClient()
orders = c.client.get_orders(
    filter=GetOrdersRequest(
        status=QueryOrderStatus.OPEN,
        limit=200,
        nested=True,
        symbols=["COIN", "PLTR", "NVDA"],
    )
)
for o in orders:
    print(
        "ROOT", o.symbol, str(o.id), o.side, o.type, o.order_class, o.status,
        "qty", o.qty, "limit", o.limit_price, "stop", o.stop_price,
    )
    for leg in getattr(o, "legs", None) or []:
        print(
            "  LEG", leg.symbol, str(leg.id), leg.side, leg.type,
            leg.order_class, leg.status, "qty", leg.qty,
            "limit", leg.limit_price, "stop", leg.stop_price,
        )
