"""QuantDesk core configuration package."""

from __future__ import annotations

from typing import Any

__all__ = [
    "AppConfig",
    "ConfigLoader",
    "DatabaseConfig",
    "LoggingConfig",
    "MonitoringConfig",
    "NotificationConfig",
    "SchedulerConfig",
    "TradingConfig",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from core.config import loader, schema

        mapping = {
            "ConfigLoader": loader.ConfigLoader,
            "AppConfig": schema.AppConfig,
            "DatabaseConfig": schema.DatabaseConfig,
            "LoggingConfig": schema.LoggingConfig,
            "MonitoringConfig": schema.MonitoringConfig,
            "NotificationConfig": schema.NotificationConfig,
            "SchedulerConfig": schema.SchedulerConfig,
            "TradingConfig": schema.TradingConfig,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
