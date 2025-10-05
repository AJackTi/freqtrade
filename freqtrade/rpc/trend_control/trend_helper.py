#!/usr/bin/env python3
"""
Trend Helper - Simple interface for strategies to check trend direction
"""

import logging


logger = logging.getLogger(__name__)


def get_trend_direction() -> str | None:
    """
    Get current trend direction from cache.

    Returns:
        str: 'bull', 'bear', 'neutral' or None if not set/expired

    Usage in strategy:
        from user_data.utils.trend_helper import get_trend_direction

        trend = get_trend_direction()
        if trend == 'bull':
            # Only consider long entries
        elif trend == 'bear':
            # Only consider short entries
        elif trend == 'neutral':
            # Consider both but be more conservative
        else:
            # No override - use normal strategy logic
    """
    try:
        # Read directly from cache file to avoid telegram dependency
        import json
        from datetime import datetime
        from pathlib import Path

        cache_file = Path("user_data/trend_direction_cache.json")
        if not cache_file.exists():
            return None

        with open(cache_file, "r") as f:
            cache_data = json.load(f)

        # Check expiration
        expire_time = datetime.fromisoformat(cache_data.get("expires_at", "1970-01-01"))
        if datetime.now() > expire_time:
            return None

        direction = cache_data.get("direction")
        if direction in ["bull", "bear", "neutral"]:
            return direction

        return None
    except Exception as e:
        logger.error(f"Error getting trend direction: {e}")
        return None


def should_enter_long() -> bool:
    """
    Check if long entries are allowed based on trend.

    Returns:
        bool: True if long entries allowed, False otherwise
    """
    trend = get_trend_direction()
    if trend is None:
        return True  # No override, allow normal logic
    return trend in ["bull", "neutral"]


def should_enter_short() -> bool:
    """
    Check if short entries are allowed based on trend.

    Returns:
        bool: True if short entries allowed, False otherwise
    """
    trend = get_trend_direction()
    if trend is None:
        return True  # No override, allow normal logic
    return trend in ["bear", "neutral"]


def get_stoploss_override() -> float | None:
    """
    Get stoploss override from cache.

    Returns:
        float: Stoploss value (e.g., -0.10 for 10%) or None for strategy default
    """
    try:
        # Read directly from cache file to avoid telegram dependency
        import json
        from datetime import datetime
        from pathlib import Path

        cache_file = Path("user_data/trend_direction_cache.json")
        if not cache_file.exists():
            return None

        with open(cache_file, "r") as f:
            cache_data = json.load(f)

        # Check expiration
        expire_time = datetime.fromisoformat(cache_data.get("expires_at", "1970-01-01"))
        if datetime.now() > expire_time:
            return None

        stoploss = cache_data.get("stoploss")
        if stoploss is not None:
            return float(stoploss)

        return None
    except Exception as e:
        logger.error(f"Error getting stoploss override: {e}")
        return None


def should_hold_on_trend(is_short: bool) -> bool:
    """
    Check if position should be held (no profit taking) based on trend.

    Args:
        is_short: True if position is short, False if long

    Returns:
        bool: True if should hold position, False for normal profit taking
    """
    try:
        # Read directly from cache file to avoid telegram dependency
        import json
        from datetime import datetime
        from pathlib import Path

        cache_file = Path("user_data/trend_direction_cache.json")
        if not cache_file.exists():
            return False

        with open(cache_file, "r") as f:
            cache_data = json.load(f)

        # Check expiration
        expire_time = datetime.fromisoformat(cache_data.get("expires_at", "1970-01-01"))
        if datetime.now() > expire_time:
            return False

        hold_enabled = cache_data.get("hold_on_trend")
        if not hold_enabled:
            return False

        trend = cache_data.get("direction")
        if trend is None:
            return False

        # Hold if position direction matches trend direction
        if trend == "bull" and not is_short:
            return True  # Bull trend + long position = hold
        elif trend == "bear" and is_short:
            return True  # Bear trend + short position = hold

        return False

    except Exception as e:
        logger.error(f"Error checking hold on trend: {e}")
        return False


# Alias functions for backward compatibility
is_long_allowed = should_enter_long
is_short_allowed = should_enter_short
