# Migration Guide: user_data to freqtrade/rpc/trend_control

## What Changed

The trend control utilities have been moved from `user_data/utils/` to `freqtrade/rpc/trend_control/` so they can be committed to git and shared across the project.

## File Locations

### Old Locations (gitignored)
```
user_data/utils/telegram_trend_controller.py  ❌ Gitignored
user_data/utils/trend_helper.py                ❌ Gitignored
user_data/TREND_CONTROLLER_USAGE.md           ❌ Gitignored
```

### New Locations (tracked by git)
```
freqtrade/rpc/trend_control/
├── __init__.py                    ✅ New module init
├── trend_controller.py            ✅ Core controller (clean, no telegram dependency)
└── trend_helper.py                ✅ Strategy helpers

docs/trend_control/
├── README.md                      ✅ Module documentation
├── USAGE.md                       ✅ Complete usage guide
├── QUICK_REFERENCE.md             ✅ Quick reference
├── INTEGRATION.md                 ✅ Integration guide
├── SAFETY_ANALYSIS.md             ✅ Telegram command safety
└── safe_commands_example.py       ✅ Safe command examples
```

### Files That Stay in user_data (user-specific)
```
user_data/trend_control.py                     ✅ CLI tool (can stay)
user_data/trend_direction_cache.json           ✅ Cache file (auto-created)
user_data/utils/telegram_trend_controller.py   ✅ Full telegram version (if you want)
```

## Update Your Imports

### In Your Strategies

**Old:**
```python
from user_data.utils.trend_helper import (
    should_enter_long,
    should_enter_short,
    get_trend_direction
)
```

**New:**
```python
from freqtrade.rpc.trend_control import (
    should_enter_long,
    should_enter_short,
    get_trend_direction
)
```

### Using the Controller

**Old:**
```python
from user_data.utils.telegram_trend_controller import get_trend_controller
```

**New:**
```python
from freqtrade.rpc.trend_control import get_trend_controller
```

### In CLI Tool (user_data/trend_control.py)

Update the import at the top:

**Old:**
```python
from user_data.utils.telegram_trend_controller import get_trend_controller
```

**New:**
```python
from freqtrade.rpc.trend_control import get_trend_controller
```

## What's Different

### Simplified Controller

The new `trend_controller.py` in `freqtrade/rpc/trend_control/` is a **clean version without Telegram dependencies**:

- ✅ Core trend control functionality
- ✅ Thread-safe cache operations
- ✅ No telegram library required
- ✅ Works standalone
- ✅ Can be imported anywhere

### Full Telegram Version

If you need the full Telegram bot integration, you can keep `user_data/utils/telegram_trend_controller.py` - it has all the Telegram command handlers. Just update its imports to use the new module:

```python
# In user_data/utils/telegram_trend_controller.py
from freqtrade.rpc.trend_control import get_trend_controller
```

## Migration Steps

### 1. Update Strategy Imports

Find all your strategies that use trend control:

```bash
grep -r "from user_data.utils.trend_helper" user_data/strategies/
```

Update them to:
```python
from freqtrade.rpc.trend_control import should_enter_long, should_enter_short
```

### 2. Update CLI Tool

Edit `user_data/trend_control.py` and update the import (if using the simplified version):

```python
# At the top, change this:
from user_data.utils.telegram_trend_controller import get_trend_controller

# To this:
from freqtrade.rpc.trend_control import get_trend_controller
```

### 3. Test Everything

```bash
# Test the CLI
python user_data/trend_control.py status

# Test in Python
python3 -c "from freqtrade.rpc.trend_control import get_trend_direction; print(get_trend_direction())"

# Test your strategy
freqtrade backtesting --strategy YourStrategy
```

### 4. Commit to Git

```bash
# Add the new files
git add freqtrade/rpc/trend_control/
git add docs/trend_control/

# Commit
git commit -m "Add trend control module to freqtrade RPC"

# Push
git push
```

## Advantages of New Structure

✅ **Tracked by Git** - All team members get the same code
✅ **Proper Module** - Clean import paths
✅ **No Telegram Dependency** - Core module works standalone
✅ **Better Organization** - RPC-related code in RPC directory
✅ **Documentation** - Properly documented in docs/
✅ **Reusable** - Can be used by other freqtrade components

## Backward Compatibility

The old files in `user_data/utils/` will still work if you keep them. You can gradually migrate by:

1. Keep both old and new imports working
2. Update strategies one by one
3. Eventually remove old files from user_data/utils/

## Questions?

- Old imports still work? **Yes, if you keep the files in user_data/utils/**
- Do I need to change anything? **Yes, update imports to use new module**
- Will cache file location change? **No, still user_data/trend_direction_cache.json**
- Can I delete user_data/utils files? **Yes, after updating all imports**

## Summary

| Item | Old Location | New Location | Action |
|------|-------------|--------------|---------|
| Core Controller | `user_data/utils/` | `freqtrade/rpc/trend_control/` | ✅ Moved |
| Helper Functions | `user_data/utils/` | `freqtrade/rpc/trend_control/` | ✅ Moved |
| Documentation | `user_data/` | `docs/trend_control/` | ✅ Moved |
| CLI Tool | `user_data/` | `user_data/` | ⚡ Update imports |
| Cache File | `user_data/` | `user_data/` | ✅ No change |
| Telegram Bot | `user_data/utils/` | Optional | ⚡ Keep if needed |

**Next Step:** Update your imports and test!
