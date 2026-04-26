"""Run the actual backtest for Bot 6 with verbose logging to see what blocks trades."""
import logging
import sys
from datetime import date

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Suppress noisy libraries
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.getLogger("peewee").setLevel(logging.WARNING)

sys.path.insert(0, ".")

from backtesting.engine import run_backtest

result = run_backtest(
    bot_id=6,
    start_date=date(2025, 4, 7),
    end_date=date(2025, 4, 15),
)

print("\n=== RESULT ===")
print(f"Trades: {len(result.trades_df)}")
print(f"Return: {result.total_return_pct*100:.2f}%")
if not result.trades_df.empty:
    print(result.trades_df.to_string())
if result.errors:
    print("ERRORS:")
    for e in result.errors:
        print(" ", e)
