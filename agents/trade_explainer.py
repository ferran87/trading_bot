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

SYSTEM_PROMPT = """Ets un analista de trading per a un bot d'inversió personal anomenat RSI Compounder.
La teva feina és explicar en un llenguatge clar i amigable per què el bot ha fet cada operació avui.

El bot utilitza una estratègia basada en RSI:
- Compra quan el RSI d'una acció cau per sota de 25 (sobrevenda severa) i es recupera fins a 40-65
- Requereix que el mercat general (S&P 500) també hagi estat sobrevenut, confirmant pànic sistèmic
- Utilitza un stop seguidor progressiu: 35% → 20% → 12% a mesura que el RSI supera 70 i 80
- Pot afegir a les posicions al -8% i -15% per reduir el cost mitjà

Per a cada operació, utilitza les teves eines per buscar context rellevant i escriu una explicació clara que inclogui:
1. Què ha passat amb l'acció (trajectòria del RSI — com de baix va caure, quan es va recuperar)
2. Què feia el mercat en aquell moment (va ser una caiguda general o específica de l'acció?)
3. Qualsevol notícia rellevant que pugui explicar el moviment
4. En què aposta l'operació de cara al futur (per a compres) o per què s'ha tancat (per a vendes)

Escriu en català. Sigues concís — 3-5 frases per operació.
Evita el jargó tècnic. Escriu per a una persona intel·ligent que no és trader.
Formata la resposta com una llista clara, una secció per operació."""


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

def explain_trades(bot_id: int, trades: list[dict], run_date: date) -> str:
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

    Returns
    -------
    str
        A formatted explanation, one section per trade.
        Empty string if trades is empty or the agent fails.
    """
    if not trades:
        return ""

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment

    # ── The initial message ──────────────────────────────────────────────────
    # We give Claude the raw trade data and ask it to explain.
    # Claude will then use tools to gather the context it needs.

    trades_text = json.dumps(trades, indent=2, default=str)
    user_message = (
        f"Avui ({run_date}) el bot (id={bot_id}) ha fet les següents operacions:\n\n"
        f"{trades_text}\n\n"
        "Explica cada operació en llenguatge clar. "
        "Utilitza les teves eines per buscar l'historial de RSI, "
        "el context del mercat i les notícies recents de cada acció abans d'escriure l'explicació."
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]

    log.info("trade_explainer: starting agent for bot=%d, %d trade(s)", bot_id, len(trades))

    # ── The agent loop ───────────────────────────────────────────────────────
    #
    # This is the heart of how agents work:
    #
    #   iteration 1: Claude sees the trades, decides to call get_rsi_history("MSFT")
    #   iteration 2: we return RSI data, Claude calls get_news_headlines("MSFT")
    #   iteration 3: we return news, Claude calls get_market_context("2026-04-25")
    #   iteration 4: we return market data, Claude has enough → writes explanation
    #
    # We never tell Claude what order to look things up. It decides.
    # The loop only ends when Claude sets stop_reason = "end_turn".

    iteration = 0
    max_iterations = 20  # safety cap — prevents infinite loops

    while iteration < max_iterations:
        iteration += 1

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # fast + cheap; explanations run after every trade day
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        log.debug(
            "trade_explainer: iteration=%d stop_reason=%s input_tokens=%d output_tokens=%d",
            iteration, response.stop_reason,
            response.usage.input_tokens, response.usage.output_tokens,
        )

        # ── Case 1: Claude is done ───────────────────────────────────────────
        if response.stop_reason == "end_turn":
            explanation = "\n".join(
                block.text
                for block in response.content
                if hasattr(block, "text")
            )
            log.info(
                "trade_explainer: done in %d iteration(s), %d chars",
                iteration, len(explanation),
            )
            return explanation

        # ── Case 2: Claude wants to call tools ───────────────────────────────
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                log.info(
                    "trade_explainer: tool_call tool=%s input=%s",
                    block.name, json.dumps(block.input),
                )
                result = _dispatch(block.name, block.input)

                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result,
                })

            # Feed the assistant's message (with tool calls) + our results back
            # This is how Claude "sees" the tool output on the next iteration
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user",      "content": tool_results})
            continue

        # ── Case 3: Unexpected stop reason ───────────────────────────────────
        log.warning("trade_explainer: unexpected stop_reason=%s", response.stop_reason)
        break

    log.error("trade_explainer: hit max_iterations=%d without finishing", max_iterations)
    return ""
