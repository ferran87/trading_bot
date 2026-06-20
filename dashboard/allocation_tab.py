"""Streamlit tab — '⚖️ Allocation'.

Lets each owner distribute their live-account capital across their live bots
as a percentage. Allocations are stored in the DB (``bots.live_capital_pct``),
not YAML, so the Streamlit Cloud dashboard and the local bot stay in sync via
Supabase. Changes take effect on the next bot run.

Paper bots are always split equally and are not shown here.
"""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from dashboard.queries import _set_bot_allocations

log = logging.getLogger(__name__)


def _current_pct(value) -> int:
    """Coerce a DB live_capital_pct (float / NaN / None) to an int percent."""
    if value is None or value != value:  # None or NaN
        return 0
    return int(round(float(value)))


def redistribute_on_disable(owner_bots: pd.DataFrame, disabled_bot_id: int) -> None:
    """When a live bot is disabled, redistribute its allocation proportionally
    to the owner's remaining enabled live bots and persist to the DB.

    ``owner_bots`` is the owner's live-bot slice of the bots DataFrame.
    """
    allocs: dict[int, int] = {
        int(r["id"]): _current_pct(r.get("live_capital_pct"))
        for _, r in owner_bots.iterrows()
    }
    if disabled_bot_id not in allocs:
        return

    freed = allocs.pop(disabled_bot_id)
    remaining = {
        bid: pct for bid, pct in allocs.items()
        if bid in set(owner_bots[owner_bots["enabled"]]["id"].astype(int))
    }
    if not remaining or freed <= 0:
        # Just zero the disabled bot.
        _set_bot_allocations({disabled_bot_id: 0.0})
        return

    total_rest = sum(remaining.values()) or 1
    redistributed: dict[int, float] = {}
    leftover = freed
    first_key = next(iter(remaining))
    for bid, pct in remaining.items():
        add = round(freed * pct / total_rest)
        redistributed[bid] = pct + add
        leftover -= add
    redistributed[first_key] += leftover  # absorb rounding remainder

    payload: dict[int, float | None] = {disabled_bot_id: 0.0, **redistributed}
    _set_bot_allocations(payload)


# ── Tab renderer ──────────────────────────────────────────────────────────────

def render_allocation_tab(
    all_bots: pd.DataFrame,
    selected_owner: str,
    can_edit: bool = True,
) -> None:
    """Render the ⚖️ Allocation tab.

    ``can_edit`` gates whether the sliders are interactive. Allocation is a
    per-owner self-service action (like the live on/off toggle), so the caller
    passes True for the account currently being viewed.
    """
    st.subheader("Distribució de capital — bots en viu")
    st.caption(
        "Cada propietari distribueix el seu dipòsit T212 (en viu) entre els seus bots. "
        "El % s'aplica en la propera execució. Els bots paper sempre es divideixen "
        "equitativament."
    )

    live_bots = all_bots[all_bots["trading_mode"] == "live"].copy()
    if live_bots.empty:
        st.info("No hi ha bots en viu configurats.")
        return

    owner_bots = live_bots[live_bots["owner"] == selected_owner].copy()
    if owner_bots.empty:
        st.info(f"No hi ha bots en viu per a {selected_owner}.")
        return

    _render_owner_section(owner_bots, selected_owner, can_edit)


def _render_owner_section(
    owner_bots: pd.DataFrame,
    owner: str,
    can_edit: bool,
) -> None:
    enabled_ids = set(owner_bots[owner_bots["enabled"]]["id"].astype(int))
    all_bot_rows = owner_bots.to_dict("records")
    if not all_bot_rows:
        return

    new_values: dict[int, int] = {}

    for bot in all_bot_rows:
        bid = int(bot["id"])
        is_enabled = bid in enabled_ids
        current_pct = _current_pct(bot.get("live_capital_pct"))

        label = f"**{bot['name']}**"
        if not is_enabled:
            label += " *(inactiu)*"

        col_label, col_slider = st.columns([2, 3])
        with col_label:
            st.markdown(label)
        with col_slider:
            if is_enabled and can_edit:
                val = st.slider(
                    f"alloc_{bid}",
                    min_value=0,
                    max_value=100,
                    value=current_pct,
                    step=1,
                    format="%d%%",
                    label_visibility="collapsed",
                    key=f"alloc_slider_{bid}",
                )
                new_values[bid] = val
            else:
                # Disabled bot or read-only view: show locked value
                st.markdown(
                    f"<div style='padding:6px 0; color:gray'>{current_pct}% "
                    f"{'(inactiu — no assignable)' if not is_enabled else '(sols lectura)'}</div>",
                    unsafe_allow_html=True,
                )
                new_values[bid] = current_pct

    # Only enabled bots must sum to 100
    enabled_total = sum(v for bid, v in new_values.items() if bid in enabled_ids)
    disabled_total = sum(v for bid, v in new_values.items() if bid not in enabled_ids)

    st.divider()
    col_sum, col_btn = st.columns([2, 1])

    with col_sum:
        if enabled_ids:
            color = "green" if enabled_total == 100 else "red"
            st.markdown(
                f"Suma bots actius: <span style='color:{color}; font-weight:700'>"
                f"{enabled_total}%</span> / 100%",
                unsafe_allow_html=True,
            )
            if disabled_total > 0:
                st.caption(f"Bots inactius retenen {disabled_total}% (ignorat en execució)")
        else:
            st.caption("Cap bot actiu.")

    with col_btn:
        can_save = can_edit and (enabled_total == 100 or not enabled_ids)
        if st.button(
            "💾 Desar assignació",
            disabled=not can_save,
            key=f"save_alloc_{owner}",
        ):
            # Persist only the bots shown for this owner (enabled + disabled).
            _set_bot_allocations({bid: float(pct) for bid, pct in new_values.items()})
            st.success("Assignació desada. Tindrà efecte en la propera execució.")
            st.rerun()

    if not can_edit:
        st.caption("Aquesta vista és de només lectura.")
    elif enabled_total != 100 and enabled_ids:
        delta = 100 - enabled_total
        sign = "+" if delta > 0 else ""
        st.warning(f"Cal ajustar {sign}{delta}% per arribar al 100%.")
