"""
Trend Control Module for Freqtrade
Provides trend direction control with cache-based expiration
"""

from .trend_controller import TrendDirectionController, get_trend_controller
from .trend_helper import (
    get_stoploss_override,
    get_trend_direction,
    is_long_allowed,
    is_short_allowed,
    should_enter_long,
    should_enter_short,
    should_hold_on_trend,
)


__all__ = [
    "TrendDirectionController",
    "get_trend_controller",
    "get_trend_direction",
    "should_enter_long",
    "should_enter_short",
    "get_stoploss_override",
    "should_hold_on_trend",
    "is_long_allowed",
    "is_short_allowed",
]
