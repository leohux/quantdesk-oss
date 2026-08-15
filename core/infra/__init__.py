"""
core.infra — Infrastructure and resilience utilities.
"""

from .resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    Heartbeat,
    Watchdog,
    circuit_breaker,
    retry,
)
from .structured_logging import (
    AuditLogger,
    ContextFilter,
    JsonFormatter,
    setup_logging,
)

__all__ = [
    # resilience
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "Heartbeat",
    "Watchdog",
    "circuit_breaker",
    "retry",
    # structured_logging
    "AuditLogger",
    "ContextFilter",
    "JsonFormatter",
    "setup_logging",
]
