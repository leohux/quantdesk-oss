"""
ConfigLoader — YAML-based configuration loader with env-profile support.

Usage:
    from core.config.loader import ConfigLoader
    from core.config.schema import AppConfig

    loader = ConfigLoader(profile="development")
    cfg: AppConfig = loader.config
    val = loader.get("trading", "sizing_pct", default=0.02)

Environment variable overrides:
    QUANTDESK_<SECTION>__<KEY>=value
    e.g. QUANTDESK_TRADING__SIZING_PCT=0.05
         QUANTDESK_DATABASE__POSTGRES_URL=postgresql://...
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

import yaml

from core.config.schema import AppConfig

logger = logging.getLogger(__name__)

# Supported profile names
_PROFILES = ("development", "paper", "production")
_ENV_PREFIX = "QUANTDESK_"
_DEFAULT_CONFIG_DIR = Path("/app/config")


def _discover_config_dir() -> Path:
    """Return config directory from env or fallback default."""
    env_val = os.environ.get("QUANTDESK_CONFIG_DIR")
    if env_val:
        return Path(env_val)
    return _DEFAULT_CONFIG_DIR


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict. Returns {} if missing."""
    if not path.is_file():
        logger.debug("Config file not found, skipping: %s", path)
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Apply environment variable overrides on top of *raw* config dict.

    Pattern: QUANTDESK_<SECTION>__<KEY>=value
    The section is lowercased; the key is lowercased; double-underscore
    separates section from key.

    For list values (e.g. trading symbols), comma-separated strings are
    converted to lists.  Numeric strings are cast to int/float when
    the existing value provides a type hint.
    """
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue

        suffix = env_key[len(_ENV_PREFIX):]  # e.g. TRADING__SIZING_PCT
        parts = suffix.split("__", maxsplit=1)
        if len(parts) != 2:
            continue

        section, key = parts[0].lower(), parts[1].lower()
        section_dict = raw.get(section)
        if not isinstance(section_dict, dict):
            # Create section if it doesn't exist yet
            if section not in raw:
                raw[section] = {}
                section_dict = raw[section]
            else:
                logger.warning(
                    "Env override for non-dict section '%s', skipping %s",
                    section,
                    env_key,
                )
                continue

        # Attempt to coerce the value to match the existing type
        existing = section_dict.get(key)
        coerced: Any = env_val

        if isinstance(existing, bool):
            coerced = env_val.lower() in ("true", "1", "yes")
        elif isinstance(existing, int) and not isinstance(existing, bool):
            try:
                coerced = int(env_val)
            except ValueError:
                pass
        elif isinstance(existing, float):
            try:
                coerced = float(env_val)
            except ValueError:
                pass
        elif isinstance(existing, list):
            coerced = [s.strip() for s in env_val.split(",")]

        section_dict[key] = coerced
        logger.debug("Env override: %s.%s = %r", section, key, coerced)

    return raw


class ConfigLoader:
    """
    Thread-safe YAML configuration loader.

    Parameters
    ----------
    profile : str
        Environment profile name (development, paper, production).
    config_dir : Path | str | None
        Explicit config directory.  When *None* the directory is auto-discovered
        via QUANTDESK_CONFIG_DIR env var or falls back to /app/config/.
    """

    def __init__(
        self,
        profile: str = "development",
        config_dir: Optional[str | Path] = None,
    ) -> None:
        if profile not in _PROFILES:
            raise ValueError(
                f"Unknown profile '{profile}'. Choose from {_PROFILES}"
            )
        self._profile = profile
        self._config_dir = (
            Path(config_dir) if config_dir else _discover_config_dir()
        )
        self._lock = threading.Lock()
        self._config: AppConfig = self._load()

    # -- public API ----------------------------------------------------------

    @property
    def profile(self) -> str:
        """Active profile name."""
        return self._profile

    @property
    def config(self) -> AppConfig:
        """Return the current AppConfig (thread-safe snapshot)."""
        with self._lock:
            return self._config

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """
        Retrieve a single config value.

        Parameters
        ----------
        section : str
            Section name (e.g. "trading", "database").
        key : str
            Key within the section (e.g. "sizing_pct").
        default : Any
            Fallback value if section or key is missing.

        Returns
        -------
        Any
        """
        with self._lock:
            section_obj = getattr(self._config, section, None)
            if section_obj is None:
                return default
            return getattr(section_obj, key, default)

    def reload(self) -> AppConfig:
        """
        Re-read YAML files and re-apply env overrides.
        Returns the freshly loaded AppConfig.
        """
        with self._lock:
            self._config = self._load()
        return self._config

    # -- internals -----------------------------------------------------------

    def _load(self) -> AppConfig:
        """Build AppConfig from YAML + env overrides."""
        base_path = self._config_dir / "base.yaml"
        profile_path = self._config_dir / f"{self._profile}.yaml"

        raw: dict[str, Any] = {}
        # Load base (shared defaults) first
        base_data = _load_yaml(base_path)
        raw.update(base_data)

        # Layer profile-specific config on top (shallow merge per section)
        profile_data = _load_yaml(profile_path)
        for section, values in profile_data.items():
            if isinstance(values, dict) and isinstance(raw.get(section), dict):
                raw[section].update(values)
            else:
                raw[section] = values

        # Apply env variable overrides
        raw = _apply_env_overrides(raw)

        # Set the environment field
        raw["environment"] = self._profile

        logger.info(
            "Loaded config profile=%s dir=%s", self._profile, self._config_dir
        )
        return AppConfig(**raw)
