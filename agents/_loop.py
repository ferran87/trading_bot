"""Shared Claude tool-use loop for the project's agents.

All four agents in this package (``strategy_critic``, ``portfolio_manager``,
``strategist``, ``trade_explainer``) repeat the same skeleton:

    iteration = 0
    while iteration < max_iter:
        response = client.messages.create(model, system, tools, messages, ...)
        log usage
        if stop_reason == "end_turn":
            collect text from response.content blocks → return
        if stop_reason == "tool_use":
            for each tool_use block: dispatch → collect tool_result
            append assistant message + user tool_results
        else: log + break

The only per-agent variation is:
  - the system prompt
  - the list of tools and the dispatcher function
  - whether to cache the system prompt / last tool (prompt caching)
  - max_tokens (4 096 or 8 096)
  - a log label prefix
  - whether to swallow ``anthropic.APIError`` (strategist does, others don't)
  - an optional ``on_tool_result`` callback for per-tool tracking
    (e.g. ``strategy_critic`` counts how many ``submit_proposal`` calls
    succeeded vs. failed validation)

This module factors out the mechanics; each agent stays focused on its own
prompt + tools + post-processing.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

import anthropic

OnToolResult = Callable[[str, dict, str], None]
"""Callback signature: (tool_name, tool_input, result_string) → None."""

Dispatcher = Callable[[str, dict], str]
"""Callback signature: (tool_name, tool_input) → result_string (usually JSON)."""


def run_tool_loop(
    client: anthropic.Anthropic,
    *,
    model: str,
    system_prompt: str,
    tools: list[dict],
    initial_user_message: str,
    dispatch: Dispatcher,
    max_iterations: int = 30,
    max_tokens: int = 4096,
    cache_prompt: bool = True,
    on_tool_result: OnToolResult | None = None,
    log_prefix: str = "agent",
    log: logging.Logger | None = None,
    swallow_api_errors: bool = False,
) -> dict[str, Any]:
    """Run a Claude tool-use loop until ``end_turn`` or ``max_iterations``.

    Parameters
    ----------
    client
        Pre-constructed Anthropic client (reads ``ANTHROPIC_API_KEY``).
    model
        e.g. ``"claude-sonnet-4-5"``.
    system_prompt
        Plain string.  Wrapped with cache_control when ``cache_prompt=True``.
    tools
        Anthropic tool definitions.  When ``cache_prompt=True`` the last entry
        is annotated with ``cache_control={"type":"ephemeral"}`` to cache the
        full tools prefix.  The original list is not mutated.
    initial_user_message
        The first user turn.
    dispatch
        ``dispatch(tool_name, tool_input) -> result_str``.  Result is sent
        back to Claude as the ``tool_result.content``.
    max_iterations
        Hard cap on iterations to prevent runaway loops.
    max_tokens
        Per-iteration token budget.
    cache_prompt
        Whether to apply prompt-caching to the system prompt + tools prefix.
    on_tool_result
        Optional callback fired after every tool dispatch; used by callers to
        track per-tool stats (e.g. how many submit_proposal succeeded).
    log_prefix
        Prefix for log lines (e.g. ``"strategy_critic"``).  Helps grep logs.
    log
        Logger to use.  Defaults to a module-level logger.
    swallow_api_errors
        When ``True``, ``anthropic.APIError`` is caught and reported in the
        returned ``errors`` list instead of bubbling up.

    Returns
    -------
    dict with keys:
        - ``final_text`` (str)  — accumulated text from the final end_turn
        - ``iterations`` (int)  — how many iterations were used
        - ``stop_reason`` (str | None) — the last response's stop_reason
        - ``completed`` (bool)  — True iff loop exited via ``end_turn``
        - ``errors`` (list[str]) — API errors caught (only if swallow_api_errors)
    """
    log = log or logging.getLogger(__name__)

    if cache_prompt:
        cached_system: Any = [{
            "type":          "text",
            "text":          system_prompt,
            "cache_control": {"type": "ephemeral"},
        }]
        if tools:
            cached_tools = [dict(t) for t in tools]
            cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
        else:
            cached_tools = tools
    else:
        cached_system = system_prompt
        cached_tools = tools

    messages: list[dict] = [{"role": "user", "content": initial_user_message}]
    errors: list[str] = []
    final_text = ""
    stop_reason: str | None = None
    completed = False

    for iteration in range(1, max_iterations + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=cached_system,
                tools=cached_tools,
                messages=messages,
            )
        except anthropic.APIError as e:
            if not swallow_api_errors:
                raise
            log.error("%s: API error at iteration %d: %s", log_prefix, iteration, e)
            errors.append(f"APIError at iteration {iteration}: {e}")
            break

        usage = response.usage
        log.info(
            "%s iter=%d stop=%s in=%d out=%d cache_read=%d cache_create=%d",
            log_prefix, iteration, response.stop_reason,
            usage.input_tokens, usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", 0),
            getattr(usage, "cache_creation_input_tokens", 0),
        )
        stop_reason = response.stop_reason

        if stop_reason == "end_turn":
            final_text = "\n".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            log.info("%s: done in %d iteration(s), %d chars",
                     log_prefix, iteration, len(final_text))
            completed = True
            break

        if stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                log.info("%s tool=%s input=%s",
                         log_prefix, block.name, json.dumps(block.input)[:200])
                result = dispatch(block.name, block.input)
                if on_tool_result is not None:
                    try:
                        on_tool_result(block.name, block.input, result)
                    except Exception as cb_exc:  # noqa: BLE001  callbacks must never break the loop
                        log.warning("%s on_tool_result callback failed: %s",
                                    log_prefix, cb_exc)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result,
                })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user",      "content": tool_results})
            continue

        log.warning("%s: unexpected stop_reason=%s", log_prefix, stop_reason)
        break
    else:
        log.error("%s: hit max_iterations=%d without finishing",
                  log_prefix, max_iterations)
        errors.append(f"Hit max_iterations={max_iterations}")

    return {
        "final_text":  final_text,
        "iterations":  iteration,
        "stop_reason": stop_reason,
        "completed":   completed,
        "errors":      errors,
    }
