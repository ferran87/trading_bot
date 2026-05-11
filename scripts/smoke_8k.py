"""Smoke test for get_recent_8k_filings — US ANET should return earnings text;
ASML.AS should return a graceful 'not US-listed' message."""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))

from agents.pm_tools import get_recent_8k_filings

print("=== ANET (US, expect earnings 8-K) ===")
data = json.loads(get_recent_8k_filings("ANET", days=90, limit=3))
print(f"  filings returned: {len(data.get('filings', []))}")
for f in data.get("filings", []):
    print(f"  - {f['filing_date']} items={f['items']} has_earnings={f['has_earnings']}")
    if f["has_earnings"] and f["earnings_text"]:
        first_chars = f["earnings_text"][:300].replace("\n", " ")
        print(f"    first 300 chars: {first_chars}...")
    if f["key_metrics"]:
        print(f"    key_metrics: {f['key_metrics']}")

print()
print("=== ASML.AS (EU, expect skip message) ===")
data = json.loads(get_recent_8k_filings("ASML.AS", days=90, limit=3))
print(f"  message: {data.get('message')}")

print()
print("=== bogus ticker ===")
data = json.loads(get_recent_8k_filings("ZZZZZ", days=90, limit=3))
print(f"  result: {data}")
