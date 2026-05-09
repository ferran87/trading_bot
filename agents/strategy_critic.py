"""Strategy Critic Agent.

Weekly slow-loop agent that proposes numeric parameter changes for the
rules-based bots. Modeled on ``agents/trade_explainer.py`` — same tool-using
pattern, different tools and prompt.

Lifecycle:
  1. Caller passes a strategy name (e.g. 'rsi_compounder')
  2. Agent loads the closed-position corpus + current params via tools
  3. Agent proposes 0-3 numeric param changes; for each it must
       - Provide a CAUSAL rationale (not just "backtest looks better")
       - Run walk_forward_validate to prove it generalises
  4. Each proposal is persisted as a RuleProposal (status='pending')
  5. The dashboard shows the cards; the user approves or rejects each one

The agent can ONLY tune params listed in critic_tools.BOUNDED_RANGES. Any
proposal that touches a frozen param or goes out of bounds is rejected at
insert time — never reaches the dashboard.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import anthropic

from core.config import CONFIG  # noqa: F401 — triggers .env load (ANTHROPIC_API_KEY)
from core.db import RuleProposal, get_session

from agents.critic_tools import (
    BOUNDED_RANGES,
    MAX_PROPOSALS_PER_STRATEGY,
    compute_ratchet,
    get_real_closed_positions,
    get_simulated_closed_positions,
    get_strategy_params,
    simulate_param_change,
    walk_forward_validate,
)

log = logging.getLogger(__name__)


# ── Tool definitions ──────────────────────────────────────────────────────
#
# These are the JSON schemas Claude sees. Descriptions are critical — they
# tell the model WHEN to use each tool. Keep them precise.

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_simulated_closed_positions",
        "description": (
            "Returns the corpus of historical round-trip trades for a strategy. "
            "Each row has ticker, entry/exit dates, return %, exit reason, and "
            "regime tags. ALWAYS call this first — it's the data you reason over."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "enum": ["rsi_compounder", "trend_momentum"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 200)",
                    "default": 200,
                },
            },
            "required": ["strategy"],
        },
    },
    {
        "name": "get_real_closed_positions",
        "description": (
            "Returns recent live (real-money) round-trips. Sparse for now — "
            "the bots launched 2026-05-06. Use this in addition to simulated "
            "data once enough live history accumulates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "enum": ["rsi_compounder", "trend_momentum"]},
                "lookback_days": {"type": "integer", "default": 365},
            },
            "required": ["strategy"],
        },
    },
    {
        "name": "get_strategy_params",
        "description": (
            "Returns the current numeric parameters of a strategy along with "
            "which ones are tunable (within what bounds) versus frozen. "
            "Call this BEFORE proposing any change so you know what's allowed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "enum": ["rsi_compounder", "trend_momentum"]},
            },
            "required": ["strategy"],
        },
    },
    {
        "name": "simulate_param_change",
        "description": (
            "Run a backtest with the proposed param overrides. Returns baseline "
            "vs proposed metrics (return, sharpe, max_dd, n_trades) plus a "
            "ratchet verdict. Call this to evaluate any change you're considering."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "enum": ["rsi_compounder", "trend_momentum"]},
                "param_overrides": {
                    "type": "object",
                    "description": "Map of param_name → new numeric value",
                    "additionalProperties": {"type": "number"},
                },
                "start": {"type": "string", "description": "YYYY-MM-DD (default: 2024-01-01)"},
                "end":   {"type": "string", "description": "YYYY-MM-DD (default: today)"},
            },
            "required": ["strategy", "param_overrides"],
        },
    },
    {
        "name": "walk_forward_validate",
        "description": (
            "MANDATORY before submitting a proposal. Splits history 70/30 and "
            "tests the override on the held-out 30%. Returns train + test "
            "summaries and an overfit_flag. A proposal whose test-period "
            "improvement is much smaller than its train-period improvement is "
            "overfit and should be rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "enum": ["rsi_compounder", "trend_momentum"]},
                "param_overrides": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
            },
            "required": ["strategy", "param_overrides"],
        },
    },
    {
        "name": "submit_proposal",
        "description": (
            "Submit a final parameter change proposal. Use this ONLY after "
            "running walk_forward_validate and confirming the change passes "
            "the ratchet and is not overfit. The proposal is persisted as "
            "status='pending' for user review in the dashboard."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string", "enum": ["rsi_compounder", "trend_momentum"]},
                "param_name": {"type": "string", "description": "The single param being changed"},
                "current_value": {"type": "number"},
                "proposed_value": {"type": "number"},
                "rationale": {
                    "type": "string",
                    "description": (
                        "Causal reasoning in Catalan. Must explain WHY this "
                        "change improves results (not just that the backtest "
                        "looks better). Reference specific patterns from the "
                        "closed-position data."
                    ),
                },
                "backtest_summary":     {"type": "object", "description": "Result of simulate_param_change full-period 'proposed' block"},
                "walk_forward_summary": {"type": "object", "description": "Result of walk_forward_validate 'test' block"},
            },
            "required": ["strategy", "param_name", "current_value", "proposed_value",
                         "rationale", "backtest_summary", "walk_forward_summary"],
        },
    },
]


# ── System prompt ─────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
Ets un analista quantitatiu en català. Treballes per a un sistema de bots
d'inversió personal (paper trading via Trading 212). Reps el conjunt
d'operacions tancades (reals i simulades en backtest) d'una estratègia i has
de proposar canvis numèrics als seus paràmetres per millorar el rendiment
ajustat al risc.

── REGLES FERMES (no negociables) ──────────────────────────────────────────
1. Només pots proposar canvis a paràmetres NUMÈRICS llistats com a "tunable"
   per get_strategy_params. Qualsevol altra cosa està congelada.
2. Cada proposta ha d'incloure una HIPÒTESI CAUSAL: per què creus que aquest
   canvi millorarà els resultats. No s'admeten propostes del tipus "el
   backtest dóna un número més alt" sense raonament causal — has de
   referir-te a patrons concrets de les operacions tancades.
3. Cada proposta ha de PASSAR EL RATCHET TEST: millorar el rendiment SENSE
   empitjorar el màxim drawdown més de 2 punts percentuals.
4. Cada proposta s'ha de validar amb walk_forward_validate. Si la millora
   in-sample (períod d'entrenament) és molt superior a la millora
   out-of-sample (test), la proposta està sobreajustada i no l'has d'enviar.
5. Màxim {max_proposals} propostes per estratègia. Si penses en més,
   prioritza les que tinguin millor relació impacte/risc.
6. Cada proposta canvia UN ÚNIC paràmetre. Combinacions complexes ja seran
   per iteracions futures un cop hàgim après què funciona.
7. Si després d'analitzar les dades no trobes cap canvi clarament positiu,
   ÉS CORRECTE no enviar cap proposta. Millor cap proposta que una de
   dolenta.

── PROCEDIMENT TÍPIC ──────────────────────────────────────────────────────
1. get_simulated_closed_positions(strategy) — entendre l'historial.
2. get_strategy_params(strategy) — saber quins paràmetres pots tocar.
3. Identificar 1-3 hipòtesis (e.g., "els trailing stops del 35% deixen córrer
   massa caigudes en règim BULL — un 25% capturaria el guany abans").
4. Per cada hipòtesi:
   a. simulate_param_change per veure l'impacte global.
   b. walk_forward_validate per assegurar que la millora generalitza.
   c. Si passa el ratchet i NO està sobreajustada → submit_proposal amb
      rationale causal en català.

── ESTIL DE LA RATIONALE (CATALÀ) ─────────────────────────────────────────
- Concís: 3-5 frases.
- Causal: "perquè X provoca Y, i aquest canvi mitiga Y reduint Z".
- Concret: cita exit_reason, regime, ticker o dates dels closed_positions.
- Vocabulari: rebot, correcció, tendència, drawdown, sortida, entrada.
- Evita castellanismes ("tenir que" → "haver de"; "inclús" → "fins i tot").

Comença ara amb l'estratègia que t'indiqui l'usuari.
""".replace("{max_proposals}", str(MAX_PROPOSALS_PER_STRATEGY))


