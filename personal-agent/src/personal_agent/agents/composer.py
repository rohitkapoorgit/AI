"""Composer — a single LLM call, no tools, that synthesizes all sub-agent reports into
one digest.

This is where cross-agent reasoning happens (e.g. rain + a BART delay + a school item to
pack -> "leave 15 min early") — the one thing no sub-agent can do alone, since each only
sees its own domain. Structurally this is exactly the "single-call synthesis" pattern
Phase 2 will apply to weather/BART (see README "Phase 1 vs Phase 2") — the composer is
already a working example of it.
"""

from __future__ import annotations

import json
import os
from typing import Any

from anthropic import AsyncAnthropic

from personal_agent.agents.base import AgentResult

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You are the composer for a personal morning digest. You'll be given \
structured reports from several sub-agents (weather, BART commute, email, school) as \
JSON — some may have failed. Write one email-ready digest a busy parent can read in \
under 30 seconds before heading out the door.

Rules:
- Lead with a one-line "what matters most today" summary that combines information \
across agents where there's a genuine connection (e.g. rain + a commute delay + a school \
item to pack means "leave 15 minutes early and grab a jacket") — don't just concatenate \
each agent's report separately when there's a real connection between them.
- Then give each section (Weather, Commute, Email, School) briefly — a few lines each, \
not a wall of text. Skip a section entirely if that agent failed.
- If any agent failed, add one line at the end naming which section(s) are missing and \
that they should be checked manually. Don't hide failures or pretend the digest is complete.
- Never invent data. Only use what's in the reports you were given — if a report is thin \
(e.g. "nothing needed today"), say that plainly rather than padding it.
- Plain text only — no markdown, this is going straight into an email body.
- Keep the whole thing under 200 words excluding the failure note."""


def _build_user_message(results: list[AgentResult]) -> str:
    sections = []
    for r in results:
        if r.status == "ok":
            sections.append(f"## {r.agent} (ok)\n{json.dumps(r.data)}")
        else:
            sections.append(f"## {r.agent} (FAILED)\nerror: {r.error}")
    return "Here are today's sub-agent reports:\n\n" + "\n\n".join(sections)


async def run_composer(
    results: list[AgentResult], *, model: str = DEFAULT_MODEL, client: Any | None = None
) -> tuple[dict, dict]:
    """Single LLM call, no tools. Returns ({"digest": text}, usage).

    Wrapped as (dict, dict) rather than returning the string directly so this fits the
    same run_with_retry contract every sub-agent uses — a transient failure here (e.g. a
    rate limit) is just as worth retrying as one in a sub-agent.
    """
    anthropic_client = client or AsyncAnthropic()
    user_message = _build_user_message(results)

    response = await anthropic_client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()

    usage = {
        "mode": "synthesis",  # no tools, no loop — one call, distinct from "agentic"/"deterministic"
        "model": model,
        "llm_calls": 1,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return {"digest": text}, usage
