from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from config.settings import get_settings

CACHE_DIR = Path(__file__).resolve().parent / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        raise ValueError(f"No data returned for {symbol}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    rename = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
        "timestamp": "Date",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns for {symbol}: {missing}")

    out = df[required].copy()
    out.index = pd.to_datetime(out.index)
    if out.index.tz is not None:
        out.index = out.index.tz_convert("UTC").tz_localize(None)
    out = out.sort_index()
    # Drop incomplete bars (developing session with NaN close poisons runners)
    out = out[out["Close"].notna() & (out["Close"].astype(float) > 0)]
    if out.empty:
        raise ValueError(f"No complete OHLCV bars for {symbol}")
    out["Symbol"] = symbol.upper()
    return out


def load_yfinance(symbol: str, start: str, end: str | None = None) -> pd.DataFrame:
    df = yf.download(
        symbol,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    return _normalize_ohlcv(df, symbol)


def load_alpaca(symbol: str, start: str, end: str | None = None) -> pd.DataFrame:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    settings = get_settings()
    if not settings.has_alpaca_keys:
        raise RuntimeError("Alpaca keys missing")

    client = StockHistoricalDataClient(
        settings.alpaca_api_key,
        settings.alpaca_secret_key,
    )
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = (
        datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
        if end
        else datetime.now(timezone.utc)
    )
    req = StockBarsRequest(
        symbol_or_symbols=symbol.upper(),
        timeframe=TimeFrame.Day,
        start=start_dt,
        end=end_dt,
    )
    bars = client.get_stock_bars(req).df
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol.upper(), level=0)
    return _normalize_ohlcv(bars, symbol)


_MEM_CACHE: dict[tuple, pd.DataFrame] = {}
_CACHE_TTL_HOURS = 20


def _cache_path(symbol: str, start: str, end: str | None) -> Path:
    end_key = end or "open"
    safe = f"{symbol.upper()}_{start}_{end_key}".replace(":", "-")
    return CACHE_DIR / f"ohlcv_{safe}.pkl"


def _read_disk_cache(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    age_h = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 3600.0
    if age_h > _CACHE_TTL_HOURS:
        return None
    try:
        df = pd.read_pickle(path)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
    except Exception:
        return None
    return None


def _write_disk_cache(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".pkl.tmp")
        df.to_pickle(tmp)
        tmp.replace(path)
    except Exception:
        pass


def load_ohlcv(
    symbol: str,
    start: str = "2018-01-01",
    end: str | None = None,
    provider: str | None = None,
    *,
    use_cache: bool = True,
) -> pd.DataFrame:
    symbol = symbol.upper()
    key = (symbol, start, end or "")
    if use_cache and key in _MEM_CACHE:
        return _MEM_CACHE[key].copy()

    disk = _cache_path(symbol, start, end)
    if use_cache:
        cached = _read_disk_cache(disk)
        if cached is not None:
            _MEM_CACHE[key] = cached
            return cached.copy()

    settings = get_settings()
    provider = (provider or settings.data_provider).lower()

    if provider == "yfinance":
        df = load_yfinance(symbol, start, end)
    elif provider == "alpaca":
        df = load_alpaca(symbol, start, end)
    elif settings.has_alpaca_keys:
        # auto: prefer Alpaca, fall back to yfinance
        try:
            df = load_alpaca(symbol, start, end)
        except Exception:
            df = load_yfinance(symbol, start, end)
    else:
        df = load_yfinance(symbol, start, end)

    if use_cache:
        _write_disk_cache(disk, df)
        _MEM_CACHE[key] = df
    return df.copy()


def to_polars(df: pd.DataFrame):
    import polars as pl

    tmp = df.reset_index(names="Date")
    return pl.from_pandas(tmp)


# === intraday helpers ===
def load_intraday(
    symbol: str,
    start: str,
    end: str | None = None,
    timeframe: str = "1Min",
    feed: str = "iex",
) -> pd.DataFrame:
    """Load intraday minute/hour bars from Alpaca (IEX feed by default).

    timeframe: '1Min' | '5Min' | '15Min' | '1Hour'
    Returns OHLCV DataFrame indexed by UTC-naive timestamps.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    settings = get_settings()
    if not settings.has_alpaca_keys:
        raise RuntimeError("Alpaca keys missing")

    tf_map = {
        "1min": TimeFrame(1, TimeFrameUnit.Minute),
        "5min": TimeFrame(5, TimeFrameUnit.Minute),
        "15min": TimeFrame(15, TimeFrameUnit.Minute),
        "1hour": TimeFrame(1, TimeFrameUnit.Hour),
    }
    tf = tf_map.get(timeframe.lower(), TimeFrame(1, TimeFrameUnit.Minute))

    client = StockHistoricalDataClient(
        settings.alpaca_api_key, settings.alpaca_secret_key
    )
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = (
        datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
        if end
        else datetime.now(timezone.utc)
    )
    req = StockBarsRequest(
        symbol_or_symbols=symbol.upper(),
        timeframe=tf,
        start=start_dt,
        end=end_dt,
        feed=feed,
    )
    bars = client.get_stock_bars(req).df
    if bars is None or bars.empty:
        raise ValueError(f"No intraday data for {symbol}")
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol.upper(), level=0)
    return _normalize_ohlcv(bars, symbol)


def _alpaca_data_client():
    from alpaca.data.historical import StockHistoricalDataClient

    settings = get_settings()
    return StockHistoricalDataClient(
        settings.alpaca_api_key, settings.alpaca_secret_key
    )


def _retry_data(fn, *, retries: int = 4, base_sleep: float = 0.6):
    import time

    last = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e).lower()
            if not any(
                x in msg
                for x in ("too many requests", "rate limit", "429", "timeout", "500")
            ):
                raise
            if i == retries - 1:
                raise
            time.sleep(base_sleep * (2**i))
    raise last  # pragma: no cover


def get_latest_prices(symbols: list[str], feed: str = "iex") -> dict[str, float]:
    """Batch latest trade prices (one request for many symbols)."""
    from alpaca.data.requests import StockLatestTradeRequest

    syms = [str(s).upper() for s in symbols if s]
    if not syms:
        return {}
    client = _alpaca_data_client()
    req = StockLatestTradeRequest(symbol_or_symbols=syms, feed=feed)
    trades = _retry_data(lambda: client.get_stock_latest_trade(req))
    out: dict[str, float] = {}
    for sym in syms:
        t = trades.get(sym) if isinstance(trades, dict) else None
        if t is not None and getattr(t, "price", None) is not None:
            out[sym] = float(t.price)
    return out


def get_latest_price(symbol: str, feed: str = "iex") -> float:
    """Real-time last trade price (IEX feed)."""
    prices = get_latest_prices([symbol], feed=feed)
    if symbol.upper() not in prices:
        raise ValueError(f"No latest trade for {symbol}")
    return prices[symbol.upper()]


_PRIOR_CLOSE_CACHE: dict[str, tuple[float, float]] = {}  # symbol -> (ts, price)


def get_prior_close(symbol: str) -> float:
    """Previous completed session's close (used as surge reference).

    If today's developing daily bar is already in the frame, use iloc[-2].
    Cached ~1h to avoid hammering daily OHLCV on every intraday poll.
    """
    import time

    sym = symbol.upper()
    now = time.time()
    hit = _PRIOR_CLOSE_CACHE.get(sym)
    if hit and now - hit[0] < 3600:
        return hit[1]

    df = load_ohlcv(
        sym,
        start=(datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d"),
    )
    closes = df["Close"].astype(float).dropna()
    if closes.empty:
        raise ValueError(f"No prior close for {sym}")
    today = datetime.now(timezone.utc).date()
    last_date = closes.index[-1].date() if hasattr(closes.index[-1], "date") else closes.index[-1]
    if last_date == today and len(closes) >= 2:
        px = float(closes.iloc[-2])
    else:
        px = float(closes.iloc[-1])
    if px != px or px <= 0:  # NaN guard
        raise ValueError(f"Bad prior close for {sym}: {px}")
    _PRIOR_CLOSE_CACHE[sym] = (now, px)
    return px