# ── Tool dispatcher ───────────────────────────────────────────────────────

def _dispatch(tool_name: str, tool_input: dict) -> str:
    """Execute a tool by name and return its result as a string.

    submit_proposal is special — it persists to Supabase and returns a
    confirmation. Other tools are pure read/compute.
    """
    if tool_name == "get_simulated_closed_positions":
        return get_simulated_closed_positions(
            tool_input["strategy"],
            tool_input.get("limit", 200),
        )
    if tool_name == "get_real_closed_positions":
        return get_real_closed_positions(
            tool_input["strategy"],
            tool_input.get("lookback_days", 365),
        )
    if tool_name == "get_strategy_params":
        return get_strategy_params(tool_input["strategy"])
    if tool_name == "simulate_param_change":
        return simulate_param_change(
            tool_input["strategy"],
            tool_input["param_overrides"],
            start=tool_input.get("start"),
            end=tool_input.get("end"),
        )
    if tool_name == "walk_forward_validate":
        return walk_forward_validate(
            tool_input["strategy"],
            tool_input["param_overrides"],
        )
    if tool_name == "submit_proposal":
        return _submit_proposal(tool_input)
    return json.dumps({"error": f"unknown tool: {tool_name}"})


def _submit_proposal(args: dict) -> str:
    """Validate + persist a single RuleProposal.

    Validation:
      - param_name must be in BOUNDED_RANGES
      - proposed_value must be within bounds
      - rationale must be non-empty and >= 50 chars
      - we recompute passes_ratchet from the supplied summaries
    """
    name = args.get("param_name", "")
    rng = BOUNDED_RANGES.get(name)
    if rng is None:
        return json.dumps({"error": f"param {name!r} is frozen (not in BOUNDED_RANGES)"})

    proposed = float(args["proposed_value"])
    lo, hi, _ = rng
    if not (lo <= proposed <= hi):
        return json.dumps({"error": f"{name}={proposed} out of bounds [{lo}, {hi}]"})

    rationale = (args.get("rationale") or "").strip()
    if len(rationale) < 50:
        return json.dumps({"error": "rationale too short — explain causally why this change helps"})

    bt_summary  = args.get("backtest_summary")     or {}
    wf_summary  = args.get("walk_forward_summary") or {}
    if not isinstance(bt_summary, dict) or not isinstance(wf_summary, dict):
        return json.dumps({"error": "summaries must be JSON objects"})

    # Recompute ratchet ourselves from the provided summaries
    # walk-forward 'test' block contains baseline + proposed; if structure
    # differs we tolerate flat dicts too.
    if "baseline" in wf_summary and "proposed" in wf_summary:
        passes = compute_ratchet(wf_summary["baseline"], wf_summary["proposed"])
    else:
        passes = False

    strategy = args["strategy"]
    with get_session() as s:
        # Defensive cap: don't allow more than MAX pending proposals per strategy in one batch
        n_pending = (
            s.query(RuleProposal)
            .filter(RuleProposal.strategy == strategy, RuleProposal.status == "pending")
            .count()
        )
        if n_pending >= MAX_PROPOSALS_PER_STRATEGY * 2:  # 2x soft cap for safety
            return json.dumps({"error": f"too many pending proposals for {strategy}; resolve some first"})

        proposal = RuleProposal(
            strategy=strategy,
            param_name=name,
            current_value=float(args["current_value"]),
            proposed_value=proposed,
            rationale=rationale,
            backtest_summary=bt_summary,
            walk_forward_summary=wf_summary,
            passes_ratchet=passes,
            status="pending",
        )
        s.add(proposal)
        s.commit()
        proposal_id = proposal.id

    log.info(
        "submit_proposal: id=%d %s %s %.4f → %.4f passes_ratchet=%s",
        proposal_id, strategy, name,
        float(args["current_value"]), proposed, passes,
    )
    return json.dumps({
        "proposal_id":    proposal_id,
        "passes_ratchet": passes,
        "status":         "pending",
    })


