# Tech & growth US equities (broader than NDX100)
TECH_UNIVERSE = [
    # Mega-cap tech / AI
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "AVGO", "ORCL",
    "CRM", "ADBE", "NFLX", "CSCO", "IBM", "INTU", "TXN", "QCOM",
    # Semiconductors & hardware
    "AMD", "INTC", "MU", "AMAT", "LRCX", "KLAC", "ASML", "TSM", "ARM", "MRVL",
    "NXPI", "ON", "SMCI", "DELL", "HPQ", "SNDK", "WDC", "STX",
    # Software / cloud / cyber
    "NOW", "SNOW", "DDOG", "NET", "PANW", "CRWD", "ZS", "OKTA", "TEAM", "MDB",
    "PLTR", "S", "FTNT", "PATH", "U", "SHOP", "XYZ", "COIN", "HOOD",  # XYZ=Block (ex-SQ)
    # Internet / platforms / consumer tech
    "UBER", "ABNB", "SPOT", "RBLX", "SNAP", "PINS", "TTD", "APP", "ROKU", "EA",
    "TTWO", "ZM", "DOCU", "TWLO", "ESTC", "BILL", "HUBS",
    # China ADRs / other tech often in news
    "BABA", "BIDU", "JD", "PDD", "NIO", "XPEV", "LI",
    # AI infra / neocloud / high-beta growth (046bfa overlap + news-heavy)
    "CRWV", "NBIS", "VRT", "ANET", "HPE", "CRDO", "ALAB",
    "UPST", "AFRM", "SOFI", "SE", "DKNG", "SPCX",
]

# News wires still tag the old ticker; map to the listed symbol.
TICKER_ALIASES = {
    "SQ": "XYZ",  # Block, Inc. renamed ticker
}

TECH_SET = set(TECH_UNIVERSE) | set(TICKER_ALIASES)

# Foreign private issuers: material events via Form 6-K (not 8-K item codes).
FPI_TICKERS = frozenset(
    {
        "BABA",
        "BIDU",
        "JD",
        "PDD",
        "NIO",
        "XPEV",
        "LI",
        "ASML",
        "TSM",
        "ARM",
        "SE",
    }
)


def canonical_symbol(symbol: str) -> str:
    s = str(symbol or "").upper()
    return TICKER_ALIASES.get(s, s)
