# Trend Controller Usage Guide

## Overview

The Trend Controller allows you to manually override trading strategy behavior through both CLI and Telegram bot commands.

## Quick Start

### CLI Commands

```bash
# Check current trend
python user_data/trend_control.py get

# Set bullish trend (longs only) for 24 hours
python user_data/trend_control.py set bull --hours 24

# Set bearish trend (shorts only) for 12 hours
python user_data/trend_control.py set bear --hours 12

# Set neutral (both directions allowed)
python user_data/trend_control.py set neutral --hours 6

# Clear trend override
python user_data/trend_control.py clear

# Show detailed status
python user_data/trend_control.py status

# Enable hold on trend
python user_data/trend_control.py hold on --hours 24

# Disable hold on trend
python user_data/trend_control.py hold off

# Clear risk settings (stoploss + hold)
python user_data/trend_control.py clearrisk
```

### Using in Your Strategy

```python
from user_data.utils.trend_helper import (
    get_trend_direction,
    should_enter_long,
    should_enter_short,
    get_stoploss_override,
    should_hold_on_trend
)

class MyStrategy(IStrategy):

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Get trend direction
        trend = get_trend_direction()  # Returns: 'bull', 'bear', 'neutral', or None

        # Method 1: Simple check
        if should_enter_long():
            dataframe.loc[
                (dataframe['your_signal'] == 1),
                'enter_long'
            ] = 1

        if should_enter_short():
            dataframe.loc[
                (dataframe['your_signal'] == -1),
                'enter_short'
            ] = 1

        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        # Check for stoploss override
        sl_override = get_stoploss_override()
        if sl_override is not None:
            return sl_override

        # Return your normal stoploss logic
        return self.stoploss

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        # Check if we should hold this position
        if should_hold_on_trend(trade.is_short):
            return None  # Don't exit, hold the position

        # Your normal exit logic
        if current_profit > 0.05:
            return 'take_profit'

        return None
```

## Telegram Bot Commands

### Trend Control
- `/start` - Show main menu
- `/help` - Show detailed help
- `/get` - Get current trend direction
- `/set bull [hours]` - Set bullish trend (default 24h)
- `/set bear [hours]` - Set bearish trend
- `/set neutral [hours]` - Set neutral trend
- `/clear` - Clear trend direction
- `/longall` - Quick set to bull for 24h
- `/shortall` - Quick set to bear for 24h
- `/neutral` - Quick set to neutral for 24h

### Risk Management
- `/sl 10 [hours]` - Set 10% stop loss (default 24h)
- `/hold on [hours]` - Enable hold on trend (default 24h)
- `/hold off` - Disable hold on trend
- `/clearrisk` - Clear stoploss & hold settings

### Parameters
- `/tp 15 [hours]` - Set 15% take profit target
- `/params` - Show all active parameters
- `/clearparams` - Clear all parameter overrides

### Position Management
- `/exitlong` - Close all long positions
- `/exitshort` - Close all short positions
- `/exitall` - Close all positions

### Status
- `/status` - Show current settings and status

## Trend Direction Modes

### Bull Mode (`bull`)
- **Behavior**: Strategy will ONLY open long positions
- **Effect**: All short entry signals are ignored
- **Use Case**: When you expect strong upward movement

### Bear Mode (`bear`)
- **Behavior**: Strategy will ONLY open short positions
- **Effect**: All long entry signals are ignored
- **Use Case**: When you expect strong downward movement

### Neutral Mode (`neutral`)
- **Behavior**: Both long and short entries allowed
- **Effect**: More conservative - can be used to reduce position sizing
- **Use Case**: Uncertain market conditions

### No Override (`None`)
- **Behavior**: Strategy uses its normal logic
- **Effect**: No manual intervention
- **Use Case**: Let the strategy trade freely

## Hold On Trend Feature

When enabled, positions aligned with the trend won't take profits:

