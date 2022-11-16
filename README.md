Archived copy after ftx modifications.
# Jesse Strategy Template Extended
[![Sourcery](https://img.shields.io/badge/Sourcery-enabled-brightgreen)](https://sourcery.ai)

Migrated from k3_base 0.2.4 strategysd

If self.use_initial_balance is True then
1 - the strategy will use the initial balance as the starting position
2 - calculate available margin based on the initial balance.
Don't use self.use_initial_balance and save profits together.

Update:
Add avail_margin, margin balance != avail margin.

Update:
Add liquidation price calculation.

0.1.3: Add liquidation price / price rate calculation.

0.1.4:

0.1.5: Rename self.capital to self.balance for compatibility with new jesse.

0.1.6:

0.1.7:

0.1.8:
Add pnl to discord wallet message.

0.1.9:
Add Alex Lau's position.entry_price fix for persistency.

0.2.0:
Add fix for insufficient margin counts and max insufficent margin counts.
Add JesseTradingViewLightReport.


### Installation
```bash
pip install -e .
```

### How to use
Modify Strategy import

from:
```python
from strategysd import Strategysd
```

to:

```python
from strat import Strat
```

and use the new class name

```python
class k3_base(Strategysd) -> class k3_base(Strat)
```

## License

[MIT](https://choosealicense.com/licenses/mit/)
