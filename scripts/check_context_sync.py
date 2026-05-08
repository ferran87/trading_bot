"""
PostToolUse hook — fires after every Edit/Write tool call.

Claude Code injects this script's stdout back into the AI's context as a
system reminder. If the edited file is "high-impact" (core logic, config,
strategies), the script prints a checklist of context files that may need
updating before the session is declared done.

Non-high-impact edits (tests, dashboard, scripts, docs themselves) produce
no output so the hook is invisible for routine work.
"""
from __future__ import annotations

import json
import re
import sys


# Files whose edits should trigger a context-sync reminder.
HIGH_IMPACT_PATTERNS = [
    r"[/\\]core[/\\].+\.py$",
    r"[/\\]config[/\\].+\.yaml$",
    r"[/\\]strategies[/\\].+\.py$",
    r"[/\\]main\.py$",
]

CONTEXT_FILES = [
    "AGENTS.md          — bot table, broker list, pitfalls",
    "docs/CONTEXT_FOR_AI.md — active bots, env vars, broker backends",
    "docs/DECISIONS.md  — add entry if this introduces a new architectural decision or non-obvious behavior",
    "memory/MEMORY.md   — add entry if this is a persistent fact worth remembering next session",
]


def main() -> None:
    try:
        data: dict = json.load(sys.stdin)
    except Exception:
        return  # malformed input — stay silent

    path: str = data.get("tool_input", {}).get("file_path", "")
    if not path:
        return

    # Normalise Windows backslashes for pattern matching.
    norm = path.replace("\\", "/")

    if not any(re.search(p, norm) for p in HIGH_IMPACT_PATTERNS):
        return  # not a high-impact file — no reminder needed

    print(
        f"\n[CONTEXT SYNC] You edited '{path}'.\n"
        "Before declaring this task done, check whether any of these need updating:\n"
        + "\n".join(f"  • {f}" for f in CONTEXT_FILES)
        + "\nOnly update files where the content has genuinely changed."
    )


if __name__ == "__main__":
    main()
