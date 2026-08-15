"""Lightweight pub/sub event bus for decoupled module communication.

Events flow: MarketData -> Strategy -> Signal -> Risk -> Order -> Execution -> Fill
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    MARKET_DATA = "market_data"
    SIGNAL = "signal"
    RISK_CHECK = "risk_check"
    ORDER = "order"
    FILL = "fill"
    EXECUTION = "execution"
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: str = ""

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


# Type alias for event handlers
EventHandler = Callable[[Event], None]


class EventBus:
    """Simple synchronous event bus."""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = {}

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def publish(self, event: Event) -> None:
        handlers = self._handlers.get(event.type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("Event handler error for %s", event.type)

    def clear(self) -> None:
        self._handlers.clear()


# Global event bus singleton
_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
