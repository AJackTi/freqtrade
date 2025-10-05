# Telegram Trend Control - Quick Reference

## 🚀 Most Used Commands

```
/trendset bull 24        # Only LONGS for 24 hours
/trendset bear 12        # Only SHORTS for 12 hours
/trendset neutral 6      # Both directions for 6 hours

/longall                 # Quick bull (24h)
/shortall                # Quick bear (24h)

/trendstatus             # Check what's active
/trendclear              # Clear everything
```

## 📊 What Each Trend Does

| Command | Longs | Shorts | Use When |
|---------|-------|--------|----------|
| `/trendset bull` | ✅ Opens | ❌ Blocked | Expecting pump |
| `/trendset bear` | ❌ Blocked | ✅ Opens | Expecting dump |
| `/trendset neutral` | ✅ Opens | ✅ Opens | Uncertain market |
| `/trendclear` | ✅ Opens | ✅ Opens | Back to normal |

## 🎯 Quick Scenarios

### 📈 Expecting Market Rally
```
/trendset bull 24
/trendhold on 24
```
→ Only longs, hold positions without profit exits

### 📉 Expecting Market Crash
```
/shortall
/trendhold on 24
```
→ Only shorts, hold positions

### ⚖️ Uncertain Market (Reduce Risk)
```
/trendset neutral 12
```
→ Both directions allowed

### 🔄 Back to Normal
```
/trendclear
```
→ Strategy uses normal logic

## 💻 Strategy Code (Add This Once)

```python
from user_data.utils.trend_helper import (
    should_enter_long,
    should_enter_short,
    should_hold_on_trend
)

def populate_entry_trend(self, dataframe, metadata):
    conditions_long = [...]
    conditions_short = [...]

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
    if should_hold_on_trend(trade.is_short):
        return None
    return None
```

## 📋 All Commands

| Command | Purpose | Example |
|---------|---------|---------|
| `/trendset <dir> [hrs]` | Set trend | `/trendset bull 12` |
| `/longall` | Quick bull (24h) | `/longall` |
| `/shortall` | Quick bear (24h) | `/shortall` |
| `/trendneutral` | Quick neutral (24h) | `/trendneutral` |
| `/trendget` | Show current trend | `/trendget` |
| `/trendstatus` | Detailed status | `/trendstatus` |
| `/trendhold on [hrs]` | Hold positions | `/trendhold on 24` |
| `/trendhold off` | Stop holding | `/trendhold off` |
| `/trendclear` | Clear all settings | `/trendclear` |

## ⚡ Pro Tips

1. **Start small** - Use 1-6 hour durations first
2. **Check often** - Use `/trendstatus` to monitor
3. **Auto-expires** - Settings clear automatically after duration
4. **Test first** - Try in dry-run before live
5. **Keep notes** - Record when and why you set trends

## 🔗 Full Documentation

- **[TELEGRAM_CLI.md](TELEGRAM_CLI.md)** - Complete Telegram command guide
- **[INTEGRATION.md](INTEGRATION.md)** - Strategy integration details
- **[SAFETY_ANALYSIS.md](SAFETY_ANALYSIS.md)** - Risk and safety info

---

## 🔄 Default State

**No trend set = Neutral forever:**
- `should_enter_long()` → `True` ✅
- `should_enter_short()` → `True` ✅
- Both directions allowed indefinitely
- Strategy uses normal logic

**After `/trendset bull 24`:**
- `should_enter_long()` → `True` ✅
- `should_enter_short()` → `False` ❌
- Longs only for 24 hours
- Auto-reverts to default after expiration
