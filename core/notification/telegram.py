"""Telegram Bot API notifier."""

import time
import logging
import threading

import requests

from .base import Notifier, Level

logger = logging.getLogger(__name__)

# Telegram rate limit: 20 messages/sec to a single chat
_MAX_RATE = 20
_RATE_WINDOW = 1.0  # seconds


class TelegramNotifier(Notifier):
    """Sends notifications via the Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str, parse_mode: str = "Markdown"):
        """
        Args:
            bot_token: Telegram bot token from BotFather.
            chat_id: Target chat/group/user ID.
            parse_mode: 'Markdown', 'MarkdownV2', or 'HTML'.
        """
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.parse_mode = parse_mode
        self.api_base = f"https://api.telegram.org/bot{bot_token}"
        self._session = requests.Session()
        self._max_retries = 3

        # Rate limiting state
        self._rate_lock = threading.Lock()
        self._timestamps: list[float] = []

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------
    def _wait_for_rate_limit(self) -> None:
        """Block until we are within the allowed message rate."""
        with self._rate_lock:
            now = time.monotonic()
            # Prune timestamps older than the window
            self._timestamps = [
                t for t in self._timestamps if now - t < _RATE_WINDOW
            ]
            if len(self._timestamps) >= _MAX_RATE:
                sleep_for = _RATE_WINDOW - (now - self._timestamps[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self._timestamps.append(time.monotonic())

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------
    def send(self, message: str, level: str = "info") -> bool:
        """Send *message* to the configured Telegram chat.

        Retries up to ``self._max_retries`` times on transient failures.

        Returns:
            True on success, False after all retries exhausted.
        """
        self._wait_for_rate_limit()

        url = f"{self.api_base}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": self.parse_mode,
        }

        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._session.post(url, json=payload, timeout=10)
                if resp.status_code == 200 and resp.json().get("ok"):
                    return True

                # Rate-limited by Telegram – back off
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get(
                        "retry_after", 5
                    )
                    logger.warning(
                        "Telegram rate-limited, retrying after %ds", retry_after
                    )
                    time.sleep(retry_after)
                    continue

                logger.warning(
                    "Telegram API returned %d (attempt %d/%d): %s",
                    resp.status_code,
                    attempt,
                    self._max_retries,
                    resp.text,
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Telegram request failed (attempt %d/%d): %s",
                    attempt,
                    self._max_retries,
                    exc,
                )

            if attempt < self._max_retries:
                time.sleep(1 * attempt)  # simple back-off

        logger.error("Failed to send Telegram message after %d attempts", self._max_retries)
        return False
