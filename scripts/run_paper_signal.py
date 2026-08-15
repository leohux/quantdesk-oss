"""CLI: read latest MA signal and optionally place Paper order."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.loader import load_ohlcv
from execution.alpaca_client import AlpacaPaperClient
from strategies.ma_cross import latest_signal, ma_crossover_signals


def main() -> None:
    parser = argparse.ArgumentParser(description="MA signal -> Alpaca Paper")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--fast", type=int, default=20)
    parser.add_argument("--slow", type=int, default=60)
    parser.add_argument("--qty", type=float, default=1)
    parser.add_argument("--execute", action="store_true", help="Actually place paper order")
    args = parser.parse_args()

    ohlcv = load_ohlcv(args.symbol, start="2020-01-01")
    frame = ma_crossover_signals(ohlcv["Close"], fast=args.fast, slow=args.slow)
    sig = latest_signal(frame)
    sig["symbol"] = args.symbol.upper()
    print(json.dumps(sig, indent=2, ensure_ascii=False))

    if not args.execute:
        print("\nDry-run only. Add --execute to place a Paper market order.")
        return

    client = AlpacaPaperClient()
    if sig["signal"] == "buy":
        order = client.market_order(args.symbol, args.qty, "buy")
        print(json.dumps(order, indent=2))
    elif sig["signal"] == "sell":
        order = client.market_order(args.symbol, args.qty, "sell")
        print(json.dumps(order, indent=2))
    else:
        print(f"No actionable order for signal={sig['signal']}")


if __name__ == "__main__":
    main()
