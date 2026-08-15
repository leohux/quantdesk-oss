"""
Pydantic models for QuantDesk configuration.

All config sections are validated at load time with sensible defaults.
Import paths assume /app is in sys.path.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """Database connection configuration."""

    postgres_url: str = Field(
        default="postgresql://quantdesk:quantdesk@localhost:5432/quantdesk",
        description="PostgreSQL connection URL",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL",
    )
    pool_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Connection pool size",
    )


class TradingConfig(BaseModel):
    """Trading parameters configuration."""

    symbols: List[str] = Field(
        default=["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"],
        description="List of symbols to trade",
    )
    sizing_pct: float = Field(
        default=0.02,
        gt=0,
        le=1.0,
        description="Position sizing as fraction of portfolio",
    )
    stop_loss_pct: float = Field(
        default=0.05,
        gt=0,
        le=1.0,
        description="Stop loss percentage",
    )
    max_position_pct: float = Field(
        default=0.10,
        gt=0,
        le=1.0,
        description="Maximum single position as fraction of portfolio",
    )
    max_daily_loss_pct: float = Field(
        default=0.03,
        gt=0,
        le=1.0,
        description="Maximum daily loss before halting",
    )
    max_drawdown_pct: float = Field(
        default=0.15,
        gt=0,
        le=1.0,
        description="Maximum drawdown before halting",
    )


class SchedulerConfig(BaseModel):
    """Trading schedule configuration."""

    market_open: str = Field(
        default="09:30",
        description="Market open time (HH:MM)",
    )
    market_close: str = Field(
        default="16:00",
        description="Market close time (HH:MM)",
    )
    timezone: str = Field(
        default="America/New_York",
        description="Timezone for market hours",
    )
    health_check_interval: int = Field(
        default=60,
        ge=10,
        description="Health check interval in seconds",
    )


class NotificationConfig(BaseModel):
    """Notification service configuration."""

    telegram_enabled: bool = Field(
        default=False,
        description="Enable Telegram notifications",
    )
    telegram_bot_token: Optional[str] = Field(
        default=None,
        description="Telegram bot API token",
    )
    telegram_chat_id: Optional[str] = Field(
        default=None,
        description="Telegram chat ID for messages",
    )


class MonitoringConfig(BaseModel):
    """Monitoring and metrics configuration."""

    prometheus_enabled: bool = Field(
        default=True,
        description="Enable Prometheus metrics endpoint",
    )
    prometheus_port: int = Field(
        default=9090,
        ge=1024,
        le=65535,
        description="Prometheus metrics port",
    )


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field(
        default="INFO",
        description="Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        description="Log message format",
    )
    log_dir: str = Field(
        default="/app/logs",
        description="Directory for log files",
    )
    max_size_mb: int = Field(
        default=100,
        ge=1,
        description="Max log file size in MB before rotation",
    )
    backup_count: int = Field(
        default=5,
        ge=1,
        description="Number of rotated log files to keep",
    )


class AppConfig(BaseModel):
    """Top-level application configuration aggregating all sections."""

    environment: str = Field(
        default="development",
        description="Current environment profile",
    )
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    def as_dict(self) -> dict:
        """Return config as a plain dictionary (for serialization)."""
        return self.model_dump()

    def as_dict_redacted(self) -> dict:
        """Return config as dict with sensitive fields redacted."""
        data = self.model_dump()
        if data["notifications"].get("telegram_bot_token"):
            data["notifications"]["telegram_bot_token"] = "***REDACTED***"
        return data
