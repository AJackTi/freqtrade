# Import Notes

## Recommended Import Methods

### Method 1: Direct Module Import (Recommended for Strategies)

Use the full module path to import directly:

```python
from freqtrade.rpc.trend_control.trend_helper import (
    should_enter_long,
    should_enter_short,
    get_trend_direction
)

from freqtrade.rpc.trend_control.trend_controller import get_trend_controller
```

**Advantages:**
- ✅ Works in all Python versions
- ✅ Explicit and clear
- ✅ No dependency on RPC package init

### Method 2: Package-Level Import

If freqtrade is running with Python 3.11+:

```python
from freqtrade.rpc.trend_control import (
    should_enter_long,
    should_enter_short,
    get_trend_direction,
    get_trend_controller
)
```

**Advantages:**
- ✅ Cleaner imports
- ✅ Uses package __init__

**Requirements:**
- Requires Python 3.11+ (for freqtrade.rpc compatibility)

## For Strategy Files

In your strategy files, use Method 1 (direct module import):

```python
# user_data/strategies/MyStrategy.py

from freqtrade.strategy import IStrategy
from freqtrade.rpc.trend_control.trend_helper import (
    should_enter_long,
    should_enter_short,
    get_trend_direction
)

class MyStrategy(IStrategy):

    def populate_entry_trend(self, dataframe, metadata):
        # Use trend control
        if should_enter_long():
            dataframe.loc[
                (dataframe['signal'] == 1),
                'enter_long'
            ] = 1

        if should_enter_short():
            dataframe.loc[
                (dataframe['signal'] == -1),
                'enter_short'
            ] = 1

        return dataframe
```

## For CLI Tools

In CLI tools like `user_data/trend_control.py`:

```python
#!/usr/bin/env python3
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import from the module
from freqtrade.rpc.trend_control.trend_controller import get_trend_controller

controller = get_trend_controller()
```

## Testing Imports

Test that imports work:

```bash
# Test direct import
cd freqtrade/rpc/trend_control
python3 -c "from trend_helper import get_trend_direction; print(get_trend_direction())"

# Test from project root
python3 -c "from freqtrade.rpc.trend_control.trend_helper import get_trend_direction; print(get_trend_direction())"
```

## Troubleshooting

### Error: "cannot import name 'UTC' from 'datetime'"

This happens when importing through `freqtrade.rpc` package with Python < 3.11.

**Solution:** Use direct module imports (Method 1) instead of package imports.

```python
# Instead of this:
from freqtrade.rpc.trend_control import should_enter_long  # May fail on Python 3.10

# Use this:
from freqtrade.rpc.trend_control.trend_helper import should_enter_long  # Works always
```

### Error: "No module named 'freqtrade'"

Your Python path doesn't include the project root.

**Solution:** Add project root to sys.path:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
```

## Summary

| Import Style | Python Version | Recommended For | Status |
|-------------|----------------|-----------------|---------|
| Direct module import | All | Strategies, CLI tools | ✅ Recommended |
| Package import | 3.11+ | Application code | ✅ Optional |
| user_data.utils import | All | Legacy only | ⚠️ Deprecated |

**Best Practice:** Use direct module imports for maximum compatibility.
