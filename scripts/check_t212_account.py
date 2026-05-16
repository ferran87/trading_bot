"""Quick check of T212 demo account — portfolio, cash, and recent orders."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

import requests
from core.t212_auth import resolve_t212_credentials, t212_basic_auth_header

key, secret = resolve_t212_credentials(demo=True, owner="ferran")
headers = {"Authorization": t212_basic_auth_header(key, secret)}
BASE = "https://demo.trading212.com"

# ── Account summary ───────────────────────────────────────────────────────────
r = requests.get(f"{BASE}/api/v0/equity/account/summary", headers=headers, timeout=10)
print(f"Account summary [{r.status_code}]:")
if r.ok:
    s = r.json()
    print(json.dumps(s, indent=2))
else:
    print(r.text)

# ── Portfolio ─────────────────────────────────────────────────────────────────
r = requests.get(f"{BASE}/api/v0/equity/portfolio", headers=headers, timeout=10)
print(f"\nPortfolio [{r.status_code}]:")
if r.ok:
    positions = r.json()
    if not positions:
        print("  (empty)")
    for p in positions:
        ticker  = p.get("ticker", "")
        qty     = p.get("quantity", 0)
        avg     = p.get("averagePrice", 0)
        curr    = p.get("currentPrice", 0)
        ppl     = p.get("ppl", 0)
        ccy     = p.get("currency", "")
        print(f"  {ticker:20s}  qty={qty:8.4f}  avg={avg:10.4f}  curr={curr:10.4f}  ppl={ppl:+8.2f}  {ccy}")
else:
    print(r.text)

# ── Recent orders ─────────────────────────────────────────────────────────────
r = requests.get(f"{BASE}/api/v0/equity/orders", headers=headers, timeout=10)
print(f"\nOpen orders [{r.status_code}]:")
if r.ok:
    orders = r.json()
    if not orders:
        print("  (none)")
    for o in orders:
        print(f"  id={o.get('id')}  {o.get('ticker')}  {o.get('type')}  status={o.get('status')}  qty={o.get('quantity')}  filledQty={o.get('filledQuantity')}")
else:
    print(r.text)

# ── Order history (filled orders) ────────────────────────────────────────────
r = requests.get(f"{BASE}/api/v0/equity/history/orders", headers=headers, timeout=10, params={"limit": 20})
print(f"\nOrder history [{r.status_code}]:")
if r.ok:
    hist = r.json()
    items = hist.get("items", hist) if isinstance(hist, dict) else hist
    if not items:
        print("  (none)")
    for o in items:
        taxes = o.get("taxes", [])
        fee   = sum(float(t.get("quantity") or t.get("value") or 0) for t in taxes)
        print(f"  {o.get('dateExecuted','')[:19]}  {o.get('ticker',''):20s}  "
              f"{o.get('type',''):4s}  status={o.get('status',''):8s}  "
              f"qty={o.get('filledQuantity',0):8.4f}  "
              f"price={o.get('fillPrice') or o.get('fillResult',{}).get('price',0):10.4f}  "
              f"taxes={fee:.4f}")
else:
    print(r.text)
