"""Streamlit tab — '📚 Temes d'inversió'.

Phase 4 of the AI Trading System. Renders:
  1. Buttons to trigger Strategist agent (propose / review)
  2. Pending theme proposals from the Strategist (approve / reject)
  3. Review notes from the Strategist (read / dismiss)
  4. Active themes with candidate tickers and linked analyses
  5. Archived themes (collapsed)

All rating edits (importance / potential) and candidate-list edits are done
directly via the dashboard form.  The Strategist agent NEVER modifies active
theme ratings — only the user can.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from core.db import Theme, ThemeReviewNote, Thesis, get_session

log = logging.getLogger(__name__)

_SEVERITY_EMOJI = {
    "info":     "ℹ️",
    "warning":  "⚠️",
    "critical": "🚨",
}

_IMPORTANCE_LABELS = {1: "Molt baix", 2: "Baix", 3: "Moderat", 4: "Alt", 5: "Molt alt / Transformacional"}
_POTENTIAL_LABELS  = {1: "Marginal", 2: "Moderat", 3: "Bo", 4: "Molt bo", 5: "Excepcional (múltiples baggers)"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return aware.strftime("%d/%m/%Y")


def _approve_theme(theme_id: int) -> None:
    with get_session() as s:
        t = s.query(Theme).filter(Theme.id == theme_id).first()
        if t:
            t.status = "active"
            t.approved_at = _utcnow()
            s.commit()


def _reject_theme(theme_id: int) -> None:
    with get_session() as s:
        t = s.query(Theme).filter(Theme.id == theme_id).first()
        if t:
            t.status = "archived"
            t.archived_at = _utcnow()
            s.commit()


def _archive_theme(theme_id: int) -> None:
    with get_session() as s:
        t = s.query(Theme).filter(Theme.id == theme_id).first()
        if t:
            t.status = "archived"
            t.archived_at = _utcnow()
            s.commit()


def _update_theme(theme_id: int, importance: int, potential: int, user_notes: str) -> None:
    with get_session() as s:
        t = s.query(Theme).filter(Theme.id == theme_id).first()
        if t:
            t.importance  = importance
            t.potential   = potential
            t.user_notes  = user_notes.strip() or None
            s.commit()


def _dismiss_note(note_id: int) -> None:
    with get_session() as s:
        n = s.query(ThemeReviewNote).filter(ThemeReviewNote.id == note_id).first()
        if n:
            n.status = "dismissed"
            s.commit()


def _mark_note_read(note_id: int) -> None:
    with get_session() as s:
        n = s.query(ThemeReviewNote).filter(ThemeReviewNote.id == note_id).first()
        if n and n.status == "unread":
            n.status = "read"
            s.commit()


def _theses_for_theme(theme_id: int) -> list:
    """Return active/waiting theses linked to a theme, newest first."""
    with get_session() as s:
        return (
            s.query(Thesis)
            .filter(
                Thesis.theme_id == theme_id,
                Thesis.status.in_(["candidate", "active", "waiting"]),
            )
            .order_by(Thesis.opened_at.desc())
            .all()
        )


def _run_strategist_subprocess(mode: str) -> None:
    """Launch scripts/run_strategist.py in a subprocess and show a spinner."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_strategist.py"
    placeholder = st.empty()
    label = "nous temes" if mode == "propose" else "revisió de temes"
    with st.spinner(f"Executant el Strategist ({label})..."):
        proc = subprocess.run(
            [sys.executable, str(script), "--mode", mode],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(script.parent.parent),
        )
    if proc.returncode == 0:
        placeholder.success("Strategist completat correctament.")
    else:
        err = (proc.stderr or proc.stdout or "")[-1200:]
        placeholder.error(f"El Strategist ha fallat (codi {proc.returncode}).\n```\n{err}\n```")
    st.rerun()


# ── Section renderers ─────────────────────────────────────────────────────────

def _render_pending_proposals(proposals: list[Theme]) -> None:
    st.subheader(f"📬 Propostes pendents del Strategist ({len(proposals)})")
    if not proposals:
        st.info("Cap proposta pendent. Prem 'Proposar nous temes' per generar-ne.")
        return

    for t in proposals:
        tickers = t.candidate_tickers or []
        invalidators = t.invalidators or []
        with st.container(border=True):
            col_title, col_approve, col_reject = st.columns([6, 1, 1])
            with col_title:
                st.markdown(
                    f"**{t.name}** &nbsp; "
                    f"Imp {t.importance}/5 · Pot {t.potential}/5 · "
                    f"Horitzó {t.horizon_years}a"
                )
            with col_approve:
                if st.button("✅", key=f"approve_theme_{t.id}", help="Aprovar tema"):
                    _approve_theme(t.id)
                    st.success(f"'{t.name}' activat.")
                    st.rerun()
            with col_reject:
                if st.button("❌", key=f"reject_theme_{t.id}", help="Rebutjar tema"):
                    _reject_theme(t.id)
                    st.info(f"'{t.name}' arxivat.")
                    st.rerun()

            st.markdown(f"_{t.narrative_text}_")

            col_imp, col_pot = st.columns(2)
            col_imp.caption(
                f"**Importància:** {_IMPORTANCE_LABELS.get(t.importance, str(t.importance))}"
            )
            col_pot.caption(
                f"**Potencial:** {_POTENTIAL_LABELS.get(t.potential, str(t.potential))}"
            )

            with st.expander("Candidats + invalidadors"):
                st.markdown(f"**Candidats ({len(tickers)}):** {', '.join(tickers)}")
                if invalidators:
                    st.markdown("**Invalida el tema si:**")
                    for inv in invalidators:
                        st.markdown(f"  - {inv}")

            st.caption(f"Proposat el {_fmt_date(t.proposed_at)}")


