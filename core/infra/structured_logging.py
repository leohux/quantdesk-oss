"""
Structured JSON logging for the US-Quant platform.

Provides:
  • JsonFormatter — JSON-lines formatter with extra-field support.
  • setup_logging — convenience root-logger configuration.
  • ContextFilter  — thread-local context (request_id, trade_id, …).
  • AuditLogger    — dedicated trade-audit logger.

Uses only the standard library.  Import path assumes /app in sys.path.
"""

from __future__ import annotations

import datetime
import json
import logging
import logging.handlers
import os
import threading
import traceback as _traceback_mod
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# 1. JsonFormatter
# ---------------------------------------------------------------------------

# Standard LogRecord attributes that should NOT be leaked into extras.
_STANDARD_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "created", "relativeCreated", "msecs",
        "pathname", "filename", "module", "funcName", "lineno", "levelno",
        "levelname", "thread", "threadName", "process", "processName",
        "exc_info", "exc_text", "stack_info", "message", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line.

    Fixed fields: ``timestamp``, ``level``, ``logger``, ``message``,
    ``module``, ``function``, ``line``.

    Any *extra* keys passed via ``logger.info("…", extra={"trade_id": 42})``
    (or injected by a :class:`ContextFilter`) are included automatically.
    Exception info and stack info are serialised when present.
    """

    def __init__(
        self,
        datefmt: Optional[str] = None,
        json_default: Optional[Any] = None,
    ) -> None:
        super().__init__(datefmt=datefmt)
        self._json_default = json_default or str

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        log_entry: Dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt or "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # ---- extra fields ------------------------------------------------
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _STANDARD_RECORD_ATTRS:
                continue
            # Avoid duplicating the fixed keys
            if key in log_entry:
                continue
            log_entry[key] = value

        # ---- exception ----------------------------------------------------
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": _traceback_mod.format_exception(*record.exc_info),
            }

        # ---- stack info ---------------------------------------------------
        if record.stack_info:
            log_entry["stack_info"] = record.stack_info

        return json.dumps(log_entry, default=self._json_default, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 2. ContextFilter
# ---------------------------------------------------------------------------

class ContextFilter(logging.Filter):
    """Inject thread-local context fields into every log record.

    Usage::

        ctx = ContextFilter()
        ctx.set_context(request_id="abc-123", trade_id=42)
        logger.addFilter(ctx)
        logger.info("order sent")  # record will include request_id, trade_id
        ctx.clear_context()
    """

    def __init__(self, name: str = "") -> None:
        super().__init__(name)
        self._local = threading.local()

    # -- context management -------------------------------------------------

    def set_context(self, **kwargs: Any) -> None:
        """Store context fields that will be attached to every record."""
        for key, value in kwargs.items():
            setattr(self._local, key, value)

    def clear_context(self, *keys: str) -> None:
        """Remove specific context fields, or all if no keys given."""
        if keys:
            for key in keys:
                self._local.__dict__.pop(key, None)
        else:
            self._local.__dict__.clear()

    def get_context(self) -> Dict[str, Any]:
        """Return a snapshot of the current context."""
        return {
            k: v
            for k, v in self._local.__dict__.items()
            if not k.startswith("_")
        }

    # -- logging.Filter interface ------------------------------------------

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in self._local.__dict__.items():
            if key.startswith("_"):
                continue
            # Don't overwrite existing explicit extras
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


# ---------------------------------------------------------------------------
# 3. setup_logging
# ---------------------------------------------------------------------------

def setup_logging(
    config_dict: Optional[Dict[str, Any]] = None,
    *,
    level: str = "INFO",
    fmt: str = "json",
    log_dir: Optional[str] = None,
    max_size_mb: int = 50,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure the root logger with console + optional rotating file handler.

    Parameters
    ----------
    config_dict : dict, optional
        If provided, keyword arguments are read from this dict (keys:
        ``level``, ``format``, ``log_dir``, ``max_size_mb``, ``backup_count``).
    level : str
        Logging level name (default ``"INFO"``).
    fmt : str
        ``"json"`` for :class:`JsonFormatter` or ``"text"`` for a human-readable
        default format.
    log_dir : str or None
        Directory for log files.  Created if it does not exist.  When *None*
        only a console handler is added.
    max_size_mb : int
        Maximum size in MiB before rotation (default 50).
    backup_count : int
        Number of rotated backup files to keep (default 5).

    Returns
    -------
    logging.Logger
        The configured root logger.
    """
    if config_dict:
        level = config_dict.get("level", level)
        fmt = config_dict.get("format", fmt)
        log_dir = config_dict.get("log_dir", log_dir)
        max_size_mb = config_dict.get("max_size_mb", max_size_mb)
        backup_count = config_dict.get("backup_count", backup_count)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates on repeated calls.
    root.handlers.clear()

    # ---- formatter --------------------------------------------------------
    if fmt == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | "
            "%(module)s:%(funcName)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # ---- console handler --------------------------------------------------
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(console)

    # ---- file handler (rotating) ------------------------------------------
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "app.log")
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(file_handler)

    return root


# ---------------------------------------------------------------------------
# 4. AuditLogger
# ---------------------------------------------------------------------------

class AuditLogger:
    """Dedicated JSON logger for trade audit events.

    Writes to ``audit.log`` in the given ``log_dir`` (or current directory).

    Each log entry is a JSON line with the event type in the ``event`` field.

    Usage::

        audit = AuditLogger(log_dir="/var/log/us-quant")
        audit.log_order("ORD-1", "AAPL", "BUY", 100, 150.25, "NEW")
        audit.log_fill("ORD-1", "AAPL", "BUY", 100, 150.25, 1.50)
        audit.log_risk_rejection("ORD-2", "TSLA", "position limit exceeded")
    """

    def __init__(
        self,
        log_dir: Optional[str] = None,
        max_size_mb: int = 50,
        backup_count: int = 10,
        logger_name: str = "audit",
    ) -> None:
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.INFO)
        # Prevent propagation so audit events don't appear in app.log / console.
        self._logger.propagate = False

        # Avoid adding duplicate handlers on repeated instantiation.
        if not self._logger.handlers:
            target_dir = log_dir or "."
            os.makedirs(target_dir, exist_ok=True)
            audit_file = os.path.join(target_dir, "audit.log")

            handler = logging.handlers.RotatingFileHandler(
                audit_file,
                maxBytes=max_size_mb * 1024 * 1024,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(JsonFormatter())
            self._logger.addHandler(handler)

    # -- public helpers -----------------------------------------------------

    def log_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        status: str,
    ) -> None:
        """Log an order event (new, replace, cancel, …)."""
        self._logger.info(
            "order %s %s %s qty=%s price=%s status=%s",
            order_id, symbol, side, qty, price, status,
            extra={
                "event": "order",
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "status": status,
            },
        )

    def log_fill(
        self,
        order_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        commission: float,
    ) -> None:
        """Log a fill / execution event."""
        self._logger.info(
            "fill %s %s %s qty=%s price=%s commission=%s",
            order_id, symbol, side, qty, price, commission,
            extra={
                "event": "fill",
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "commission": commission,
            },
        )

    def log_risk_rejection(
        self,
        order_id: str,
        symbol: str,
        reason: str,
    ) -> None:
        """Log a risk-rejection event."""
        self._logger.warning(
            "risk_rejection %s %s reason=%s",
            order_id, symbol, reason,
            extra={
                "event": "risk_rejection",
                "order_id": order_id,
                "symbol": symbol,
                "reason": reason,
            },
        )


__all__ = [
    "JsonFormatter",
    "ContextFilter",
    "setup_logging",
    "AuditLogger",
]
