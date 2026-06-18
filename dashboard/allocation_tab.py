"""Streamlit tab — '⚖️ Allocation'.

Lets each owner distribute their live-account capital across their live bots
as a percentage. Changes are written surgically to ``config/strategies.yaml``
and take effect on the next bot run.

Paper bots are always split equally and are not shown here.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import streamlit as st

from core.config import CONFIG, PROJECT_ROOT

log = logging.getLogger(__name__)


# ── YAML surgical edit ────────────────────────────────────────────────────────

def _save_allocations(allocations: dict[str, dict[int, int]]) -> None:
    """Overwrite the ``bot_allocations`` block in ``config/strategies.yaml``.

    ``allocations`` format: ``{owner_name: {bot_id: pct_int}}``.

    Uses line-level surgery (no YAML round-trip) so the rest of the file —
    comments, key order, strategy params — is fully preserved.
    """
    yaml_path = PROJECT_ROOT / "config" / "strategies.yaml"
    lines = yaml_path.read_text(encoding="utf-8").splitlines(keepends=True)

    # Build new block lines
    new_block: list[str] = [
        "# Capital allocation — fraction of each owner's deposited capital assigned to each live bot.\n",
        "# Must sum to 100 per owner. Managed from the ⚖️ Allocation tab in the dashboard.\n",
        "# Paper bots always split equally and are not configured here.\n",
        "bot_allocations:\n",
        "  live:\n",
    ]
    for owner, bots in sorted(allocations.items()):
        new_block.append(f"    {owner}:\n")
        for bot_id, pct in sorted(bots.items()):
            new_block.append(f"      {bot_id}: {pct}\n")
    new_block.append("\n")

    # Locate existing block boundaries
    start_idx: int | None = None
    end_idx = len(lines)
    for i, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        if stripped == "bot_allocations:":
            start_idx = i
        elif start_idx is not None and i > start_idx:
            if stripped and not stripped[0].isspace() and not stripped.startswith("#"):
                end_idx = i
                break

    if start_idx is not None:
        # Replace existing block (including any leading comment lines right above it)
        comment_start = start_idx
        for j in range(start_idx - 1, -1, -1):
            if lines[j].startswith("#"):
                comment_start = j
            else:
                break
        lines[comment_start:end_idx] = new_block
    else:
        # Insert before `strategies:` line
        for i, line in enumerate(lines):
            if line.startswith("strategies:"):
                lines[i:i] = new_block
                break

    yaml_path.write_text("".join(lines), encoding="utf-8")


def redistribute_on_disable(owner: str, disabled_bot_id: int) -> None:
    """When a live bot is disabled, redistribute its allocation to the other
    enabled live bots of the same owner proportionally.

    Called from the live-toggle handler in app.py.
    """
    allocs_cfg = CONFIG.strategies.get("bot_allocations", {}).get("live", {})
    owner_allocs: dict[int, int] = {
        int(k): int(v)
        for k, v in allocs_cfg.get(owner, {}).items()
    }
    if disabled_bot_id not in owner_allocs:
        return

    freed = owner_allocs.pop(disabled_bot_id)
    remaining = {k: v for k, v in owner_allocs.items() if k != disabled_bot_id}
    if not remaining:
        return

    total_rest = sum(remaining.values()) or 1
    # Distribute freed % proportionally, rounding down; give remainder to first bot.
    redistributed: dict[int, int] = {}
    leftover = freed
    first_key = next(iter(remaining))
    for bid, pct in remaining.items():
        add = round(freed * pct / total_rest)
        redistributed[bid] = pct + add
        leftover -= add
    redistributed[first_key] += leftover  # absorb rounding remainder

    # Merge back into full allocations and save
    full: dict[str, dict[int, int]] = {}
    for o, bots in allocs_cfg.items():
        if o == owner:
            full[o] = {disabled_bot_id: 0, **redistributed}
        else:
            full[o] = {int(k): int(v) for k, v in bots.items()}

    _save_allocations(full)
    CONFIG.reload_strategies()


# ── Tab renderer ──────────────────────────────────────────────────────────────

def render_allocation_tab(
    all_bots: pd.DataFrame,
    selected_owner: str,
    is_admin: bool,
) -> None:
    """Render the ⚖️ Allocation tab."""
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

    # Load current allocations
    allocs_cfg = CONFIG.strategies.get("bot_allocations", {}).get("live", {})

    # Show only the selected owner's section (consistent with rest of dashboard)
    owner_bots = live_bots[live_bots["owner"] == selected_owner].copy()
    if owner_bots.empty:
        st.info(f"No hi ha bots en viu per a {selected_owner}.")
        return

    _render_owner_section(owner_bots, selected_owner, allocs_cfg, is_admin)


def _render_owner_section(
    owner_bots: pd.DataFrame,
    owner: str,
    allocs_cfg: dict,
    is_admin: bool,
) -> None:
    owner_allocs_raw = allocs_cfg.get(owner, {})
    owner_allocs: dict[int, int] = {int(k): int(v) for k, v in owner_allocs_raw.items()}

    enabled_ids = set(owner_bots[owner_bots["enabled"]]["id"].astype(int))
    all_bot_rows = owner_bots.to_dict("records")

    if not all_bot_rows:
        return

    new_values: dict[int, int] = {}

    for bot in all_bot_rows:
        bid = int(bot["id"])
        is_enabled = bid in enabled_ids
        current_pct = owner_allocs.get(bid, 0)

        label = f"**{bot['name']}**"
        if not is_enabled:
            label += " *(inactiu)*"

        col_label, col_slider = st.columns([2, 3])
        with col_label:
            st.markdown(label)
        with col_slider:
            if is_enabled and is_admin:
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
                # Disabled bot or viewer: show locked value
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
        can_save = is_admin and (enabled_total == 100 or not enabled_ids)
        if st.button(
            "💾 Desar assignació",
            disabled=not can_save,
            key=f"save_alloc_{owner}",
        ):
            _persist_allocations(owner, new_values, allocs_cfg)
            st.success("Assignació desada. Tindrà efecte en la propera execució.")
            st.rerun()

    if not is_admin:
        st.caption("Necessites rol d'administrador per modificar l'assignació.")
    elif enabled_total != 100 and enabled_ids:
        delta = 100 - enabled_total
        sign = "+" if delta > 0 else ""
        st.warning(f"Cal ajustar {sign}{delta}% per arribar al 100%.")


def _persist_allocations(
    owner: str,
    new_owner_values: dict[int, int],
    allocs_cfg: dict,
) -> None:
    """Merge the updated owner slice back into the full allocations dict and save."""
    full: dict[str, dict[int, int]] = {}
    for o, bots in allocs_cfg.items():
        if o == owner:
            full[o] = new_owner_values
        else:
            full[o] = {int(k): int(v) for k, v in bots.items()}
    # Add owner if it wasn't there yet
    if owner not in full:
        full[owner] = new_owner_values

    _save_allocations(full)
    CONFIG.reload_strategies()
