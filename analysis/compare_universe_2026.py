import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import date
import pandas as pd
from core.config import CONFIG
from backtesting.engine import run_backtest

START = date(2026, 1, 2)
END   = date(2026, 4, 25)

NEW_STOCKS = [
    "ORCL", "CRM", "ADBE", "AVGO", "QCOM", "NOW", "NFLX", "UBER", "MU", "INTC",
    "MA", "MS", "BLK", "AXP", "WFC", "SPGI",
    "LLY", "ABBV", "MRK", "TMO", "ISRG", "ABT",
    "MCD", "SBUX", "BKNG", "TGT", "LOW", "COST", "MAR",
    "RTX", "GE", "DE", "UPS", "LMT", "BA",
    "TMUS", "VZ", "COP",
]

def fmt(r):
    ret = r.total_return_pct
    trades = len(r.trades_df)
    final = r.equity_df["total_eur"].iloc[-1] if not r.equity_df.empty else 0
    initial = r.initial_capital_eur
    return f"  return={ret:+.2f}%  trades={trades}  final=€{final:,.0f}  initial=€{initial:,.0f}"

def monthly_equity(r):
    """Return equity at end of Jan, Feb, Mar, Apr 2026."""
    if r.equity_df.empty:
        return {}
    df = r.equity_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    result = {}
    for (label, year, month) in [
        ("End Jan", 2026, 1),
        ("End Feb", 2026, 2),
        ("End Mar", 2026, 3),
        ("End Apr", 2026, 4),
    ]:
        month_data = df[(df["date"].dt.year == year) & (df["date"].dt.month == month)]
        if not month_data.empty:
            val = month_data.iloc[-1]["total_eur"]
            result[label] = val
        else:
            result[label] = None
    return result

def print_monthly(label, r):
    me = monthly_equity(r)
    print(f"  {label} monthly equity:")
    for k, v in me.items():
        if v is not None:
            print(f"    {k}: €{v:,.0f}")
        else:
            print(f"    {k}: (no data)")

# ── CURRENT UNIVERSE ──────────────────────────────────────────────────────────
print("=" * 60)
print("Running backtest with CURRENT universe...")
print("=" * 60)
r7_cur = run_backtest(7, START, END)
print(f"Bot 7  (RSI Compounder): {fmt(r7_cur)}")
print(f"  sharpe={r7_cur.sharpe:.3f}  max_drawdown={r7_cur.max_drawdown:+.2%}")
print_monthly("Bot 7 current", r7_cur)
if r7_cur.errors:
    print(f"  ERRORS ({len(r7_cur.errors)}): {r7_cur.errors[:3]}")

r10_cur = run_backtest(10, START, END)
print(f"Bot 10 (Trend Momentum): {fmt(r10_cur)}")
print(f"  sharpe={r10_cur.sharpe:.3f}  max_drawdown={r10_cur.max_drawdown:+.2%}")
print_monthly("Bot 10 current", r10_cur)
if r10_cur.errors:
    print(f"  ERRORS ({len(r10_cur.errors)}): {r10_cur.errors[:3]}")

combined_cur = (r7_cur.total_return_pct + r10_cur.total_return_pct) / 2
print(f"Combined avg return:      {combined_cur:+.2f}%")

# ── EXPANDED UNIVERSE ─────────────────────────────────────────────────────────
# Temporarily extend stocks_us in the live CONFIG cache
orig = list(CONFIG.watchlists["stocks_us"])
CONFIG.watchlists["stocks_us"].extend(NEW_STOCKS)

print()
print("=" * 60)
print("Running backtest with EXPANDED universe...")
print("=" * 60)
r7_exp = run_backtest(7, START, END)
print(f"Bot 7  (RSI Compounder): {fmt(r7_exp)}")
print(f"  sharpe={r7_exp.sharpe:.3f}  max_drawdown={r7_exp.max_drawdown:+.2%}")
print_monthly("Bot 7 expanded", r7_exp)
if r7_exp.errors:
    print(f"  ERRORS ({len(r7_exp.errors)}): {r7_exp.errors[:3]}")

r10_exp = run_backtest(10, START, END)
print(f"Bot 10 (Trend Momentum): {fmt(r10_exp)}")
print(f"  sharpe={r10_exp.sharpe:.3f}  max_drawdown={r10_exp.max_drawdown:+.2%}")
print_monthly("Bot 10 expanded", r10_exp)
if r10_exp.errors:
    print(f"  ERRORS ({len(r10_exp.errors)}): {r10_exp.errors[:3]}")

combined_exp = (r7_exp.total_return_pct + r10_exp.total_return_pct) / 2
print(f"Combined avg return:      {combined_exp:+.2f}%")

# Restore
CONFIG.watchlists["stocks_us"][:] = orig

# ── COMPARISON ─────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("=== COMPARISON ===")
print("=" * 60)
print(f"Bot 7  current={r7_cur.total_return_pct:+.2f}%  expanded={r7_exp.total_return_pct:+.2f}%  delta={r7_exp.total_return_pct - r7_cur.total_return_pct:+.2f}%")
print(f"Bot 10 current={r10_cur.total_return_pct:+.2f}%  expanded={r10_exp.total_return_pct:+.2f}%  delta={r10_exp.total_return_pct - r10_cur.total_return_pct:+.2f}%")
print(f"Combo  current={combined_cur:+.2f}%  expanded={combined_exp:+.2f}%  delta={combined_exp - combined_cur:+.2f}%")

# ── WHICH NEW STOCKS WERE ACTUALLY TRADED? ─────────────────────────────────────
print()
print("=" * 60)
print("=== NEW STOCKS ACTUALLY TRADED (Expanded universe) ===")
print("=" * 60)
for label, r in [("Bot 7", r7_exp), ("Bot 10", r10_exp)]:
    traded = set(r.trades_df["ticker"].unique()) if not r.trades_df.empty else set()
    new_traded = [t for t in NEW_STOCKS if t in traded]
    print(f"{label}: {sorted(new_traded)}")
    # Also show trade counts for new stocks
    if new_traded and not r.trades_df.empty:
        for ticker in sorted(new_traded):
            tc = r.trades_df[r.trades_df["ticker"] == ticker]
            buys = len(tc[tc["side"] == "BUY"])
            sells = len(tc[tc["side"] == "SELL"])
            print(f"  {ticker}: {buys} buys, {sells} sells")

# ── ALL TRADED TICKERS (both scenarios) ────────────────────────────────────────
print()
print("=== ALL TRADED TICKERS (current universe) ===")
for label, r in [("Bot 7", r7_cur), ("Bot 10", r10_cur)]:
    traded = sorted(r.trades_df["ticker"].unique()) if not r.trades_df.empty else []
    print(f"{label}: {traded}")

print()
print("=== ALL TRADED TICKERS (expanded universe) ===")
for label, r in [("Bot 7", r7_exp), ("Bot 10", r10_exp)]:
    traded = sorted(r.trades_df["ticker"].unique()) if not r.trades_df.empty else []
    print(f"{label}: {traded}")
