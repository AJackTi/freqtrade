# Telegram Trend Control CLI

Quick reference for controlling trend direction via Telegram bot.

## 🎯 Quick Commands

### Set Trend Direction
```
/trendset bull 24        # Only LONGS for 24 hours
/trendset bear 12        # Only SHORTS for 12 hours
/trendset neutral 6      # Both allowed for 6 hours
```

### Quick Actions
```
/longall                 # Bull trend (24h) - fast command
/shortall                # Bear trend (24h) - fast command
/trendneutral            # Neutral trend (24h)
```

### Check Status
```
/trendget                # Show current trend
/trendstatus             # Detailed status with timing
```

### Clear Settings
```
/trendclear              # Clear trend direction
```

### Hold on Trend
```
/trendhold on 24         # Hold positions aligned with trend (24h)
/trendhold off           # Disable holding
```

---

## 📋 Command Details

### `/trendset <direction> [hours]`
Set trend direction with optional duration.

**Directions:**
- `bull` - Only open LONG positions (blocks shorts)
- `bear` - Only open SHORT positions (blocks longs)
- `neutral` - Both directions allowed

**Examples:**
```
/trendset bull           # Bull for 24h (default)
/trendset bear 6         # Bear for 6 hours
/trendset neutral 12     # Neutral for 12 hours
```

**Max duration:** 168 hours (1 week)

---

### `/longall` & `/shortall`
Quick commands for instant trend setting (24 hours).

```
/longall                 # Same as: /trendset bull 24
/shortall                # Same as: /trendset bear 24
```

**Use when:** You need to quickly switch to one direction.

---

### `/trendget`
Show current active trend.

**Output:**
- Current direction (bull/bear/neutral)
- Time remaining
- Quick link to detailed status

```
/trendget
📈 Current Trend: BULLISH (Longs only)
⏱ Time remaining: 23:45:12
```

---

### `/trendstatus`
Detailed trend control status.

**Shows:**
- Direction
- Stop loss override (if set)
- Hold on trend status
- Time remaining
- Set timestamp
- Quick actions

---

### `/trendhold <on|off> [hours]`
Control profit-taking for positions aligned with trend.

**When enabled:**
- Bull trend + Long position = Hold (no profit exit)
- Bear trend + Short position = Hold (no profit exit)
- Counter-trend positions = Normal profit taking

**Examples:**
```
/trendhold on 24         # Hold for 24 hours
/trendhold on 12         # Hold for 12 hours
/trendhold off           # Disable holding
```

**Use when:** You want to ride trends without taking profits.

---

### `/trendclear`
Clear all trend settings immediately.

```
/trendclear
✅ Trend direction cleared
Strategy will now use its normal logic.
```

**Effect:** Bot returns to normal strategy behavior (both directions allowed).

---

## 🔄 Workflow Examples

### Example 1: Expecting Market Rally
```bash
# Set bull trend + hold positions
/trendset bull 24
/trendhold on 24

# Check status
/trendstatus

# When done
/trendclear
```

### Example 2: Quick Market Crash Response
```bash
# Immediately switch to shorts only
/shortall

# Check what's active
/trendget

# Clear when market stabilizes
/trendclear
```

### Example 3: Uncertain Market (Reduce Risk)
```bash
# Allow both but signal uncertainty
/trendset neutral 12

# Check in 12 hours, will auto-expire
```

---

## ⚙️ How It Works

1. **Telegram Command** → Sets trend in cache file
2. **Strategy reads cache** → Blocks/allows entries
3. **Auto-expires** → Returns to normal after duration

**Cache location:** `user_data/trend_direction_cache.json`

---

## ✅ Strategy Integration

Add to your strategy:

```python
from user_data.utils.trend_helper import (
    should_enter_long,
    should_enter_short,
    should_hold_on_trend
)

def populate_entry_trend(self, dataframe, metadata):
    # Your conditions here
    conditions_long = [...]
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
    # Hold if aligned with trend
    if should_hold_on_trend(trade.is_short):
        return None

    # Normal exit logic
    return None
```

---

## ⚠️ Safety Tips

1. **Start with short durations** (1-6 hours) to test
2. **Use `/trendstatus` frequently** to check settings
3. **Settings auto-expire** - no need to manually clear (but you can)
4. **Test in dry-run first** before live trading
5. **Monitor positions closely** when using trend control
6. **Keep a trading journal** - note when you set trends and why

---

## 🚀 Quick Start

1. **Set trend via Telegram:**
   ```
   /trendset bull 24
   ```

2. **Verify it's working:**
   ```
   /trendstatus
   ```

3. **Strategy automatically:**
   - Opens only long positions
   - Blocks all short entries
   - Auto-expires after 24 hours

4. **Clear when done:**
   ```
   /trendclear
   ```

---

## 📊 Command Summary Table

| Command | Purpose | Duration | Example |
|---------|---------|----------|---------|
| `/trendset bull [hrs]` | Longs only | Custom (default 24h) | `/trendset bull 12` |
| `/trendset bear [hrs]` | Shorts only | Custom (default 24h) | `/trendset bear 6` |
| `/trendset neutral [hrs]` | Both directions | Custom (default 24h) | `/trendset neutral 8` |
| `/longall` | Quick bull | 24h fixed | `/longall` |
| `/shortall` | Quick bear | 24h fixed | `/shortall` |
| `/trendneutral` | Quick neutral | 24h fixed | `/trendneutral` |
| `/trendget` | Check trend | - | `/trendget` |
| `/trendstatus` | Detailed info | - | `/trendstatus` |
| `/trendhold on [hrs]` | Hold positions | Custom (default 24h) | `/trendhold on 12` |
| `/trendhold off` | Stop holding | Immediate | `/trendhold off` |
| `/trendclear` | Clear all | Immediate | `/trendclear` |

---

## 🔗 Related Docs

- [Strategy Integration](INTEGRATION.md) - How to add to your strategy
- [Safety Analysis](SAFETY_ANALYSIS.md) - Risk considerations
- [Full Usage Guide](USAGE.md) - Complete documentation

---

## 🔄 Default Behavior

**When NO trend is set (default state):**
- `should_enter_long()` → `True` ✅
- `should_enter_short()` → `True` ✅
- **Effect:** Neutral forever - both directions allowed indefinitely
- **Strategy uses:** Normal logic, no restrictions

This is the safe default - your strategy works normally until you explicitly set a trend via Telegram.
