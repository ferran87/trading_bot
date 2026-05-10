"""Quick validation of config/ai_thesis_universe.yaml."""
import sys
from pathlib import Path
from collections import Counter
sys.path.insert(0, str(Path(__file__).parents[1]))

import yaml

data = yaml.safe_load(Path("config/ai_thesis_universe.yaml").read_text(encoding="utf-8"))
tickers = data["tickers"]

seen = {}
dupes = []
for t in tickers:
    if t["ticker"] in seen:
        dupes.append(t["ticker"])
    seen[t["ticker"]] = True

print(f"Total entries : {len(tickers)}")
print(f"Unique tickers: {len(seen)}")
print(f"Duplicates    : {dupes if dupes else 'none'}")
print()

themes = Counter(t.get("theme", "untagged") for t in tickers)
print("By theme:")
for theme, count in sorted(themes.items(), key=lambda x: -x[1]):
    print(f"  {theme}: {count}")
print()

regions = Counter(t.get("region", "?") for t in tickers)
print("By region:")
for r, c in sorted(regions.items()):
    print(f"  {r}: {c}")
print()

caps = Counter(t.get("market_cap", "large") for t in tickers)
print("By market cap:")
for cap, count in sorted(caps.items(), key=lambda x: -x[1]):
    print(f"  {cap}: {count}")
