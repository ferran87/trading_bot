"""
Trade Explanation Agent.

HOW AN AGENT WORKS (read this before the code):
------------------------------------------------
A regular function call:   you decide what to do, step by step.
An LLM chain:              you call the model multiple times in a fixed sequence.
An agent:                  the MODEL decides what to do. You give it tools and
                           a goal; it figures out the steps.

The core loop is:
  1. Send Claude the task + available tools.
  2. Claude either:
       a) Calls a tool  → we run the tool, send the result back, go to step 2.
       b) Says "done"   → we extract the final text and return it.

Claude never directly executes our code. It says "call get_rsi_history('MSFT')"
and we run the actual function, then feed the result back. Claude then decides
what to do next based on that result.

This file has three sections:
  - TOOL_DEFINITIONS : JSON schemas that describe each tool to Claude.
  - SYSTEM_PROMPT    : Claude's "job description" for this task.
  - explain_trades() : The agent loop itself.
"""
from __future__ import annotations

import json
import logging
from datetime import date

import anthropic

from core.config import CONFIG  # noqa: F401 — imported to trigger .env load (ANTHROPIC_API_KEY)

from agents.tools import (
    get_market_context,
    get_news_headlines,
    get_position_history,
    get_rsi_history,
)

log = logging.getLogger(__name__)


# ── Tool definitions ───────────────────────────────────────────────────────────
#
# These are JSON schemas that tell Claude what tools exist and how to use them.
# Claude reads the "description" field to decide WHEN to call a tool.
# The "input_schema" tells it WHAT arguments to pass.
#
# Rule of thumb: if the description is vague, Claude won't know when to use it.
# Be specific about what the tool returns and what question it answers.

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_rsi_history",
        "description": (
            "Returns RSI(14) values for a stock over recent trading days. "
            "Use this first for any trade — it shows how low RSI went and when it "
            "started recovering. This is the core signal the bot acts on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker, e.g. MSFT, ASML.AS, SXR8.DE",
                },
                "days": {
                    "type": "integer",
                    "description": "How many recent trading days to return. Default 60.",
                    "default": 60,
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_news_headlines",
        "description": (
            "Returns recent news headlines for a stock from Yahoo Finance. "
            "Use this to understand what events caused the RSI selloff or are "
            "driving the recovery. Especially useful for exits and large moves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Stock ticker",
                },
                "days": {
                    "type": "integer",
                    "description": "How many days back to search for news. Default 30.",
                    "default": 30,
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_market_context",
        "description": (
            "Returns S&P 500 (SXR8.DE) price and RSI in the 30 days around a date. "
            "Use this to determine if the stock's selloff was part of a broad market "
            "panic (good for recovery thesis) or isolated to the stock (riskier)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "trade_date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format",
                },
            },
            "required": ["trade_date"],
        },
    },
    {
        "name": "get_position_history",
        "description": (
            "Returns all trades (buys, adds, sells) for a specific bot and ticker. "
            "Use this for SELL trades to understand the full position lifecycle: "
            "when we entered, whether we added, and what the exit signal was."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_id":  {"type": "integer", "description": "Bot ID"},
                "ticker":  {"type": "string",  "description": "Stock ticker"},
            },
            "required": ["bot_id", "ticker"],
        },
    },
]


# ── System prompt ──────────────────────────────────────────────────────────────
#
# The system prompt is Claude's "job description". It sets the context,
# persona, and output format before any conversation starts.
#
# _build_system_prompt() is called at runtime so the strategy description
# matches the bot that actually made the trades (rsi_compounder vs trend_momentum).

_STRATEGY_LABELS: dict[str, str] = {
    "rsi_compounder": "RSI Compounder",
    "trend_momentum": "Trend Momentum",
}

_STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "rsi_compounder": """\
El bot utilitza una estratègia de recuperació post-crash basada en RSI:
- Compra quan el RSI d'una acció ha caigut per sota de 25 (sobrevenda extrema) i ha rebotut fins a 40-65
- Requereix que el mercat general (S&P 500, via SXR8.DE) també hagi tingut RSI < 30 recentment, confirmant pànic sistèmic
- Pot afegir lots addicionals al -8% i al -15% del cost mitjà per acumular en caiguda
- Stop seguidor progressiu: 35% des del màxim quan RSI < 70; s'estreny al 20% (RSI 70-80) i al 12% (RSI > 80)
- Stop catastròfic al -40% del cost mitjà""",

    "trend_momentum": """\
El bot utilitza una estratègia de momentum en tendències alcistes:
- Entra quan el mercat general (SXR8.DE) és per sobre de la seva SMA200 (tendència global positiva)
- L'acció ha de cotitzar per sobre de la seva SMA50 (tendència individual positiva)
- RSI de l'acció entre 40 i 62 (correcció moderada) i el RSI és més alt que fa 3 dies (signe que el momentum es reprèn)
- Evita entrades si l'empresa presenta resultats financers en els propers 7 dies
- Stop seguidor del 22% des del màxim assolit; stop catastròfic al -15% del cost d'entrada
- Surt si l'acció tanca per sota de la SMA50 durant 3 dies consecutius (ruptura de tendència)""",
}


