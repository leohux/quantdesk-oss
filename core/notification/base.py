"""Base notifier abstract class and Level enum."""

from abc import ABC, abstractmethod
from enum import IntEnum


class Level(IntEnum):
    """Alert severity levels. Higher value = more severe."""
    INFO = 0
    WARNING = 1
    ERROR = 2
    CRITICAL = 3


class Notifier(ABC):
    """Abstract base class for all notification backends."""

    @abstractmethod
    def send(self, message: str, level: str = "info") -> bool:
        """Send a plain text message.

        Args:
            message: The message body.
            level: Severity level string (info, warning, error, critical).

        Returns:
            True if sent successfully, False otherwise.
        """
        ...

    def send_trade_alert(
        self, symbol: str, side: str, qty: float, price: float, status: str
    ) -> bool:
        """Send a trade execution alert.

        Args:
            symbol: Ticker symbol (e.g. AAPL).
            side: 'buy' or 'sell'.
            qty: Quantity traded.
            price: Execution price.
            status: Order status (e.g. filled, partial, cancelled).

        Returns:
            True if sent successfully.
        """
        emoji = "🟢" if side.lower() == "buy" else "🔴"
        message = (
            f"{emoji} *Trade Alert*\n"
            f"Symbol: `{symbol}`\n"
            f"Side: *{side.upper()}*\n"
            f"Qty: `{qty}`\n"
            f"Price: `${price:.2f}`\n"
            f"Status: {status}"
        )
        return self.send(message, level="info")

    def send_risk_alert(self, reason: str, details: str) -> bool:
        """Send a risk management alert.

        Args:
            reason: Brief reason for the alert.
            details: Detailed explanation.

        Returns:
            True if sent successfully.
        """
        message = (
            f"⚠️ *Risk Alert*\n"
            f"Reason: {reason}\n"
            f"Details: {details}"
        )
        return self.send(message, level="warning")

    def send_system_alert(self, message: str, severity: str = "error") -> bool:
        """Send a system-level alert.

        Args:
            message: Alert message.
            severity: Severity level string.

        Returns:
            True if sent successfully.
        """
        formatted = f"🚨 *System Alert*\n{message}"
        return self.send(formatted, level=severity)
