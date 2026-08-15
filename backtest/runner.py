from __future__ import annotations

from typing import Any

import pandas as pd

from strategies.engine import run_signal_fn


def run_signal_backtest(
    ohlcv: pd.DataFrame,
    code: str,
    params: dict[str, Any] | None = None,
    init_cash: float = 100_000.0,
    fees: float = 0.001,
) -> dict[str, Any]:
    close = ohlcv["Close"].astype(float)
    high = ohlcv["High"].astype(float) if "High" in ohlcv.columns else None
    low = ohlcv["Low"].astype(float) if "Low" in ohlcv.columns else None
    open_ = ohlcv["Open"].astype(float) if "Open" in ohlcv.columns else None
    volume = ohlcv["Volume"].astype(float) if "Volume" in ohlcv.columns else None

    # Inject OHLC/Volume for intraday stops + false-breakout filters.
    run_params = dict(params or {})
    if high is not None:
        run_params["_high"] = high
    if low is not None:
        run_params["_low"] = low
    if open_ is not None:
        run_params["_open"] = open_
    if volume is not None:
        run_params["_volume"] = volume

    entries, exits = run_signal_fn(close, code, run_params)
    try:
        return _vectorbt(
            close,
            entries,
            exits,
            init_cash,
            fees,
            run_params,
            open_=open_,
            high=high,
            low=low,
        )
    except Exception:
        return _pandas(
            close,
            entries,
            exits,
            init_cash,
            fees,
            run_params,
            open_=open_,
            high=high,
            low=low,
        )