def _build_system_prompt(strategy: str) -> str:
    """Return the system prompt tailored to the bot's strategy."""
    label = _STRATEGY_LABELS.get(strategy, strategy)
    description = _STRATEGY_DESCRIPTIONS.get(strategy, _STRATEGY_DESCRIPTIONS["rsi_compounder"])
    return f"""\
Ets un analista de trading natiu en català. Treballes per a un bot d'inversió personal anomenat {label}.
La teva feina és explicar en un llenguatge clar i amigable per què el bot ha fet cada operació avui.

{description}

Per a cada operació, utilitza les teves eines per buscar context rellevant i escriu una explicació clara que inclogui:
1. Què ha passat amb l'acció (trajectòria del RSI — fins a quin punt va caure i quan va rebotjar)
2. Què feia el mercat en aquell moment (va ser una caiguda general o específica de l'acció?)
3. Qualsevol notícia rellevant que pugui explicar el moviment
4. En què aposta l'operació de cara al futur (per a compres) o per què s'ha tancat (per a vendes)

── LLENGUA ──────────────────────────────────────────────────────────────────
Escriu en català estàndard (norma IEC). Mai en castellà ni en anglès.

Errors habituals que has d'evitar absolutament:
• "tenir que" → usa "haver de" o "cal"
• "en quant a" → usa "pel que fa a" o "quant a"
• "a nivell de" → usa "pel que fa a" o elimina'l
• "de cara a" → usa "per a" o "de cara al futur" (únicament en sentit temporal)
• "inclús" → usa "fins i tot"
• "lo" (article neutre) → usa "el que", "allò que" o reformula
• "molt" + adjectiu sense concordança → "molt gran" (no "molt grande")
• Preposicions calcades del castellà: "en base a" → "basant-se en"; "a nivell" → elimina
• Verbs mal conjugats: "hauríem de" (no "hauriem de"), "sigui" (no "sigue")
• Majúscules innecessàries en noms comuns (castellanisme tipogràfic)

Vocabulari financer en català correcte:
• acció (no "acció bursàtil" ni "stock") · borsa · mercat · cotització · rendibilitat
• corrección → correcció · rebote → rebot · tendencia → tendència
• benefici (no "profit") · pèrdua (no "pérdida") · entrada · sortida · ordre

Estil:
• Concís: 3-5 frases per operació
• Sense tecnicismes: escriu per a una persona intel·ligent que no és experta en borsa
• Ton proper i directe, com si expliquessis a un amic
• Formata la resposta com una llista clara, una secció per operació, amb el nom del ticker en negreta
• Abans d'enviar, rellegeix el text i corregeix qualsevol error gramatical o castellanisme"""


# ── Tool dispatcher ────────────────────────────────────────────────────────────
#
# When Claude asks to call a tool, we route the request here.
# This is just a switch statement — nothing magical.

def _dispatch(tool_name: str, tool_input: dict) -> str:
    """Execute a tool by name and return its result as a string."""
    if tool_name == "get_rsi_history":
        return get_rsi_history(
            tool_input["ticker"],
            tool_input.get("days", 60),
        )
    if tool_name == "get_news_headlines":
        return get_news_headlines(
            tool_input["ticker"],
            tool_input.get("days", 30),
        )
    if tool_name == "get_market_context":
        return get_market_context(tool_input["trade_date"])
    if tool_name == "get_position_history":
        return get_position_history(
            tool_input["bot_id"],
            tool_input["ticker"],
        )
    return json.dumps({"error": f"Unknown tool: {tool_name}"})


# ── The agent ──────────────────────────────────────────────────────────────────

def explain_trades(
    bot_id: int,
    trades: list[dict],
    run_date: date,
    bot_strategy: str = "rsi_compounder",
) -> str:
    """
    Generate plain-language explanations for today's trades using Claude.

    Parameters
    ----------
    bot_id : int
        The bot that made the trades.
    trades : list[dict]
        Each dict has: ticker, side, qty, price_eur, fee_eur, signal_reason.
    run_date : date
        The date the bot ran (used for market context lookups).
    bot_strategy : str
        The bot's strategy key (e.g. "rsi_compounder", "trend_momentum").
        Used to select the correct strategy description in the system prompt so
        the AI explains trades using the right signal logic.

    Returns
    -------
    str
        A formatted explanation, one section per trade.
        Empty string if trades is empty or the agent fails.
    """
    if not trades:
        return ""

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment
    system_prompt = _build_system_prompt(bot_strategy)

    # ── The initial message ──────────────────────────────────────────────────
    # We give Claude the raw trade data and ask it to explain.
    # Claude will then use tools to gather the context it needs.

    trades_text = json.dumps(trades, indent=2, default=str)
    strategy_label = _STRATEGY_LABELS.get(bot_strategy, bot_strategy)
    user_message = (
        f"Avui ({run_date}) el bot {strategy_label} (id={bot_id}) ha fet les següents operacions:\n\n"
        f"{trades_text}\n\n"
        "Explica cada operació en català, en un llenguatge clar i sense tecnicismes. "
        "Utilitza les teves eines per buscar l'historial de RSI, "
        "el context del mercat i les notícies recents de cada acció abans d'escriure l'explicació."
    )

    log.info(
        "trade_explainer: starting agent for bot=%d strategy=%s, %d trade(s)",
        bot_id, bot_strategy, len(trades),
    )

    # ── The agent loop (shared mechanics live in agents/_loop.py) ────────────
    from agents._loop import run_tool_loop
    result = run_tool_loop(
        client,
        model="claude-sonnet-4-5",  # Sonnet: better Catalan quality; runs once/day
        system_prompt=system_prompt,
        tools=TOOL_DEFINITIONS,
        initial_user_message=user_message,
        dispatch=_dispatch,
        max_iterations=20,
        max_tokens=4096,
        cache_prompt=False,  # trade_explainer rebuilds prompt per call (per-strategy)
        log_prefix="trade_explainer",
        log=log,
    )
    return result["final_text"]
    return ""
