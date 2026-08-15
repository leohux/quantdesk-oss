from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    data_provider: str = "auto"  # auto | alpaca | yfinance
    default_symbol: str = "AAPL"
    fast_ma: int = 20
    slow_ma: int = 60
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "*"
    quant_mode: str = "paper"  # paper | live_locked | live_armed

    # IBKR connectivity (server-side IB Gateway / TWS)
    ibkr_host: str = "ibkr-gateway"
    ibkr_port: int = 4002  # Paper gateway default; live is usually 4001
    ibkr_client_id: int = 101
    ibkr_account: str = ""
    ibkr_allowed_accounts: str = ""
    ibkr_trading_mode: str = "paper"  # paper | live
    ibkr_gateway_mode: str = "paper"  # paper | live | mock
    ibkr_read_only: bool = True
    ibkr_connect_timeout_sec: float = 8.0
    ibkr_reconnect_backoff_sec: float = 5.0
    ibkr_market_data_freshness_sec: int = 120

    # Live execution hard locks: all must be explicitly enabled to ever submit.
    live_trading_enabled: bool = False
    live_execution_armed: bool = False
    live_require_admin_role: bool = True
    live_arming_token: str = ""
    live_allowed_symbols: str = ""
    live_allowed_sides: str = "buy,sell"
    live_max_order_value_usd: float = 1_000.0
    live_max_position_pct: float = 5.0
    live_max_exposure_pct: float = 25.0
    live_max_daily_loss_pct: float = 2.0
    live_max_drawdown_pct: float = 8.0
    live_min_cash_pct: float = 25.0
    live_max_open_orders: int = 6
    live_only_reduce_existing: bool = True
    live_allow_short: bool = False
    live_allow_margin: bool = False
    live_allow_extended_hours: bool = False
    live_audit_log_path: str = "/app/data/store/live_audit.jsonl"
    live_oms_state_path: str = "/app/data/store/live_oms_state.json"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def has_alpaca_keys(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def ibkr_allowed_account_list(self) -> list[str]:
        return [
            x.strip().upper()
            for x in self.ibkr_allowed_accounts.split(",")
            if x.strip()
        ]

    @property
    def live_allowed_symbol_list(self) -> list[str]:
        return [
            x.strip().upper()
            for x in self.live_allowed_symbols.split(",")
            if x.strip()
        ]

    @property
    def live_allowed_side_list(self) -> list[str]:
        return [
            x.strip().lower()
            for x in self.live_allowed_sides.split(",")
            if x.strip()
        ]

    @property
    def live_submission_unlocked(self) -> bool:
        return bool(
            self.live_trading_enabled
            and self.live_execution_armed
            and self.ibkr_trading_mode.lower() == "live"
            and self.ibkr_gateway_mode.lower() == "live"
            and not self.ibkr_read_only
            and self.ibkr_account
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
