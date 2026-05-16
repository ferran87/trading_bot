"""Streamlit tab — '🧪 Laboratori d'estratègies'.

Phase 1 of the AI Trading System. Renders pending RuleProposals as cards the
user can approve or reject. Approval edits ``config/strategies.yaml`` in
place (surgically, to preserve comments) and logs to ``rule_change_log``.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from core.config import CONFIG, PROJECT_ROOT
from core.db import RuleChangeLog, RuleProposal, SimulatedClosedPosition, get_session

from dashboard._helpers import _md, _utcnow

log = logging.getLogger(__name__)

_STRATEGY_LABELS: dict[str, str] = {
    "rsi_compounder": "🤖 RSI Compounder",
    "trend_momentum": "📈 Trend Momentum",
}


# ── YAML surgical edit ────────────────────────────────────────────────────

def _apply_yaml_edit(strategy: str, param_name: str, new_value: float) -> tuple[str, str]:
    """Edit ``config/strategies.yaml`` in place: <strategy>.<param> = new_value.

    Uses a line-level edit (NOT yaml round-trip) to preserve comments,
    blank lines, and key order. Returns (old_value_str, new_value_str).

    YAML structure assumption:
        strategies:
          <strategy_name>:
            <param>: <value>  # optional comment
    """
    yaml_path = PROJECT_ROOT / "config" / "strategies.yaml"
    lines = yaml_path.read_text(encoding="utf-8").splitlines(keepends=True)

    strategy_header = f"  {strategy}:"
    in_strategy = False
    target_idx: int | None = None

    for i, line in enumerate(lines):
        # Identify the strategy header (exact match, accounting for trailing newline)
        if line.rstrip("\r\n") == strategy_header:
            in_strategy = True
            continue
        if in_strategy:
            # End of this strategy block: another 2-space-indented key, or top-level
            stripped = line.rstrip("\r\n")
            if stripped and not line.startswith("    ") and not line.startswith("\t"):
                if line.startswith("  ") and ":" in stripped and not line.startswith("   "):
                    break  # next strategy
                if not line.startswith(" "):
                    break  # top-level (e.g. blank line then root key)
            # Look for the param line
            inner = line.lstrip()
            if inner.startswith(f"{param_name}:") or inner.startswith(f"{param_name} :"):
                target_idx = i
                break

    if target_idx is None:
        raise ValueError(f"could not find {strategy}.{param_name} in {yaml_path}")

    line = lines[target_idx]
    prefix, _sep, rest = line.partition(":")
    if "#" in rest:
        value_part, hash_, comment_part = rest.partition("#")
        old_value = value_part.strip()
        new_line = f"{prefix}: {new_value}  #{comment_part}"
        if not new_line.endswith("\n"):
            new_line += "\n"
    else:
        old_value = rest.strip()
        new_line = f"{prefix}: {new_value}\n"

    lines[target_idx] = new_line
    yaml_path.write_text("".join(lines), encoding="utf-8")

    # Invalidate the in-process cache so the bot picks up the new value
    CONFIG.reload_strategies()

    log.info("strategy_lab: %s.%s edited %s → %s in %s",
             strategy, param_name, old_value, new_value, yaml_path)
    return old_value, str(new_value)


# ── Approve / reject handlers ─────────────────────────────────────────────

def _approve(proposal_id: int) -> None:
    """Apply the proposal: edit YAML, log change, mark proposal approved."""
    with get_session() as s:
        proposal = s.query(RuleProposal).filter(RuleProposal.id == proposal_id).one_or_none()
        if proposal is None:
            st.error(f"Proposta {proposal_id} no existeix")
            return
        if proposal.status != "pending":
            st.warning(f"La proposta ja està en estat '{proposal.status}', no es pot reaprovar.")
            return

        try:
            old_val, new_val = _apply_yaml_edit(
                proposal.strategy, proposal.param_name, proposal.proposed_value,
            )
        except Exception as exc:
            st.error(f"Error editant YAML: {exc}")
            log.exception("YAML edit failed for proposal %d", proposal_id)
            return

        # Log to audit trail
        s.add(RuleChangeLog(
            proposal_id=proposal.id,
            strategy=proposal.strategy,
            param_name=proposal.param_name,
            old_value=proposal.current_value,
            new_value=proposal.proposed_value,
        ))
        proposal.status     = "approved"
        proposal.decided_at = _utcnow()
        proposal.decided_by = "ferran"
        s.commit()

    st.success(
        f"✅ Aprovada: {proposal.strategy}.{proposal.param_name} "
        f"{old_val} → {new_val}"
    )


def _reject(proposal_id: int) -> None:
    with get_session() as s:
        proposal = s.query(RuleProposal).filter(RuleProposal.id == proposal_id).one_or_none()
        if proposal is None or proposal.status != "pending":
            st.warning("Proposta ja resolta o no existeix.")
            return
        proposal.status     = "rejected"
        proposal.decided_at = _utcnow()
        proposal.decided_by = "ferran"
        s.commit()
    st.info("❌ Rebutjada")


# ── Data fetchers (cached briefly) ────────────────────────────────────────

@st.cache_data(ttl=15)
def _fetch_pending() -> pd.DataFrame:
    with get_session() as s:
        rows = (
            s.query(RuleProposal)
            .filter(RuleProposal.status == "pending")
            .order_by(RuleProposal.created_at.desc())
            .all()
        )
    return pd.DataFrame([
        {
            "id":             r.id,
            "created_at":     r.created_at,
            "strategy":       r.strategy,
            "param_name":     r.param_name,
            "current_value":  r.current_value,
            "proposed_value": r.proposed_value,
            "rationale":      r.rationale,
            "backtest":       r.backtest_summary or {},
            "walk_forward":   r.walk_forward_summary or {},
            "passes_ratchet": bool(r.passes_ratchet),
        }
        for r in rows
    ])


@st.cache_data(ttl=30)
def _fetch_history() -> pd.DataFrame:
    with get_session() as s:
        rows = (
            s.query(RuleProposal)
            .filter(RuleProposal.status.in_(["approved", "rejected"]))
            .order_by(RuleProposal.decided_at.desc())
            .limit(50)
            .all()
        )
    return pd.DataFrame([
        {
            "id":             r.id,
            "decided_at":     r.decided_at,
            "status":         r.status,
            "strategy":       r.strategy,
            "param_name":     r.param_name,
            "current_value":  r.current_value,
            "proposed_value": r.proposed_value,
            "passes_ratchet": bool(r.passes_ratchet),
        }
        for r in rows
    ])


@st.cache_data(ttl=60)
def _fetch_corpus_stats() -> dict:
    """Show how many simulated closed positions Claude has to reason over."""
    with get_session() as s:
        rows = (
            s.query(SimulatedClosedPosition.strategy)
            .all()
        )
    df = pd.DataFrame([{"strategy": r[0]} for r in rows])
    if df.empty:
        return {}
    return df.groupby("strategy").size().to_dict()


# ── Rendering ─────────────────────────────────────────────────────────────

def _format_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v*100:+.2f}%"


def _format_delta_pct(proposed: float | None, baseline: float | None) -> str:
    if proposed is None or baseline is None:
        return ""
    delta = (proposed - baseline) * 100
    arrow = "🟢" if delta > 0 else ("🔴" if delta < 0 else "⚪")
    return f"  {arrow} ({delta:+.2f}pp)"


def _render_proposal_card(row: pd.Series, *, is_admin: bool = False) -> None:
    bt = row["backtest"] or {}
    wf = row["walk_forward"] or {}
    wf_train = wf.get("train", {})
    wf_test  = wf.get("test", {})

    strategy_label = _STRATEGY_LABELS.get(row["strategy"], row["strategy"])
    direction = "↑" if row["proposed_value"] > row["current_value"] else "↓"

    with st.container(border=True):
        if is_admin:
            c1, c2, c3 = st.columns([6, 1, 1])
        else:
            c1 = st.container()
        with c1:
            st.markdown(
                f"#### {strategy_label} — `{row['param_name']}`: "
                f"**{row['current_value']:g}** {direction} **{row['proposed_value']:g}**"
            )
        if is_admin:
            with c2:
                if st.button("✅ Aprovar", key=f"appr_{row['id']}", use_container_width=True):
                    _approve(int(row["id"]))
                    _fetch_pending.clear()
                    _fetch_history.clear()
                    st.rerun()
            with c3:
                if st.button("❌ Rebutjar", key=f"rej_{row['id']}", use_container_width=True):
                    _reject(int(row["id"]))
                    _fetch_pending.clear()
                    _fetch_history.clear()
                    st.rerun()

        # Ratchet badge
        if row["passes_ratchet"]:
            st.markdown("**🟢 Passa el ratchet test** — millora el rendiment sense empitjorar el drawdown")
        else:
            st.markdown("**🟠 No passa el ratchet test** — empitjora el drawdown o no millora el rendiment")

        # Backtest comparison table
        st.markdown("##### Backtest complet (període 2024+)")
        if bt:
            base = bt.get("baseline", bt) if "baseline" in bt else bt
            prop = bt.get("proposed", {})
            comp_df = pd.DataFrame([
                {
                    "Mètrica":   "Rendiment",
                    "Actual":    _format_pct(base.get("return_pct")),
                    "Proposat":  _format_pct(prop.get("return_pct")) +
                                 _format_delta_pct(prop.get("return_pct"), base.get("return_pct")),
                },
                {
                    "Mètrica":   "Màxim drawdown",
                    "Actual":    _format_pct(base.get("max_drawdown_pct")),
                    "Proposat":  _format_pct(prop.get("max_drawdown_pct")) +
                                 _format_delta_pct(prop.get("max_drawdown_pct"), base.get("max_drawdown_pct")),
                },
                {
                    "Mètrica":   "Operacions",
                    "Actual":    str(base.get("n_trades", "—")),
                    "Proposat":  str(prop.get("n_trades", "—")),
                },
            ])
            st.dataframe(comp_df, use_container_width=True, hide_index=True)

        # Walk-forward
        st.markdown("##### Walk-forward (validació out-of-sample)")
        if wf_train and wf_test:
            wf_df = pd.DataFrame([
                {
                    "Període":           "Train (in-sample)",
                    "Rendiment actual":  _format_pct(wf_train.get("baseline", {}).get("return_pct")),
                    "Rendiment proposat": _format_pct(wf_train.get("proposed", {}).get("return_pct")),
                    "Δ":                 _format_pct(wf_train.get("delta_return_pct")),
                },
                {
                    "Període":           "Test (out-of-sample)",
                    "Rendiment actual":  _format_pct(wf_test.get("baseline", {}).get("return_pct")),
                    "Rendiment proposat": _format_pct(wf_test.get("proposed", {}).get("return_pct")),
                    "Δ":                 _format_pct(wf_test.get("delta_return_pct")),
                },
            ])
            st.dataframe(wf_df, use_container_width=True, hide_index=True)
            if wf.get("overfit_flag"):
                st.warning("⚠️ Sobreajustada — la millora out-of-sample és molt menor que in-sample.")
        else:
            st.caption("(walk-forward no disponible)")

        # Rationale (Claude's Catalan reasoning)
        st.markdown("##### 💭 Per què (raonament de l'analista)")
        st.markdown(f"> {_md(row['rationale'])}")

        st.caption(f"Proposta #{row['id']} — creada {row['created_at']:%Y-%m-%d %H:%M}")


def _render_run_button(*, is_admin: bool = False) -> None:
    """Manual trigger to invoke the critic agent in a background subprocess."""
    if not is_admin:
        return
    if st.button("▶️ Executar revisió ara", help="Llança l'agent crític per a totes les estratègies"):
        # Fire-and-forget subprocess. The dashboard remains responsive; the
        # user refreshes the page to see new proposals appear.
        script = PROJECT_ROOT / "scripts" / "run_strategy_critic.py"
        venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
        python_exe = str(venv_python) if venv_python.exists() else sys.executable
        try:
            subprocess.Popen(
                [python_exe, str(script)],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            st.info("L'agent crític està executant-se en segon pla. Recarrega la pàgina d'aquí a 1-2 minuts.")
        except Exception as exc:
            st.error(f"No s'ha pogut iniciar l'agent: {exc}")


def _render_history(history: pd.DataFrame) -> None:
    if history.empty:
        st.caption("Encara no hi ha decisions historitzades.")
        return

    n_approved = int((history["status"] == "approved").sum())
    n_rejected = int((history["status"] == "rejected").sum())
    st.markdown(
        f"**Aprovades**: {n_approved} · **Rebutjades**: {n_rejected}"
    )

    display = history.copy()
    display["acció"] = display.apply(
        lambda r: f"{r['param_name']}: {r['current_value']:g} → {r['proposed_value']:g}",
        axis=1,
    )
    display["estat"] = display["status"].map(
        {"approved": "✅ aprovada", "rejected": "❌ rebutjada"}
    )
    display["ratchet"] = display["passes_ratchet"].map({True: "🟢", False: "🟠"})
    out = display[["decided_at", "estat", "strategy", "acció", "ratchet"]].rename(
        columns={"decided_at": "decidida el", "strategy": "estratègia"}
    )
    st.dataframe(out, use_container_width=True, hide_index=True)


def render_strategy_lab_tab(*, is_admin: bool = False) -> None:
    st.subheader("🧪 Laboratori d'estratègies")
    st.caption(
        "L'agent crític analitza l'historial de cada estratègia i proposa "
        "canvis numèrics als seus paràmetres. Cap canvi s'aplica fins que el "
        "validis aquí."
    )
    if not is_admin:
        st.info("👁 Mode visualització — només Ferran pot aprovar o rebutjar propostes.")

    # Top control row
    cl, cr = st.columns([1, 3])
    with cl:
        _render_run_button(is_admin=is_admin)
    with cr:
        corpus = _fetch_corpus_stats()
        if corpus:
            corpus_str = " · ".join(f"**{k}**: {v} operacions" for k, v in corpus.items())
            st.caption(f"📚 Corpus disponible — {corpus_str}")
        else:
            st.warning(
                "⚠️ El corpus està buit. Executa `python scripts/bootstrap_strategy_lab.py` "
                "abans de demanar a l'agent que faci propostes."
            )

    st.divider()

    # Pending proposals
    pending = _fetch_pending()
    st.markdown(f"### 📋 Propostes pendents ({len(pending)})")
    if pending.empty:
        st.info("Cap proposta pendent. Executa la revisió per generar-ne.")
    else:
        for _, row in pending.iterrows():
            _render_proposal_card(row, is_admin=is_admin)

    st.divider()

    # History
    st.markdown("### 📜 Historial")
    history = _fetch_history()
    _render_history(history)
