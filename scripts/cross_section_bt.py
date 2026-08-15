# -*- coding: utf-8 -*-
"""JoinQuant-style cross-sectional strategies backtest (pandas + yfinance).

熊猫量化 / 聚宽公开策略思路，适配到本项目的美股数据域。
所有因子延迟一个交易日执行，避免当日收盘因子按同一收盘价成交。

Usage:
  .venv\\Scripts\\python.exe scripts/cross_section_bt.py
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd
import yfinance as yf

# Liquid US names ~ 聚宽股票池 / ETF 轮动池的美股近似
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
    "NFLX", "AVGO", "COST", "JPM", "XOM", "UNH", "LLY", "V",
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "GLD",
]

START = "2021-01-01"
END = None
INIT_CASH = 100_000.0
FEE = 0.0005  # one-way, mirrors ALPHA_MINER_FEES
REBALANCE = 5  # trading days between rebalances
TOP_K = 5
LOOKBACK = 20


@dataclass
class Metrics:
    name: str
    total_return: float
    ann_return: float
    sharpe: float
    max_dd: float
    turnover: float
    n_rebalances: int
    final_equity: float


def load_close_panel(symbols: list[str], start: str, end: str | None) -> pd.DataFrame:
    """Download per-symbol (more resilient than bulk when Yahoo flakes)."""
    import time

    series_list: list[pd.Series] = []
    for sym in symbols:
        got = False
        for attempt in range(3):
            try:
                df = yf.download(
                    sym,
                    start=start,
                    end=end,
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
                if df is None or df.empty:
                    raise ValueError("empty")
                if isinstance(df.columns, pd.MultiIndex):
                    # (field, ticker) layout
                    if ("Close", sym) in df.columns:
                        s = df[("Close", sym)]
                    elif "Close" in df.columns.get_level_values(0):
                        s = df["Close"].iloc[:, 0]
                    else:
                        raise ValueError(f"no Close in {df.columns}")
                else:
                    s = df["Close"]
                s = pd.Series(pd.to_numeric(s, errors="coerce"), index=pd.to_datetime(s.index), name=sym)
                s = s[~s.index.duplicated(keep="last")].sort_index()
                if len(s) < LOOKBACK + 40:
                    raise ValueError(f"too short: {len(s)}")
                series_list.append(s)
                print(f"  {sym}: {len(s)} bars")
                got = True
                break
            except Exception as exc:
                print(f"  {sym} attempt {attempt+1}: {exc}")
            time.sleep(1.2 * (attempt + 1))
        if not got:
            print(f"  {sym}: SKIPPED")
    if not series_list:
        raise RuntimeError("no symbols loaded")
    panel = pd.concat(series_list, axis=1).sort_index().dropna(how="all").ffill(limit=3)
    return panel


def factor_momentum(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    return close / close.shift(lookback) - 1.0


def factor_reversal(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    return -(close / close.shift(lookback) - 1.0)


def factor_vol_adj_mom(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    ret = close.pct_change()
    mom = close / close.shift(lookback) - 1.0
    vol = ret.rolling(lookback).std()
    return mom / vol.replace(0, np.nan)


def factor_low_vol(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """聚宽低波动：优先持有历史波动率较低的资产。"""
    return -close.pct_change().rolling(lookback).std()


def factor_multi_horizon_mom(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """熊猫/ETF轮动：20、60、120日动量加权。"""
    r20 = close / close.shift(20) - 1.0
    r60 = close / close.shift(60) - 1.0
    r120 = close / close.shift(120) - 1.0
    return 0.5 * r20 + 0.3 * r60 + 0.2 * r120


def factor_skip5_mom(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """跳过最近5日，降低短期反转对中期动量的污染。"""
    r20 = close.shift(5) / close.shift(25) - 1.0
    r60 = close.shift(5) / close.shift(65) - 1.0
    r120 = close.shift(5) / close.shift(125) - 1.0
    return 0.2 * r20 + 0.3 * r60 + 0.5 * r120


def factor_mom_short_reversal(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """熊猫多因子：中期动量 + 短期反转。"""
    mom20 = close / close.shift(20) - 1.0
    rev5 = -(close / close.shift(5) - 1.0)
    return 0.7 * mom20 + 0.3 * rev5


def factor_downside_adj_mom(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """动量除以下行波动，只惩罚负收益风险。"""
    ret = close.pct_change()
    downside = ret.clip(upper=0).pow(2).rolling(lookback).mean().pow(0.5)
    mom = close / close.shift(lookback) - 1.0
    return mom / downside.replace(0, np.nan)


def factor_high_proximity(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """价格接近120日高点，近似52周高点效应。"""
    return close / close.rolling(120).max() - 1.0


def factor_trend_strength(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """均线趋势强度：价格相对60日均线的位置。"""
    return close / close.rolling(60).mean() - 1.0


def _cs_rank(frame: pd.DataFrame, ascending: bool = True) -> pd.DataFrame:
    return frame.rank(axis=1, pct=True, ascending=ascending)


def factor_rank_composite(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """熊猫量化风格：动量、低波、趋势质量的截面排名合成。"""
    ret = close.pct_change()
    mom = close / close.shift(60) - 1.0
    vol = ret.rolling(20).std()
    trend = close / close.rolling(60).mean() - 1.0
    return 0.45 * _cs_rank(mom) + 0.30 * _cs_rank(vol, ascending=False) + 0.25 * _cs_rank(trend)


def factor_slope_rsq(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """JoinQuant-style: annualized log-price slope * R^2."""
    logp = np.log(close.astype(float))
    x = np.arange(lookback, dtype=float)
    x_mean = x.mean()
    ss_xx = ((x - x_mean) ** 2).sum()

    def _score(window: np.ndarray) -> float:
        if np.any(~np.isfinite(window)):
            return np.nan
        y = window
        y_mean = y.mean()
        slope = ((x - x_mean) * (y - y_mean)).sum() / ss_xx
        y_hat = slope * x + (y_mean - slope * x_mean)
        ss_res = ((y - y_hat) ** 2).sum()
        ss_tot = ((y - y_mean) ** 2).sum()
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        ann = math.exp(slope * 252) - 1.0
        return float(ann * r2)

    out = {}
    for col in logp.columns:
        out[col] = logp[col].rolling(lookback).apply(_score, raw=True)
    return pd.DataFrame(out, index=close.index)


def pick_top(scores: pd.Series, k: int) -> list[str]:
    s = scores.dropna()
    if s.empty:
        return []
    return list(s.nlargest(min(k, len(s))).index)


def run_cs_backtest(
    name: str,
    close: pd.DataFrame,
    factor_fn: Callable[[pd.DataFrame, int], pd.DataFrame],
    lookback: int = LOOKBACK,
    top_k: int = TOP_K,
    rebalance: int = REBALANCE,
    fee: float = FEE,
    init_cash: float = INIT_CASH,
    eval_start: str | None = None,
    eval_end: str | None = None,
    eligible: pd.DataFrame | None = None,
    exposure: pd.Series | None = None,
    max_weight: float | None = None,
) -> tuple[Metrics, pd.Series]:
    # Yesterday's completed factor is traded today; never trade on the same
    # close used to calculate the signal.
    scores = factor_fn(close, lookback).shift(1)
    if eligible is not None:
        # Point-in-time membership / liquidity mask (True = tradable that day).
        mask = eligible.reindex(index=scores.index, columns=scores.columns).fillna(False)
        scores = scores.where(mask)
    dates = close.index[lookback:]
    if eval_start:
        dates = dates[dates >= pd.Timestamp(eval_start)]
    if eval_end:
        dates = dates[dates <= pd.Timestamp(eval_end)]
    if len(dates) < 2:
        raise ValueError(f"insufficient evaluation dates: {eval_start} .. {eval_end}")
    if exposure is not None:
        exposure = exposure.reindex(dates).ffill().fillna(1.0).clip(0.0, 1.0)
    equity = init_cash
    equity_curve = []
    holdings: dict[str, float] = {}  # symbol -> shares
    cash = init_cash
    turnover_notional = 0.0
    n_reb = 0
    next_reb_i = 0

    for i, dt in enumerate(dates):
        px = close.loc[dt]
        # mark-to-market
        pos_val = sum(holdings.get(s, 0.0) * float(px[s]) for s in holdings if pd.notna(px.get(s)))
        equity = cash + pos_val
        equity_curve.append((dt, equity))

        if i < next_reb_i:
            continue
        next_reb_i = i + rebalance
        n_reb += 1

        exp = float(exposure.loc[dt]) if exposure is not None else 1.0
        target = pick_top(scores.loc[dt], top_k) if exp > 1e-9 else []
        if not target:
            for s in list(holdings.keys()):
                if holdings[s] != 0 and pd.notna(px.get(s)):
                    proceeds = holdings[s] * float(px[s])
                    cash += proceeds * (1.0 - fee)
                    turnover_notional += abs(proceeds)
                    holdings[s] = 0.0
            holdings = {}
            continue

        # Equal weight, optionally capped by max_weight (idle cash if caps bind).
        raw_w = 1.0 / len(target)
        if max_weight is not None and max_weight > 0:
            w = min(raw_w, float(max_weight))
        else:
            w = raw_w
        target_w = {s: w for s in target}
        investable = equity * exp
        target_notional = {s: investable * tw for s, tw in target_w.items()}

        # sell names not in target
        for s in list(holdings.keys()):
            if s not in target_w and holdings[s] != 0 and pd.notna(px.get(s)):
                proceeds = holdings[s] * float(px[s])
                cash += proceeds * (1.0 - fee)
                turnover_notional += abs(proceeds)
                holdings[s] = 0.0

        # rebalance target names
        for s in target:
            if not pd.notna(px.get(s)) or float(px[s]) <= 0:
                continue
            cur_shares = holdings.get(s, 0.0)
            cur_notional = cur_shares * float(px[s])
            delta = target_notional[s] - cur_notional
            if abs(delta) < 1.0:
                continue
            shares_delta = delta / float(px[s])
            cost = abs(delta) * fee
            cash -= delta + cost
            holdings[s] = cur_shares + shares_delta
            turnover_notional += abs(delta)

        # drop zero holdings
        holdings = {s: sh for s, sh in holdings.items() if abs(sh) > 1e-9}

    eq = pd.Series({d: e for d, e in equity_curve}).sort_index()
    rets = eq.pct_change().dropna()
    total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0) if len(eq) > 1 else 0.0
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    ann = (1.0 + total_ret) ** (1.0 / years) - 1.0 if total_ret > -1 else -1.0
    vol = float(rets.std() * math.sqrt(252)) if len(rets) else 0.0
    sharpe = float(rets.mean() * 252 / vol) if vol > 1e-12 else 0.0
    peak = eq.cummax()
    dd = float(((eq - peak) / peak).min()) if len(eq) else 0.0
    avg_equity = float(eq.mean()) if len(eq) else init_cash
    turnover = turnover_notional / (avg_equity * max(n_reb, 1))

    return (
        Metrics(
            name=name,
            total_return=total_ret,
            ann_return=float(ann),
            sharpe=sharpe,
            max_dd=dd,
            turnover=float(turnover),
            n_rebalances=n_reb,
            final_equity=float(eq.iloc[-1]),
        ),
        eq,
    )


def buy_hold_equal(close: pd.DataFrame, init_cash: float = INIT_CASH) -> Metrics:
    """Equal-weight buy & hold benchmark on universe."""
    rets = close.pct_change().dropna(how="all")
    port = rets.mean(axis=1).fillna(0.0)
    eq = (1.0 + port).cumprod() * init_cash
    total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    ann = (1.0 + total_ret) ** (1.0 / years) - 1.0
    vol = float(port.std() * math.sqrt(252))
    sharpe = float(port.mean() * 252 / vol) if vol > 1e-12 else 0.0
    peak = eq.cummax()
    dd = float(((eq - peak) / peak).min())
    return Metrics(
        name="buy_hold_equal",
        total_return=total_ret,
        ann_return=float(ann),
        sharpe=sharpe,
        max_dd=dd,
        turnover=0.0,
        n_rebalances=0,
        final_equity=float(eq.iloc[-1]),
    )


def main() -> None:
    print(f"Loading panel {START} .. universe={len(UNIVERSE)}")
    close = load_close_panel(UNIVERSE, START, END)
    print(f"Panel shape={close.shape} cols={list(close.columns)}")

    strategies = [
        ("jq_momentum_20d", factor_momentum, 20),
        ("jq_reversal_20d", factor_reversal, 20),
        ("jq_low_vol_20d", factor_low_vol, 20),
        ("jq_vol_adj_mom_20d", factor_vol_adj_mom, 20),
        ("jq_slope_rsq_20d", factor_slope_rsq, 20),
        ("panda_mom_plus_rev", factor_mom_short_reversal, 20),
        ("panda_rank_composite", factor_rank_composite, 60),
        ("rotation_multi_horizon", factor_multi_horizon_mom, 120),
        ("rotation_skip5_mom", factor_skip5_mom, 125),
        ("downside_adj_mom_20d", factor_downside_adj_mom, 20),
        ("high_proximity_120d", factor_high_proximity, 120),
        ("ma_trend_strength_60d", factor_trend_strength, 60),
    ]

    rows = []
    bh = buy_hold_equal(close)
    rows.append(bh)

    for name, fn, lookback in strategies:
        print(f"Running {name} ...")
        m, _ = run_cs_backtest(name, close, fn, lookback=lookback)
        rows.append(m)
        print(
            f"  ret={m.total_return:.2%} ann={m.ann_return:.2%} "
            f"sharpe={m.sharpe:.2f} maxDD={m.max_dd:.2%} "
            f"turn/reb={m.turnover:.2f} equity={m.final_equity:,.0f}"
        )

    print("\n=== Summary ===")
    hdr = f"{'strategy':<26} {'ret':>8} {'ann':>8} {'sharpe':>8} {'maxDD':>8} {'turn':>6}"
    print(hdr)
    print("-" * len(hdr))
    for m in sorted(rows, key=lambda x: x.sharpe, reverse=True):
        print(
            f"{m.name:<26} {m.total_return:8.1%} {m.ann_return:8.1%} "
            f"{m.sharpe:8.2f} {m.max_dd:8.1%} {m.turnover:6.2f}"
        )


if __name__ == "__main__":
    main()