def _render_review_notes(notes: list[ThemeReviewNote], themes_by_id: dict[int, Theme]) -> None:
    unread = [n for n in notes if n.status == "unread"]
    read   = [n for n in notes if n.status == "read"]

    all_visible = unread + read
    if not all_visible:
        return

    st.subheader(f"🔔 Recomanacions de revisió ({len(unread)} sense llegir)")
    st.caption(
        "Observacions informatives del Strategist. No modifiquen cap qualificació — "
        "l'usuari decideix si edita el tema o descarta la nota."
    )

    for n in all_visible:
        theme = themes_by_id.get(n.theme_id)
        theme_name = theme.name if theme else f"Tema #{n.theme_id}"
        sev_emoji  = _SEVERITY_EMOJI.get(n.severity, "ℹ️")
        is_unread  = (n.status == "unread")

        with st.container(border=True):
            col_title, col_read, col_dismiss = st.columns([6, 1, 1])
            with col_title:
                badge = " 🆕" if is_unread else ""
                st.markdown(f"{sev_emoji} **{theme_name}**{badge}")
            with col_read:
                if is_unread and st.button(
                    "👁", key=f"read_note_{n.id}", help="Marcar com llegida"
                ):
                    _mark_note_read(n.id)
                    st.rerun()
            with col_dismiss:
                if st.button("✖", key=f"dismiss_note_{n.id}", help="Descartar nota"):
                    _dismiss_note(n.id)
                    st.rerun()

            st.markdown(f"**Observació:** {n.observation}")
            st.markdown(f"**Recomanació:** {n.recommendation}")
            st.caption(
                f"Severitat: {n.severity.upper()} · "
                f"Creat el {_fmt_date(n.created_at)}"
            )


def _render_active_themes(themes: list[Theme]) -> None:
    st.subheader(f"📚 Temes actius ({len(themes)})")
    if not themes:
        st.info(
            "Cap tema actiu. Prem 'Proposar nous temes' per generar propostes "
            "del Strategist, o aproveu les propostes pendents de dalt."
        )
        return

    for t in themes:
        tickers     = t.candidate_tickers or []
        invalidators = t.invalidators or []
        theses      = _theses_for_theme(t.id)

        with st.container(border=True):
            hcol1, hcol2 = st.columns([4, 1])
            with hcol1:
                st.markdown(
                    f"### {t.name}\n"
                    f"Imp **{t.importance}/5** · Pot **{t.potential}/5** · "
                    f"Horitzó **{t.horizon_years}a** · "
                    f"Aprovat {_fmt_date(t.approved_at)}"
                )
            with hcol2:
                if st.button(
                    "🗄 Arxivar",
                    key=f"archive_theme_{t.id}",
                    help="Arxivar aquest tema",
                ):
                    _archive_theme(t.id)
                    st.info(f"'{t.name}' arxivat.")
                    st.rerun()

            st.markdown(t.narrative_text)

            # Candidate tickers with linked thesis summaries
            st.markdown(f"**Candidats ({len(tickers)}):** `{'` `'.join(tickers)}`")

            if theses:
                st.markdown("**Anàlisi recents per candidat:**")
                for thesis in theses:
                    verdict_map = {
                        "candidate": "🟢",
                        "waiting":   "⏳",
                        "active":    "📈",
                    }
                    emoji = verdict_map.get(thesis.status, "•")
                    conv  = f"conv {thesis.conviction}/5" if thesis.conviction else ""
                    with st.expander(
                        f"{emoji} **{thesis.ticker}** {conv}  — {thesis.thesis_text[:80]}..."
                        if len(thesis.thesis_text or "") > 80
                        else f"{emoji} **{thesis.ticker}** {conv}  — {thesis.thesis_text}"
                    ):
                        if thesis.positioning_vs_theme:
                            st.markdown(f"**Posicionament vs tema:** {thesis.positioning_vs_theme}")
                        if thesis.execution_evidence:
                            st.markdown(f"**Evidència d'execució:** {thesis.execution_evidence}")
                        if thesis.valuation_assessment:
                            st.markdown(f"**Valoració:** {thesis.valuation_assessment}")
                        st.markdown(f"**🐂 Bull case:** {thesis.bull_case}")
                        st.markdown(f"**🐻 Bear case:** {thesis.bear_case}")
                        if thesis.invalidates_if:
                            st.markdown("**🚨 Invalida si:**")
                            conds = (
                                thesis.invalidates_if
                                if isinstance(thesis.invalidates_if, list)
                                else json.loads(thesis.invalidates_if)
                            )
                            for cond in conds:
                                st.markdown(f"  - {cond}")

            # Invalidators
            with st.expander("Condicions d'invalidació del tema"):
                if invalidators:
                    for inv in invalidators:
                        st.markdown(f"  - {inv}")
                else:
                    st.caption("Cap condició definida.")

            # Edit form — inline
            with st.expander("✏️ Editar qualificacions"):
                with st.form(key=f"edit_theme_{t.id}"):
                    new_imp = st.slider(
                        "Importància (1-5)",
                        min_value=1, max_value=5,
                        value=t.importance,
                        key=f"imp_slider_{t.id}",
                    )
                    st.caption(_IMPORTANCE_LABELS.get(new_imp, ""))
                    new_pot = st.slider(
                        "Potencial (1-5)",
                        min_value=1, max_value=5,
                        value=t.potential,
                        key=f"pot_slider_{t.id}",
                    )
                    st.caption(_POTENTIAL_LABELS.get(new_pot, ""))
                    new_notes = st.text_area(
                        "Notes personals",
                        value=t.user_notes or "",
                        key=f"notes_{t.id}",
                        height=80,
                    )
                    if st.form_submit_button("💾 Desar canvis"):
                        _update_theme(t.id, new_imp, new_pot, new_notes)
                        st.success("Qualificacions actualitzades.")
                        st.rerun()


