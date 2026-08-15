from __future__ import annotations

import pandas as pd


def ma_crossover_signals(
    close: pd.Series,
    fast: int = 20,
    slow: int = 60,
) -> pd.DataFrame:
    """Generate long-only MA crossover entries/exits."""
    if fast >= slow:
        raise ValueError("fast MA must be < slow MA")

    fast_ma = close.rolling(fast).mean()
    slow_ma = close.rolling(slow).mean()
    position = (fast_ma > slow_ma).astype(int)

    entries = (position == 1) & (position.shift(1).fillna(0) == 0)
    exits = (position == 0) & (position.shift(1).fillna(0) == 1)

    return pd.DataFrame(
        {
            "close": close,
            "fast_ma": fast_ma,
            "slow_ma": slow_ma,
            "position": position,
            "entries": entries,
            "exits": exits,
        }
    )


def latest_signal(df: pd.DataFrame) -> dict:
    """Return the latest actionable signal from a signal frame."""
    if df.empty:
        return {"signal": "hold", "reason": "no data"}

    row = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else row

    if bool(row["entries"]):
        signal = "buy"
    elif bool(row["exits"]):
        signal = "sell"
    elif int(row["position"]) == 1:
        signal = "hold_long"
    else:
        signal = "hold_cash"

    return {
        "signal": signal,
        "close": float(row["close"]),
        "fast_ma": float(row["fast_ma"]) if pd.notna(row["fast_ma"]) else None,
        "slow_ma": float(row["slow_ma"]) if pd.notna(row["slow_ma"]) else None,
        "position": int(row["position"]),
        "prev_position": int(prev["position"]),
    }
