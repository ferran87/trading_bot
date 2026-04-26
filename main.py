"""Entry point. Called by Windows Task Scheduler twice per day.

Usage:
    python main.py --once
    python main.py --once --as-of 2026-04-17 --force-rebalance   # weekend, Friday closes
    python main.py --reset-virtual-book 1 --yes                # wipe bot 1 trades/positions in SQLite
    python main.py --init-db
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import date, datetime

from core.config import CONFIG, LOG_DIR
from core.db import init_db


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = CONFIG.settings["logging"].get("level", "INFO")
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(fmt)
    root.addHandler(stdout)

    daily_log = LOG_DIR / f"{date.today().isoformat()}.log"
    fh = logging.FileHandler(daily_log, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Like --once but skips weekends and skips if already ran today (for Task Scheduler).",
    )
    parser.add_argument("--init-db", action="store_true", help="Create tables and seed bots.")
    parser.add_argument("--date", type=str, default=None, help="Override today (YYYY-MM-DD).")
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Truncate yfinance bars to this date (e.g. Friday close when running Saturday).",
    )
    parser.add_argument(
        "--force-rebalance",
        action="store_true",
        help="ETF momentum: ignore Monday-only calendar (for manual / weekend runs).",
    )
    parser.add_argument(
        "--reset-virtual-book",
        type=int,
        metavar="BOT_ID",
        help="Wipe trades, positions, and equity rows for this bot in SQLite "
        "(back to initial cash). Requires --yes. Does not flatten IBKR.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive actions (e.g. --reset-virtual-book).",
    )
    args = parser.parse_args()

    _configure_logging()
    log = logging.getLogger("main")

    if args.init_db:
        init_db()
        return 0

    if args.reset_virtual_book is not None:
        if not args.yes:
            print(
                "Refusing: --reset-virtual-book is destructive. "
                "Re-run with --yes to confirm."
            )
            return 2
        from core.db import Bot, get_session
        from core.portfolio import Portfolio

        bid = args.reset_virtual_book
        with get_session() as session:
            if session.query(Bot).filter(Bot.id == bid).one_or_none() is None:
                print(f"No bot with id={bid}")
                return 2
            Portfolio.reset_virtual_book(session, bid)
            session.commit()
        print(
            f"Reset virtual book for bot_id={bid}. "
            "IBKR paper positions (if any) were NOT closed."
        )
        return 0

    if not args.once and not args.auto:
        parser.print_help()
        return 2

    from core.runner import run_once

    today = date.fromisoformat(args.date) if args.date else datetime.now().date()

    skip_bot_ids: frozenset[int] = frozenset()
    if args.auto:
        if today.weekday() >= 5:
            log.info("Auto mode: weekend (%s) — skipping run.", today.strftime("%A"))
            return 0
        from core.db import Bot, RunLog, get_session
        with get_session() as s:
            enabled_ids = frozenset(b.id for b in s.query(Bot).filter(Bot.enabled == 1).all())
            ran_today = frozenset(
                r.bot_id for r in s.query(RunLog.bot_id).filter(
                    RunLog.run_date == today,
                    RunLog.bot_id.in_(list(enabled_ids)),
                ).all()
            )
        pending_ids = enabled_ids - ran_today
        if not pending_ids:
            log.info(
                "Auto mode: all %d enabled bot(s) already ran today — skipping.",
                len(enabled_ids),
            )
            return 0
        if ran_today:
            log.info(
                "Auto mode: %d bot(s) already ran today, resuming %d pending bot(s): %s",
                len(ran_today), len(pending_ids), sorted(pending_ids),
            )
        skip_bot_ids = ran_today
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    log.info(
        "=== run_once start (date=%s, backend=%s, as_of=%s, force_rebalance=%s) ===",
        today,
        CONFIG.broker_backend,
        as_of,
        args.force_rebalance,
    )
    try:
        reports = run_once(
            today=today,
            force_rebalance=args.force_rebalance,
            as_of=as_of,
            skip_bot_ids=skip_bot_ids,
        )
    except Exception as e:
        log.exception("fatal: %s", e)
        return 1

    for r in reports:
        log.info("DONE %s", r.summary_line())
    log.info("=== run_once end ===")

    # ── Trade Explanation Agent ──────────────────────────────────────────────
    # After all bots run, if any trades fired, ask Claude to explain them
    # in plain language. Output goes to the daily log file.
    # Runs only when ANTHROPIC_API_KEY is set and there are actual fills.
    import os
    if os.getenv("ANTHROPIC_API_KEY") and any(r.approved for r in reports):
        try:
            from agents.trade_explainer import explain_trades
            from core.db import RunLog, get_session
            for r in reports:
                if not r.approved:
                    continue
                trades_for_agent = [
                    {
                        "ticker":        fill.ticker,
                        "side":          order.side.value,
                        "qty":           round(fill.qty, 4),
                        "price_eur":     round(fill.price_eur, 2),
                        "fee_eur":       round(fill.fee_eur, 2),
                        "signal_reason": order.signal_reason,
                    }
                    for order, fill in r.approved
                ]
                explanation = explain_trades(r.bot_id, trades_for_agent, today)
                if explanation:
                    log.info(
                        "\n=== EXPLICACIÓ D'OPERACIONS (bot=%d) ===\n%s\n"
                        "=" * 45,
                        r.bot_id, explanation,
                    )
                    # Persist to the RunLog so the dashboard can show it
                    with get_session() as s:
                        run_log = (
                            s.query(RunLog)
                            .filter(RunLog.bot_id == r.bot_id, RunLog.run_date == today)
                            .order_by(RunLog.timestamp.desc())
                            .first()
                        )
                        if run_log:
                            run_log.explanation = explanation
                            s.commit()
        except Exception as exc:
            log.warning("trade_explainer failed (non-fatal): %s", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
