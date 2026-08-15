"""CLI: run MA crossover backtest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.runner import run_ma_backtest
from data.loader import load_ohlcv


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MA crossover backtest")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--fast", type=int, default=20)
    parser.add_argument("--slow", type=int, default=60)
    args = parser.parse_args()

    ohlcv = load_ohlcv(args.symbol, start=args.start, end=args.end)
    result = run_ma_backtest(ohlcv, fast=args.fast, slow=args.slow)
    result["symbol"] = args.symbol.upper()
    # Keep CLI output compact
    slim = {k: v for k, v in result.items() if k != "equity_curve"}
    print(json.dumps(slim, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
