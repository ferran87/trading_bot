"""Smoke test for the new earnings tools (next_earnings_date + history)."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))

from agents.pm_tools import get_fundamentals, get_recent_earnings_history

f = json.loads(get_fundamentals("ANET"))
print("=== fundamentals (earnings calendar fields) ===")
for k in [
    "next_earnings_date",
    "next_earnings_eps_estimate",
    "next_earnings_revenue_estimate",
    "next_earnings_date_is_estimate",
]:
    print(f"  {k}: {f.get(k)}")

print()
h = json.loads(get_recent_earnings_history("ANET", 8))
print("=== earnings history ===")
print(f"  beats={h['beats']} / misses={h['misses']} / inline={h['inline']}")
print(f"  avg_surprise_pct={h['average_surprise_pct']}")
print(f"  quarters_with_results={h['quarters_with_results']}")
print(f"  first 4 rows:")
for r in h["history"][:4]:
    print(f"    {r}")