# ── The agent loop ────────────────────────────────────────────────────────

def run_critic_for_strategy(strategy: str, *, max_iterations: int = 30) -> dict:
    """Run the Strategy Critic for one strategy.

    Returns a dict with summary stats: number of proposals submitted,
    number rejected by validation, total iterations used.
    """
    if strategy not in ("rsi_compounder", "trend_momentum"):
        raise ValueError(f"unknown strategy: {strategy}")

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    user_message = (
        f"Analitza l'estratègia '{strategy}' i proposa fins a "
        f"{MAX_PROPOSALS_PER_STRATEGY} canvis numèrics que millorin el "
        f"rendiment ajustat al risc. Segueix el procediment del system prompt: "
        f"primer carrega les dades, després identifica hipòtesis causals, valida "
        f"cadascuna amb walk_forward_validate, i només envia les que passin el "
        f"ratchet test i no estiguin sobreajustades."
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]

    n_proposals_submitted = 0
    n_validation_errors   = 0

    log.info("strategy_critic: starting agent for strategy=%s", strategy)

    # ── Prompt caching ──────────────────────────────────────────────────
    # The system prompt and tool definitions are identical across every
    # iteration. Marking them with cache_control lets Anthropic serve them
    # from cache (5-min TTL) at ~10% of full input price. With 10-12
    # iterations per critic run, this typically saves 30-40% of total cost.
    #
    # Reference: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
    cached_system = [{
        "type":          "text",
        "text":          _SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]
    # Mark the LAST tool definition with cache_control — this caches all
    # tools above it (the cache breakpoint covers everything before it
    # within the same scope).
    cached_tools = [dict(t) for t in TOOL_DEFINITIONS]
    cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

    for iteration in range(1, max_iterations + 1):
        response = client.messages.create(
            model="claude-sonnet-4-5-20251001",
            max_tokens=4096,
            system=cached_system,
            tools=cached_tools,
            messages=messages,
        )

        # cache_read_input_tokens / cache_creation_input_tokens are the cache
        # hit/miss counters from the Anthropic Usage object — log them so we
        # can verify caching is actually working in production.
        usage = response.usage
        log.info(
            "strategy_critic[%s] iter=%d stop=%s in=%d out=%d "
            "cache_read=%d cache_create=%d",
            strategy, iteration, response.stop_reason,
            usage.input_tokens, usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", 0),
            getattr(usage, "cache_creation_input_tokens", 0),
        )

        if response.stop_reason == "end_turn":
            log.info(
                "strategy_critic[%s]: done in %d iterations, %d proposal(s) submitted, %d rejected",
                strategy, iteration, n_proposals_submitted, n_validation_errors,
            )
            return {
                "strategy":             strategy,
                "iterations":           iteration,
                "proposals_submitted":  n_proposals_submitted,
                "validation_errors":    n_validation_errors,
            }

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                log.info(
                    "strategy_critic[%s] tool=%s input=%s",
                    strategy, block.name,
                    json.dumps(block.input)[:200],
                )
                result = _dispatch(block.name, block.input)
                # Track submit_proposal outcomes
                if block.name == "submit_proposal":
                    parsed = json.loads(result)
                    if parsed.get("error"):
                        n_validation_errors += 1
                    elif parsed.get("proposal_id"):
                        n_proposals_submitted += 1
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result,
                })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user",      "content": tool_results})
            continue

        log.warning("strategy_critic[%s]: unexpected stop_reason=%s", strategy, response.stop_reason)
        break

    log.error("strategy_critic[%s]: hit max_iterations=%d", strategy, max_iterations)
    return {
        "strategy":             strategy,
        "iterations":           max_iterations,
        "proposals_submitted":  n_proposals_submitted,
        "validation_errors":    n_validation_errors,
        "warning":              "max_iterations reached",
    }
