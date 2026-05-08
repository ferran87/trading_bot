from core.db import get_session, Trade, Position, EquitySnapshot, RunLog
from core.config import CONFIG
print("DB:", CONFIG.db_url[:60])
with get_session() as s:
    trades = s.query(Trade).all()
    positions = s.query(Position).all()
    runlogs = s.query(RunLog).all()
    snaps = s.query(EquitySnapshot).count()
    print(f"Trades: {len(trades)}, Positions: {len(positions)}, RunLogs: {len(runlogs)}, Snapshots: {snaps}")
    for t in trades:
        print(f"  TRADE bot={t.bot_id} {t.ticker} {t.side} qty={t.qty}")
    for p in positions:
        print(f"  POS   bot={p.bot_id} {p.ticker} qty={p.qty}")
    for r in runlogs:
        print(f"  LOG   bot={r.bot_id} buys={r.n_buys} sells={r.n_sells} summary={r.summary}")
