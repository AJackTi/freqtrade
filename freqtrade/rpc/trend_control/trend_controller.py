#!/usr/bin/env python3
"""
Trend Direction Controller
Thread-safe cache-based trend control with auto-expiration
"""

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class TrendDirectionController:
    """Controls trading trend direction with cache expiration."""

    def __init__(self, cache_file: str | None = None):
        """
        Initialize trend controller

        Args:
            cache_file: Path to cache file (default: user_data/trend_direction_cache.json)
        """
        self.cache_file = Path(cache_file or "user_data/trend_direction_cache.json")

        # Thread-safe lock for cache operations
        self._lock = threading.Lock()

        # Ensure cache directory exists
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

    def get_trend_direction(self) -> str | None:
        """
        Get current trend direction from cache.
        Returns None if expired or not set.

        Returns:
            str: 'bull', 'bear', 'neutral' or None if expired/not set
        """
        with self._lock:
            try:
                if not self.cache_file.exists():
                    return None

                with open(self.cache_file, "r") as f:
                    cache_data = json.load(f)

                # Check expiration
                expire_time = datetime.fromisoformat(cache_data.get("expires_at", "1970-01-01"))
                if datetime.now() > expire_time:
                    logger.info("Trend direction cache expired")
                    return None

                direction = cache_data.get("direction")
                if direction in ["bull", "bear", "neutral"]:
                    logger.info(f"Using cached trend direction: {direction}")
                    return direction

                return None

            except Exception as e:
                logger.error(f"Error reading trend direction cache: {e}")
                return None

    def set_trend_direction(self, direction: str, duration_hours: int = 24) -> bool:
        """
        Set trend direction with expiration.

        Args:
            direction: 'bull', 'bear', or 'neutral'
            duration_hours: Hours until expiration (default 24)

        Returns:
            bool: True if successful
        """
        if direction not in ["bull", "bear", "neutral"]:
            logger.error(f"Invalid trend direction: {direction}")
            return False

        with self._lock:
            try:
                # Read existing cache or create new
                cache_data = {}
                if self.cache_file.exists():
                    try:
                        with open(self.cache_file, "r") as f:
                            cache_data = json.load(f)
                    except Exception:
                        cache_data = {}

                expires_at = datetime.now() + timedelta(hours=duration_hours)

                cache_data.update(
                    {
                        "direction": direction,
                        "set_at": datetime.now().isoformat(),
                        "expires_at": expires_at.isoformat(),
                        "duration_hours": duration_hours,
                        "set_by": "trend_controller",
                    }
                )

                with open(self.cache_file, "w") as f:
                    json.dump(cache_data, f, indent=2)

                logger.info(f"Set trend direction to {direction} for {duration_hours} hours")
                return True

            except Exception as e:
                logger.error(f"Error setting trend direction: {e}")
                return False

    def clear_trend_direction(self) -> bool:
        """Clear current trend direction."""
        with self._lock:
            try:
                if self.cache_file.exists():
                    self.cache_file.unlink()
                    logger.info("Cleared trend direction cache")
                return True
            except Exception as e:
                logger.error(f"Error clearing trend direction: {e}")
                return False

    def get_stoploss(self) -> float | None:
        """
        Get current stoploss value from cache.
        Returns None if not set or expired.

        Returns:
            float: Stoploss value (e.g., -0.10 for 10% stop) or None
        """
        with self._lock:
            try:
                if not self.cache_file.exists():
                    return None

                with open(self.cache_file, "r") as f:
                    cache_data = json.load(f)

                # Check expiration
                expire_time = datetime.fromisoformat(cache_data.get("expires_at", "1970-01-01"))
                if datetime.now() > expire_time:
                    return None

                stoploss = cache_data.get("stoploss")
                if stoploss is not None:
                    logger.info(f"Using cached stoploss: {stoploss}")
                    return float(stoploss)

                return None

            except Exception as e:
                logger.error(f"Error reading stoploss from cache: {e}")
                return None

    def set_stoploss(self, stoploss: float, duration_hours: int = 24) -> bool:
        """
        Set stoploss value with expiration.

        Args:
            stoploss: Stoploss value (e.g., -0.10 for 10% stop)
            duration_hours: Hours until expiration (default 24)

        Returns:
            bool: True if successful
        """
        # Ensure stoploss is negative
        if stoploss > 0:
            stoploss = -stoploss

        # Validate reasonable range
        if stoploss < -0.99 or stoploss > -0.01:
            logger.error(f"Invalid stoploss value: {stoploss}")
            return False

        with self._lock:
            try:
                # Read existing cache or create new
                cache_data = {}
                if self.cache_file.exists():
                    try:
                        with open(self.cache_file, "r") as f:
                            cache_data = json.load(f)
                    except Exception:
                        cache_data = {}

                expires_at = datetime.now() + timedelta(hours=duration_hours)

                cache_data.update(
                    {
                        "stoploss": stoploss,
                        "stoploss_set_at": datetime.now().isoformat(),
                        "expires_at": expires_at.isoformat(),
                        "duration_hours": duration_hours,
                        "set_by": "trend_controller",
                    }
                )

                with open(self.cache_file, "w") as f:
                    json.dump(cache_data, f, indent=2)

                logger.info(f"Set stoploss to {stoploss:.2%} for {duration_hours} hours")
                return True

            except Exception as e:
                logger.error(f"Error setting stoploss: {e}")
                return False

    def get_hold_on_trend(self) -> bool | None:
        """
        Get hold_on_trend setting from cache.
        Returns None if not set or expired.

        Returns:
            bool: True to hold positions in trend, False to take profits, None if not set
        """
        with self._lock:
            try:
                if not self.cache_file.exists():
                    return None

                with open(self.cache_file, "r") as f:
                    cache_data = json.load(f)

                # Check expiration
                expire_time = datetime.fromisoformat(cache_data.get("expires_at", "1970-01-01"))
                if datetime.now() > expire_time:
                    return None

                hold_on_trend = cache_data.get("hold_on_trend")
                if hold_on_trend is not None:
                    logger.info(f"Using cached hold_on_trend: {hold_on_trend}")
                    return bool(hold_on_trend)

                return None

            except Exception as e:
                logger.error(f"Error reading hold_on_trend from cache: {e}")
                return None

    def set_hold_on_trend(self, hold: bool, duration_hours: int = 24) -> bool:
        """
        Set hold_on_trend setting with expiration.
        When enabled, positions in the trend direction will be held without taking profits.

        Args:
            hold: True to hold positions, False to take profits normally
            duration_hours: Hours until expiration (default 24)

        Returns:
            bool: True if successful
        """
        with self._lock:
            try:
                # Read existing cache or create new
                cache_data = {}
                if self.cache_file.exists():
                    try:
                        with open(self.cache_file, "r") as f:
                            cache_data = json.load(f)
                    except Exception:
                        cache_data = {}

                expires_at = datetime.now() + timedelta(hours=duration_hours)

                cache_data.update(
                    {
                        "hold_on_trend": hold,
                        "hold_on_trend_set_at": datetime.now().isoformat(),
                        "expires_at": expires_at.isoformat(),
                        "duration_hours": duration_hours,
                        "set_by": "trend_controller",
                    }
                )

                with open(self.cache_file, "w") as f:
                    json.dump(cache_data, f, indent=2)

                logger.info(f"Set hold_on_trend to {hold} for {duration_hours} hours")
                return True

            except Exception as e:
                logger.error(f"Error setting hold_on_trend: {e}")
                return False

    def clear_risk_settings(self) -> bool:
        """Clear stoploss and hold_on_trend settings."""
        with self._lock:
            try:
                if self.cache_file.exists():
                    with open(self.cache_file, "r") as f:
                        cache_data = json.load(f)

                    # Remove risk-related settings
                    cache_data.pop("stoploss", None)
                    cache_data.pop("stoploss_set_at", None)
                    cache_data.pop("hold_on_trend", None)
                    cache_data.pop("hold_on_trend_set_at", None)

                    # If only metadata remains, clear the file
                    if not any(
                        k
                        for k in cache_data.keys()
                        if k not in ["expires_at", "duration_hours", "set_by"]
                    ):
                        self.cache_file.unlink()
                        logger.info("Cleared all risk settings and removed cache file")
                    else:
                        with open(self.cache_file, "w") as f:
                            json.dump(cache_data, f, indent=2)
                        logger.info("Cleared risk settings from cache")

                return True
            except Exception as e:
                logger.error(f"Error clearing risk settings: {e}")
                return False

    def get_cache_info(self) -> dict[str, Any]:
        """Get detailed cache information."""
        with self._lock:
            try:
                if not self.cache_file.exists():
                    return {
                        "status": "empty",
                        "direction": None,
                        "stoploss": None,
                        "hold_on_trend": None,
                    }

                with open(self.cache_file, "r") as f:
                    cache_data = json.load(f)

                expire_time = datetime.fromisoformat(cache_data.get("expires_at", "1970-01-01"))
                now = datetime.now()

                info = {
                    "status": "active" if now <= expire_time else "expired",
                    "direction": cache_data.get("direction"),
                    "stoploss": cache_data.get("stoploss"),
                    "hold_on_trend": cache_data.get("hold_on_trend"),
                    "set_at": cache_data.get("set_at"),
                    "stoploss_set_at": cache_data.get("stoploss_set_at"),
                    "hold_on_trend_set_at": cache_data.get("hold_on_trend_set_at"),
                    "expires_at": cache_data.get("expires_at"),
                    "duration_hours": cache_data.get("duration_hours"),
                    "time_remaining": str(expire_time - now) if now <= expire_time else "expired",
                }

                return info

            except Exception as e:
                logger.error(f"Error getting cache info: {e}")
                return {"status": "error", "error": str(e)}


# Global controller instance
_controller = None


def get_trend_controller() -> TrendDirectionController:
    """Get global trend controller instance."""
    global _controller
    if _controller is None:
        _controller = TrendDirectionController()
    return _controller
