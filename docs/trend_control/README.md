# Trend Control System

Control your bot's trend direction directly from Telegram! 📈📉

## 🚀 Quick Start

### 1. Use Telegram Commands (Easiest!)

```
/trendset bull 24        # Only LONGS for 24 hours
/shortall                # Quick: Only SHORTS for 24 hours
/trendstatus             # Check what's active
/trendclear              # Clear and return to normal
```

**👉 [Full Telegram CLI Guide](TELEGRAM_CLI.md)** ← Start here!

### 2. Add to Your Strategy

```python
from user_data.utils.trend_helper import (
    should_enter_long,
    should_enter_short,
    should_hold_on_trend
)

class MyStrategy(IStrategy):

    def populate_entry_trend(self, dataframe, metadata):
        conditions_long = [...]  # Your conditions
        conditions_short = [...]

        # Check trend before entering
        if should_enter_long():
            dataframe.loc[
                reduce(lambda x, y: x & y, conditions_long),
                'enter_long'
            ] = 1

        if should_enter_short():
            dataframe.loc[
                reduce(lambda x, y: x & y, conditions_short),
                'enter_short'
            ] = 1

        return dataframe

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        # Hold positions aligned with trend
        if should_hold_on_trend(trade.is_short):
            return None
        return None
```

### 3. Done!

Telegram commands → Strategy automatically follows the trend

## 📚 Documentation

**Quick Guides:**
- **[TELEGRAM_CLI.md](TELEGRAM_CLI.md)** ⭐ - All Telegram commands (start here!)
- [QUICK_REFERENCE.md](QUICK_REFERENCE.md) - Command cheat sheet

**Detailed Guides:**
- [INTEGRATION.md](INTEGRATION.md) - How to add to your strategy
- [USAGE.md](USAGE.md) - Complete feature documentation
- [SAFETY_ANALYSIS.md](SAFETY_ANALYSIS.md) - Safety & risk analysis

## Trend Modes

| Mode | Longs | Shorts | Use Case |
|------|-------|--------|----------|
| `bull` | ✅ | ❌ | Expecting uptrend |
| `bear` | ❌ | ✅ | Expecting downtrend |
| `neutral` | ✅ | ✅ | Uncertain / Conservative |
| `None` | ✅ | ✅ | Normal strategy logic |

## Cache File

Settings are stored in: `user_data/trend_direction_cache.json`

This file is automatically managed and includes:
- Trend direction
- Expiration timestamp
- Optional stoploss override
- Optional hold_on_trend setting

## Safety Features

- Thread-safe operations
- Auto-expiration prevents forgotten settings
- Read-only helper functions for strategies
- No forced entries - only bias control
- Comprehensive logging

## Helper Functions

```python
get_trend_direction()           # Returns: 'bull', 'bear', 'neutral', or None
should_enter_long()             # Returns: True/False
should_enter_short()            # Returns: True/False
get_stoploss_override()         # Returns: float or None
should_hold_on_trend(is_short)  # Returns: True/False
```

## Example Use Cases

### Expecting a Rally
```python
controller = get_trend_controller()
controller.set_trend_direction('bull', 4)  # Longs only for 4 hours
controller.set_hold_on_trend(True, 4)      # Hold positions without taking profits
```

### Reduce Risk During Uncertainty
```python
controller = get_trend_controller()
controller.set_trend_direction('neutral', 12)  # Both directions allowed
controller.set_stoploss(-0.05, 12)             # Tighter 5% stop loss
```

### Back to Normal
```python
controller = get_trend_controller()
controller.clear_trend_direction()
controller.clear_risk_settings()
```

## License

Same as Freqtrade