def _render_archived_themes(themes: list[Theme]) -> None:
    if not themes:
        return
    with st.expander(f"🗄 Temes arxivats ({len(themes)})"):
        for t in themes:
            st.markdown(
                f"**{t.name}** — Arxivat {_fmt_date(t.archived_at)} · "
                f"Imp {t.importance}/5 · Pot {t.potential}/5"
            )
            st.caption(t.narrative_text[:200] + ("..." if len(t.narrative_text or "") > 200 else ""))
            st.divider()


# ── Main render ───────────────────────────────────────────────────────────────

def render_themes_tab() -> None:
    st.header("📚 Temes d'inversió")
    st.caption(
        "El Strategist proposa narratives d'inversió durables (2-3 anys). "
        "Tu les aproves, edites i arxives. L'Analyst (bot 30) avalua els candidats "
        "dins de cada tema aprovat."
    )

    # ── Trigger buttons ───────────────────────────────────────────────────────
    col_propose, col_review = st.columns(2)
    with col_propose:
        if st.button(
            "✨ Proposar nous temes",
            help="Claude analitza l'univers i proposa 4-5 noves narratives temàtiques.",
        ):
            _run_strategist_subprocess("propose")

    with col_review:
        if st.button(
            "🔍 Revisar temes existents",
            help="Claude examina els temes actius i mostra notes informatives si detecta novetats.",
        ):
            _run_strategist_subprocess("review")

    st.divider()

    # ── Load DB data ──────────────────────────────────────────────────────────
    with get_session() as s:
        all_themes: list[Theme] = (
            s.query(Theme)
            .order_by(Theme.proposed_at.desc())
            .all()
        )

        pending_proposals = [t for t in all_themes if t.status == "proposed"]
        active_themes     = [t for t in all_themes if t.status == "active"]
        archived_themes   = [t for t in all_themes if t.status == "archived"]

        themes_by_id = {t.id: t for t in all_themes}

        # Unread + read notes (not dismissed) for active themes
        active_ids = [t.id for t in active_themes]
        review_notes: list[ThemeReviewNote] = []
        if active_ids:
            review_notes = (
                s.query(ThemeReviewNote)
                .filter(
                    ThemeReviewNote.theme_id.in_(active_ids),
                    ThemeReviewNote.status.in_(["unread", "read"]),
                )
                .order_by(ThemeReviewNote.created_at.desc())
                .all()
            )

    # ── Unread badge in header ────────────────────────────────────────────────
    n_unread = sum(1 for n in review_notes if n.status == "unread")
    n_pending = len(pending_proposals)

    if n_pending or n_unread:
        badges = []
        if n_pending:
            badges.append(f"📬 {n_pending} proposta{'es' if n_pending != 1 else ''} pendents")
        if n_unread:
            badges.append(f"🔔 {n_unread} nota{'es' if n_unread != 1 else ''} noves")
        st.info("  ·  ".join(badges))

    # ── Sections ──────────────────────────────────────────────────────────────
    if pending_proposals:
        _render_pending_proposals(pending_proposals)
        st.divider()

    if review_notes:
        _render_review_notes(review_notes, themes_by_id)
        st.divider()

    _render_active_themes(active_themes)

    if archived_themes:
        st.divider()
        _render_archived_themes(archived_themes)
