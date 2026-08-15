"""Trade Logger - structured logging for all trading events."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("quantdesk.trading")


class LogEventType(str, Enum):
    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    ORDER_PARTIAL = "order_partial"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    RISK_REJECTED = "risk_rejected"
    RISK_HALT = "risk_halt"
    SIGNAL_GENERATED = "signal_generated"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    ENGINE_START = "engine_start"
    ENGINE_STOP = "engine_stop"
    ERROR = "error"


@dataclass
class TradeLogEntry:
    event_type: LogEventType
    timestamp: datetime = field(default_factory=datetime.utcnow)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            **self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class TradeLogger:
    """Structured trade event logger.

    Logs to both Python logger and an in-memory buffer.
    Can optionally log to a JSON-lines file.
    """

    def __init__(self, log_file: str | None = None) -> None:
        self._entries: list[TradeLogEntry] = []
        self._log_file = log_file
        if log_file:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: LogEventType, **kwargs: Any) -> TradeLogEntry:
        entry = TradeLogEntry(event_type=event_type, data=kwargs)
        self._entries.append(entry)

        # Python logger
        level = logging.WARNING if event_type in (
            LogEventType.RISK_REJECTED, LogEventType.RISK_HALT, LogEventType.ERROR
        ) else logging.INFO
        logger.log(level, "[%s] %s", event_type.value, json.dumps(kwargs, default=str))

        # File
        if self._log_file:
            with open(self._log_file, "a") as f:
                f.write(entry.to_json() + "\n")

        return entry

    def log_order_submitted(self, order_id: str, symbol: str, side: str, qty: float, price: float | None = None) -> None:
        self.log(LogEventType.ORDER_SUBMITTED, order_id=order_id, symbol=symbol, side=side, qty=qty, price=price)

    def log_order_filled(self, order_id: str, symbol: str, side: str, qty: float, price: float) -> None:
        self.log(LogEventType.ORDER_FILLED, order_id=order_id, symbol=symbol, side=side, qty=qty, price=price, amount=round(qty * price, 2))

    def log_order_cancelled(self, order_id: str, symbol: str) -> None:
        self.log(LogEventType.ORDER_CANCELLED, order_id=order_id, symbol=symbol)

    def log_risk_rejected(self, order_id: str, symbol: str, reason: str) -> None:
        self.log(LogEventType.RISK_REJECTED, order_id=order_id, symbol=symbol, reason=reason)

    def log_risk_halt(self, reason: str) -> None:
        self.log(LogEventType.RISK_HALT, reason=reason)

    def log_signal(self, symbol: str, signal: str, **kwargs: Any) -> None:
        self.log(LogEventType.SIGNAL_GENERATED, symbol=symbol, signal=signal, **kwargs)

    def log_error(self, message: str, error: str = "") -> None:
        self.log(LogEventType.ERROR, message=message, error=error)

    def get_entries(self, event_type: LogEventType | None = None, limit: int = 100) -> list[dict[str, Any]]:
        entries = self._entries
        if event_type:
            entries = [e for e in entries if e.event_type == event_type]
        return [e.to_dict() for e in entries[-limit:]]

    def get_rejected_orders(self) -> list[dict[str, Any]]:
        return self.get_entries(LogEventType.RISK_REJECTED)

    def get_filled_orders(self) -> list[dict[str, Any]]:
        return self.get_entries(LogEventType.ORDER_FILLED)

    def reset(self) -> None:
        self._entries.clear()
