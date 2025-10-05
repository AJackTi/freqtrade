#!/usr/bin/env python3
"""Test trend helper import"""

from user_data.utils.trend_helper import (
    get_stoploss_override,
    get_trend_direction,
    should_enter_long,
    should_enter_short,
    should_hold_on_trend,
)


print("✅ Import successful!")
print(f"Trend direction: {get_trend_direction()}")
print(f"Should enter long: {should_enter_long()}")
print(f"Should enter short: {should_enter_short()}")
print(f"Stoploss override: {get_stoploss_override()}")
print(f"Hold on trend (long): {should_hold_on_trend(False)}")
print(f"Hold on trend (short): {should_hold_on_trend(True)}")