- **Bull trend + Long position** = Hold until stop loss or trend expires
- **Bear trend + Short position** = Hold until stop loss or trend expires
- **Counter-trend positions** = Normal profit taking applies

Example:
```bash
# Set bull trend and hold longs for 24 hours
python user_data/trend_control.py set bull --hours 24
python user_data/trend_control.py hold on --hours 24
```

## Cache File Location

All settings are stored in: `user_data/trend_direction_cache.json`

This file is automatically created/updated by the controller and read by your strategy.

## Integration with freqtrade-telegram

To enable Telegram bot integration, you need to:

1. Set environment variables:
```bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
```

2. Create a bot startup script (see `user_data/telegram_commands_implementation.py`)

3. Run the bot alongside your freqtrade instance

## Examples

### Example 1: Force Long Bias During Expected Rally
```bash
# Expecting a strong rally, only want longs for next 6 hours
python user_data/trend_control.py set bull --hours 6

# Also hold positions without taking profits
python user_data/trend_control.py hold on --hours 6

# Check status
python user_data/trend_control.py status
```

### Example 2: Reduce Risk During Uncertainty
```bash
# Set neutral to reduce position sizing
python user_data/trend_control.py set neutral --hours 12

# Tighter stop loss
python user_data/trend_control.py set sl 5 --hours 12  # 5% stop
```

### Example 3: Clear All Overrides
```bash
# Back to normal strategy behavior
python user_data/trend_control.py clear
python user_data/trend_control.py clearrisk
```

## Tips

1. **Start Conservative**: Begin with shorter durations (1-4 hours) to test behavior
2. **Monitor Results**: Use `/status` frequently to check remaining time
3. **Don't Forget**: Overrides expire automatically, but you can clear them manually
4. **Combine Carefully**: Using bull + hold + tight stop can maximize trend following
5. **Test First**: Try commands in dry-run mode before live trading

## Troubleshooting

### Trend not applying to strategy
- Verify the cache file exists: `cat user_data/trend_direction_cache.json`
- Check strategy imports trend_helper correctly
- Ensure strategy is checking trend before entries
- Restart strategy after making changes

### Cache file permission errors
```bash
# Fix permissions
chmod 644 user_data/trend_direction_cache.json
```

### Expired cache still showing
```bash
# Manually clear
python user_data/trend_control.py clear
```

## Safety Notes

⚠️ **Important Safety Reminders:**

1. Manual overrides affect ALL trading pairs
2. Settings persist until expired or manually cleared
3. Always verify settings with `/status` before trading
4. Use shorter durations initially (1-6 hours)
5. Monitor positions closely when using overrides
6. Test in dry-run mode first
7. Keep a record of when you set overrides
8. Don't forget to clear after trading session ends

## Architecture

```
┌─────────────────────────────────────────┐
│  CLI / Telegram Bot Commands            │
│  (user_data/trend_control.py)           │
└─────────────────┬───────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────┐
│  Trend Direction Controller             │
│  (utils/telegram_trend_controller.py)   │
│  - Manages cache file                   │
│  - Handles expiration                   │
│  - Thread-safe operations               │
└─────────────────┬───────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────┐
│  Cache File (JSON)                      │
│  user_data/trend_direction_cache.json   │
│  - direction: bull/bear/neutral         │
│  - stoploss: override value             │
│  - hold_on_trend: boolean               │
│  - expires_at: timestamp                │
└─────────────────┬───────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────┐
│  Trend Helper (utils/trend_helper.py)   │
│  - Simple API for strategies            │
│  - should_enter_long()                  │
│  - should_enter_short()                 │
│  - get_stoploss_override()              │
│  - should_hold_on_trend()               │
└─────────────────┬───────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────┐
│  Your Trading Strategy                  │
│  - Checks trend before entries          │
│  - Applies stoploss override            │
│  - Implements hold logic                │
└─────────────────────────────────────────┘
```
