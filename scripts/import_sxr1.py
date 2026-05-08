"""One-time: import SXR1 manual position from IBKR into SQLite."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.reconciliation import import_manual_positions, reconcile_positions
from core.db import get_session, Position, Trade

n = import_manual_positions([7, 10], ibkr_port=4002, primary_bot_id=7)
print(f"Imported: {n} manual position(s)")

with get_session() as s:
    positions = s.query(Position).filter(Position.bot_id.in_([7, 10])).all()
    print("SQLite positions now:")
    for p in positions:
        print(f"  bot={p.bot_id} {p.ticker} qty={p.qty} avg=EUR{p.avg_entry_eur:.2f}")
    manual = s.query(Trade).filter(Trade.order_type == "MANUAL").all()
    print("Manual trades:")
    for t in manual:
        print(f"  bot={t.bot_id} {t.side} {t.qty} {t.ticker} @ EUR{t.price_eur:.2f} -- {t.signal_reason}")

print()
print("Reconciliation after import:")
disc = reconcile_positions([7, 10], ibkr_port=4002)
if not disc:
    print("  All clear - SQLite and IBKR match")
else:
    for d in disc:
        print(f"  {d.ticker}: SQLite={d.sqlite_qty} IBKR={d.ibkr_qty} [{d.severity}]")