def enrich_chart_metrics(equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
    if not equity_curve:
        return {"drawdown_curve": [], "annual_returns": [], "monthly_returns": []}

    eq = pd.Series(
        {pd.Timestamp(p["date"]): float(p["equity"]) for p in equity_curve}
    ).sort_index()
    peak = eq.cummax()
    dd = (eq - peak) / peak
    drawdown_curve = [
        {"date": d.strftime("%Y-%m-%d"), "drawdown_pct": float(v * 100)}
        for d, v in dd.items()
    ]
    yearly = eq.resample("YE").last().pct_change().dropna()
    annual_returns = [
        {"year": int(d.year), "return_pct": float(v * 100)} for d, v in yearly.items()
    ]
    monthly = eq.resample("ME").last().pct_change().dropna()
    monthly_returns = [
        {"year": int(d.year), "month": int(d.month), "return_pct": float(v * 100)}
        for d, v in monthly.items()
    ]
    return {
        "drawdown_curve": drawdown_curve[-365:],
        "annual_returns": annual_returns,
        "monthly_returns": monthly_returns[-36:],
    }


def run_ma_backtest(
    ohlcv: pd.DataFrame,
    fast: int = 20,
    slow: int = 60,
    init_cash: float = 100_000.0,
    fees: float = 0.001,
) -> dict[str, Any]:
    from strategies.engine import TEMPLATES

    return run_signal_backtest(
        ohlcv,
        TEMPLATES["ma_cross"],
        {"fast": fast, "slow": slow},
        init_cash=init_cash,
        fees=fees,
    )


def _stop_fill_price(
    *,
    entry_price: float,
    close_px: float,
    open_px: float | None,
    high_px: float | None,
    low_px: float | None,
    stop_loss: float | None,
    take_profit: float | None,
) -> float:
    """Prefer stop/TP fill using High/Low; gap-through uses Open."""
    fill = close_px
    hit_sl = False
    hit_tp = False
    sl_px = None
    tp_px = None

    if entry_price > 0 and stop_loss is not None and low_px is not None:
        sl_px = entry_price * (1.0 + float(stop_loss))
        if low_px <= sl_px:
            hit_sl = True
    if entry_price > 0 and take_profit is not None and high_px is not None:
        tp_px = entry_price * (1.0 + float(take_profit))
        if high_px >= tp_px:
            hit_tp = True

    # Same-day both hit: conservative — assume stop first.
    if hit_sl and sl_px is not None:
        if open_px is not None and open_px < sl_px:
            return float(open_px)
        return float(sl_px)
    if hit_tp and tp_px is not None:
        if open_px is not None and open_px > tp_px:
            return float(open_px)
        return float(tp_px)
    return float(fill)


def _vectorbt(
    close,
    entries,
    exits,
    init_cash,
    fees,
    params,
    open_=None,
    high=None,
    low=None,
):
    import vectorbt as vbt

    kwargs: dict[str, Any] = {}
    if open_ is not None:
        kwargs["open"] = open_
    if high is not None:
        kwargs["high"] = high
    if low is not None:
        kwargs["low"] = low

    # Engine-level intraday stops (High/Low). Skip when strategy comparison
    # requests close-only exits via params["_disable_engine_stops"]=True.
    if not params.get("_disable_engine_stops"):
        if params.get("stop_loss") is not None and low is not None:
            kwargs["sl_stop"] = abs(float(params["stop_loss"]))
        if params.get("take_profit") is not None and high is not None:
            kwargs["tp_stop"] = abs(float(params["take_profit"]))

    pf = vbt.Portfolio.from_signals(
        close,
        entries=entries,
        exits=exits,
        init_cash=init_cash,
        fees=fees,
        freq="1D",
        **kwargs,
    )
    stats = pf.stats()
    equity = pf.value()
    equity_curve = [
        {"date": d.strftime("%Y-%m-%d"), "equity": float(v)} for d, v in equity.items()
    ]
    charts = enrich_chart_metrics(equity_curve)
    return {
        "engine": "vectorbt",
        "params": {k: v for k, v in params.items() if not str(k).startswith("_")},
        "init_cash": init_cash,
        "total_return": float(stats.get("Total Return [%]", 0.0)) / 100.0,
        "total_return_pct": float(stats.get("Total Return [%]", 0.0)),
        "max_drawdown_pct": float(stats.get("Max Drawdown [%]", 0.0)),
        "sharpe": _sf(stats.get("Sharpe Ratio")),
        "sortino": _sf(stats.get("Sortino Ratio")),
        "calmar": _sf(stats.get("Calmar Ratio")),
        "win_rate_pct": _sf(stats.get("Win Rate [%]")),
        "profit_factor": _sf(stats.get("Profit Factor")),
        "trades": int(stats.get("Total Trades", 0) or 0),
        "end_value": float(equity.iloc[-1]),
        "equity_curve": equity_curve[-365:],
        "buy_hold_return_pct": float((close.iloc[-1] / close.iloc[0] - 1) * 100),
        "intraday_stops": bool(kwargs.get("sl_stop") or kwargs.get("tp_stop")),
        **charts,
    }


def _pandas(
    close,
    entries,
    exits,
    init_cash,
    fees,
    params,
    open_=None,
    high=None,
    low=None,
):
    cash = init_cash
    shares = 0.0
    equity_curve = []
    trades = 0
    wins = 0
    entry_price = None
    stop_loss = params.get("stop_loss")
    take_profit = params.get("take_profit")
    try:
        stop_loss = float(stop_loss) if stop_loss is not None else None
    except (TypeError, ValueError):
        stop_loss = None
    try:
        take_profit = float(take_profit) if take_profit is not None else None
    except (TypeError, ValueError):
        take_profit = None

    for dt in close.index:
        price = float(close.loc[dt])
        open_px = float(open_.loc[dt]) if open_ is not None else None
        high_px = float(high.loc[dt]) if high is not None else None
        low_px = float(low.loc[dt]) if low is not None else None

        # Path-dependent intraday stop/TP while holding (even if strategy
        # forgot to mark exit). Gap-through fills at Open.
        force_exit = False
        if (
            shares > 0
            and entry_price
            and not params.get("_disable_engine_stops")
        ):
            if stop_loss is not None and low_px is not None:
                sl_px = float(entry_price) * (1.0 + stop_loss)
                if (open_px is not None and open_px <= sl_px) or low_px <= sl_px:
                    force_exit = True
            if take_profit is not None and high_px is not None and not force_exit:
                tp_px = float(entry_price) * (1.0 + take_profit)
                if (open_px is not None and open_px >= tp_px) or high_px >= tp_px:
                    force_exit = True

        if bool(entries.loc[dt]) and shares == 0:
            shares = (cash * (1 - fees)) / price
            cash = 0.0
            entry_price = price
            trades += 1
        elif (bool(exits.loc[dt]) or force_exit) and shares > 0:
            fill = _stop_fill_price(
                entry_price=float(entry_price or price),
                close_px=price,
                open_px=open_px,
                high_px=high_px,
                low_px=low_px,
                stop_loss=None if params.get("_disable_engine_stops") else stop_loss,
                take_profit=None if params.get("_disable_engine_stops") else take_profit,
            )
            cash = shares * fill * (1 - fees)
            if entry_price and fill > entry_price:
                wins += 1
            shares = 0.0
            entry_price = None
        equity = cash + shares * price
        equity_curve.append(
            {"date": pd.Timestamp(dt).strftime("%Y-%m-%d"), "equity": equity}
        )

    end_value = equity_curve[-1]["equity"] if equity_curve else init_cash
    total_return = end_value / init_cash - 1
    eq = pd.Series([p["equity"] for p in equity_curve])
    peak = eq.cummax()
    dd = ((eq - peak) / peak).min() if len(eq) else 0.0
    rets = eq.pct_change().dropna()
    sharpe = (
        float((rets.mean() / rets.std()) * (252**0.5))
        if len(rets) > 2 and rets.std() > 0
        else None
    )
    charts = enrich_chart_metrics(equity_curve)
    return {
        "engine": "pandas",
        "params": {k: v for k, v in params.items() if not str(k).startswith("_")},
        "init_cash": init_cash,
        "total_return": float(total_return),
        "total_return_pct": float(total_return * 100),
        "max_drawdown_pct": float(abs(dd) * 100),
        "sharpe": sharpe,
        "sortino": None,
        "calmar": None,
        "win_rate_pct": float(wins / trades * 100) if trades else None,
        "profit_factor": None,
        "trades": trades,
        "end_value": float(end_value),
        "equity_curve": equity_curve[-365:],
        "buy_hold_return_pct": float((close.iloc[-1] / close.iloc[0] - 1) * 100),
        "intraday_stops": stop_loss is not None or take_profit is not None,
        **charts,
    }


def _sf(value):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return float(value)
    except Exception:
        return None
