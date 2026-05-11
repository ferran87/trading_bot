"""Streamlit tab — '🧠 Tesis d'inversió'.

Phase 2 of the AI Trading System. Renders:
  1. Pending action cards (open / add / reduce / exit) — user approves or rejects
  2. Candidates waiting for a technical signal (amber state)
  3. Active theses with current conviction + P&L
  4. Track record (conviction × hit-rate breakdown)

All approve/reject actions write to the database only. The strategy module
(strategies/ai_thesis.py) picks up approved actions on the next bot run.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from core.db import Thesis, ThesisAction, ThesisReviewLog, get_session

log = logging.getLogger(__name__)

_ACTION_LABELS = {
    "open":   "Obrir posició",
    "add":    "Ampliar posició",
    "reduce": "Reduir posició",
    "exit":   "Tancar posició",
}

_VERDICT_EMOJI = {
    "intact":       "✅",
    "strengthened": "💪",
    "weakening":    "⚠️",
    "invalidated":  "❌",
}

_STATUS_EMOJI = {
    "candidate": "🟢",
    "waiting":   "⏳",
    "active":    "📈",
    "invalidated": "❌",
    "exited":    "🏁",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _decide_action(action_id: int, decision: str, decided_by: str = "user") -> None:
    """Approve or reject a ThesisAction and update the related Thesis status."""
    with get_session() as s:
        action = s.query(ThesisAction).filter(ThesisAction.id == action_id).first()
        if action is None:
            return
        action.status = decision
        action.decided_at = _utcnow()

        thesis = s.query(Thesis).filter(Thesis.id == action.thesis_id).first()

        if thesis is not None:
            if decision == "approved" and action.action_type == "open":
                # Approved entry: position will be opened by strategy module on next run
                if thesis.status in ("candidate", "waiting"):
                    thesis.status = "active"

            elif decision == "rejected" and action.action_type == "open":
                # Rejected entry: close the thesis entirely (no orphan candidates).
                # User explicitly said "no" — don't keep the thesis around.
                thesis.status = "exited"
                thesis.closed_at = _utcnow()

            elif decision == "rejected" and action.action_type == "exit":
                # Rejected exit: revert thesis back to active
                if thesis.status == "invalidated":
                    thesis.status = "active"

            # Approved 'exit' / 'add' / 'reduce': thesis status unchanged here;
            # strategy module handles position-level changes on next run.

        s.commit()


def _run_pm_agent(sunday_mode: bool = False) -> None:
    """Trigger run_portfolio_manager.py in a subprocess and stream output."""
    python = Path(sys.executable)
    script = Path(__file__).parents[1] / "scripts" / "run_portfolio_manager.py"
    cmd = [str(python), str(script)]
    if sunday_mode:
        cmd.append("--sunday")

    placeholder = st.empty()
    lines: list[str] = []
    with st.spinner("Executant l'agent de tesis..."):
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        ) as proc:
            for line in proc.stdout:
                lines.append(line.rstrip())
                placeholder.code("\n".join(lines[-40:]))
            proc.wait()
    if proc.returncode == 0:
        st.success("Agent completat correctament.")
    else:
        st.error(f"L'agent ha fallat (codi {proc.returncode}). Revisa els logs.")


# ── Main render function ───────────────────────────────────────────────────────

def render_thesis_tab() -> None:
    """Render the full '🧠 Tesis d'inversió' tab."""
    st.header("🧠 Tesis d'inversió")
    st.caption(
        "Claude manté tesis narratives a mig termini sobre un univers curat de 30-50 accions. "
        "Cada acció proposada requereix la teva aprovació — el bot no opera mai de forma autònoma."
    )

    # ── Capital metric ────────────────────────────────────────────────────────
    col_cap, col_pos, col_wait = st.columns(3)
    with get_session() as s:
        active_count = (
            s.query(Thesis)
            .filter(Thesis.bot_id == 30, Thesis.status == "active")
            .count()
        )
        waiting_count = (
            s.query(Thesis)
            .filter(Thesis.bot_id == 30, Thesis.status.in_(["candidate", "waiting"]))
            .count()
        )
        pending_count = (
            s.query(ThesisAction)
            .join(Thesis, ThesisAction.thesis_id == Thesis.id)
            .filter(
                Thesis.bot_id == 30,
                ThesisAction.status == "pending",
            )
            .count()
        )

    col_cap.metric("Capital inicial", "€5,000")
    col_pos.metric("Tesis actives", active_count)
    col_wait.metric("Accions pendents", pending_count)

    st.divider()

    # ── Trigger buttons ───────────────────────────────────────────────────────
    col_daily, col_sunday = st.columns(2)
    with col_daily:
        if st.button("▶ Revisió diària", help="Revisa totes les tesis actives"):
            _run_pm_agent(sunday_mode=False)
            st.rerun()
    with col_sunday:
        if st.button("☀️ Revisió diumenge (+ candidats)", help="Revisió + escaneig de nous candidats"):
            _run_pm_agent(sunday_mode=True)
            st.rerun()

    st.divider()

    # ── Section 1: Pending action cards ──────────────────────────────────────
    with get_session() as s:
        pending_actions = (
            s.query(ThesisAction)
            .join(Thesis, ThesisAction.thesis_id == Thesis.id)
            .filter(
                Thesis.bot_id == 30,
                ThesisAction.status == "pending",
            )
            .order_by(ThesisAction.proposed_at.desc())
            .all()
        )

        # Eager-load theses
        thesis_map: dict[int, Thesis] = {
            t.id: t
            for t in s.query(Thesis).filter(
                Thesis.id.in_([a.thesis_id for a in pending_actions])
            ).all()
        } if pending_actions else {}

    if pending_actions:
        st.subheader(f"📬 Accions pendents ({len(pending_actions)})")
        for action in pending_actions:
            thesis = thesis_map.get(action.thesis_id)
            if not thesis:
                continue

            action_label = _ACTION_LABELS.get(action.action_type, action.action_type.upper())
            conviction = thesis.conviction
            size_pct = action.size_pct or 0.10
            capital_eur = 5000.0
            size_eur = capital_eur * size_pct

            with st.container(border=True):
                col_title, col_approve, col_reject = st.columns([6, 1, 1])
                with col_title:
                    st.markdown(
                        f"**{action_label}: {thesis.ticker}** &nbsp;&nbsp; "
                        f"{'⭐' * conviction} ({conviction}/5) &nbsp;&nbsp; "
                        f"Mida: {size_pct:.0%} (≈ €{size_eur:.0f})"
                    )

                with col_approve:
                    if st.button("✅", key=f"approve_{action.id}", help="Aprovar"):
                        _decide_action(action.id, "approved")
                        st.success(f"{thesis.ticker}: acció aprovada.")
                        st.rerun()

                with col_reject:
                    if st.button("❌", key=f"reject_{action.id}", help="Rebutjar"):
                        _decide_action(action.id, "rejected")
                        st.info(f"{thesis.ticker}: acció rebutjada.")
                        st.rerun()

                # Thesis narrative
                st.markdown(f"**Tesi:** {thesis.thesis_text}")

                with st.expander("Veure cas bull/bear + invalidació"):
                    st.markdown(f"**🐂 Bull case:** {thesis.bull_case}")
                    st.markdown(f"**🐻 Bear case:** {thesis.bear_case}")
                    if thesis.invalidates_if:
                        st.markdown("**🚨 Invalida si:**")
                        for cond in thesis.invalidates_if:
                            st.markdown(f"  - {cond}")
                    if thesis.catalysts:
                        st.markdown("**⚡ Catalitzadors:**")
                        for cat in thesis.catalysts:
                            st.markdown(
                                f"  - **{cat.get('event', '')}** "
                                f"({cat.get('expected_date', '?')}): "
                                f"{cat.get('expected_outcome', '')}"
                            )

                # Exit rationale (only for exit actions)
                if action.action_type == "exit":
                    st.warning(f"💬 Raó de sortida: {action.rationale}")

                st.caption(
                    f"Proposat: {action.proposed_at.strftime('%d/%m/%Y %H:%M')} UTC  |  "
                    f"Horitzó: {thesis.horizon_months} mesos"
                )
    else:
        st.info("Cap acció pendent d'aprovació.")

    st.divider()

    # ── Section 2: Waiting theses ─────────────────────────────────────────────
    with get_session() as s:
        waiting_theses = (
            s.query(Thesis)
            .filter(Thesis.bot_id == 30, Thesis.status.in_(["candidate", "waiting"]))
            .order_by(Thesis.opened_at.desc())
            .all()
        )

    if waiting_theses:
        st.subheader(f"⏳ Candidatures en espera de senyal tècnic ({len(waiting_theses)})")
        for thesis in waiting_theses:
            days_waiting = (datetime.now(timezone.utc) - thesis.opened_at.replace(tzinfo=timezone.utc)).days
            expires_in = max(0, 30 - days_waiting)
            with st.container(border=True):
                st.markdown(
                    f"**{thesis.ticker}** — convicció {thesis.conviction}/5 &nbsp;|&nbsp; "
                    f"Esperant senyal RSI/SMA50 &nbsp;|&nbsp; Caduca en {expires_in} dies"
                )
                st.caption(thesis.thesis_text)

    st.divider()

    # ── Section 3: Active theses ──────────────────────────────────────────────
    with get_session() as s:
        active_theses = (
            s.query(Thesis)
            .filter(Thesis.bot_id == 30, Thesis.status == "active")
            .order_by(Thesis.opened_at.desc())
            .all()
        )

        # Get last review for each thesis
        last_review_map: dict[int, ThesisReviewLog] = {}
        if active_theses:
            from sqlalchemy import func
            subq = (
                s.query(
                    ThesisReviewLog.thesis_id,
                    func.max(ThesisReviewLog.reviewed_at).label("max_ts"),
                )
                .filter(ThesisReviewLog.thesis_id.in_([t.id for t in active_theses]))
                .group_by(ThesisReviewLog.thesis_id)
                .subquery()
            )
            latest_reviews = (
                s.query(ThesisReviewLog)
                .join(subq, (ThesisReviewLog.thesis_id == subq.c.thesis_id)
                      & (ThesisReviewLog.reviewed_at == subq.c.max_ts))
                .all()
            )
            last_review_map = {r.thesis_id: r for r in latest_reviews}

    if active_theses:
        st.subheader(f"📊 Tesis actives ({len(active_theses)})")

        for thesis in active_theses:
            last_review = last_review_map.get(thesis.id)
            verdict = last_review.verdict if last_review else "—"
            verdict_emoji = _VERDICT_EMOJI.get(verdict, "—")
            days_active = (datetime.now(timezone.utc) - thesis.opened_at.replace(tzinfo=timezone.utc)).days
            stale = (
                thesis.last_reviewed_at is None
                or (datetime.now(timezone.utc) - thesis.last_reviewed_at.replace(tzinfo=timezone.utc)).days > 7
            )
            stale_flag = " 🔴 (no revisada en >7 dies)" if stale else ""

            weakening_bar = (
                f" ⚠️ {thesis.consecutive_weakening_count}/5 debilitaments"
                if thesis.consecutive_weakening_count >= 3
                else ""
            )

            with st.expander(
                f"{verdict_emoji} **{thesis.ticker}** — conv {thesis.conviction}/5 "
                f"| {days_active}d activa{stale_flag}{weakening_bar}"
            ):
                st.markdown(f"**Tesi:** {thesis.thesis_text}")
                col_bull, col_bear = st.columns(2)
                with col_bull:
                    st.markdown(f"🐂 **Bull:** {thesis.bull_case[:300]}...")
                with col_bear:
                    st.markdown(f"🐻 **Bear:** {thesis.bear_case[:300]}...")

                if last_review:
                    st.markdown(
                        f"**Última revisió** ({last_review.reviewed_at.strftime('%d/%m/%Y')}): "
                        f"{verdict_emoji} {verdict} — {last_review.new_info_summary[:300]}"
                    )

                if thesis.invalidates_if:
                    st.markdown("**🚨 Invalida si:**")
                    for cond in thesis.invalidates_if:
                        st.markdown(f"  - {cond}")

                st.caption(
                    f"Horitzó: {thesis.horizon_months} mesos | "
                    f"Revisions: {thesis.review_count} | "
                    f"Oberta: {thesis.opened_at.strftime('%d/%m/%Y')}"
                )
    else:
        st.info("Cap tesi activa de moment.")

    st.divider()

    # ── Section 4: Track record ───────────────────────────────────────────────
    st.subheader("📈 Track record de tesis tancades")

    with get_session() as s:
        closed = (
            s.query(Thesis)
            .filter(
                Thesis.bot_id == 30,
                Thesis.status == "exited",
                Thesis.realized_pnl_eur.is_not(None),
            )
            .all()
        )

    if not closed:
        st.info("Encara no hi ha tesis tancades amb resultat.")
        return

    rows = [
        {
            "Ticker":        t.ticker,
            "Convicció":     t.conviction,
            "P&L (€)":       round(t.realized_pnl_eur or 0, 2),
            "Encertat":      "✅" if (t.realized_pnl_eur or 0) > 0 else "❌",
            "Tancada":       t.closed_at.strftime("%d/%m/%Y") if t.closed_at else "—",
        }
        for t in closed
    ]
    df = pd.DataFrame(rows)

    total_closed = len(df)
    win_count = (df["P&L (€)"] > 0).sum()
    win_rate = win_count / total_closed if total_closed else 0
    total_pnl = df["P&L (€)"].sum()

    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Tancades", total_closed)
    col_b.metric("Taxa d'encert", f"{win_rate:.0%}")
    col_c.metric("P&L total", f"€{total_pnl:+.2f}")

    # Conviction breakdown
    st.markdown("**Taxa d'encert per nivell de convicció:**")
    conv_stats = (
        df.groupby("Convicció")
        .apply(lambda g: pd.Series({
            "Tancades": len(g),
            "Encertades": (g["P&L (€)"] > 0).sum(),
            "Taxa": f"{(g['P&L (€)'] > 0).mean():.0%}",
            "P&L mig (€)": f"€{g['P&L (€)'].mean():+.2f}",
        }))
        .reset_index()
        .sort_values("Convicció", ascending=False)
    )
    st.dataframe(conv_stats, use_container_width=True, hide_index=True)

    with st.expander("Veure totes les tesis tancades"):
        st.dataframe(df, use_container_width=True, hide_index=True)
