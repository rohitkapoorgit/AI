"""School sub-agent — reviews today's Procare (son's school/daycare app) notifications
and returns what he needs to bring, know, or prepare for.

Same shape as email_agent.py: genuinely agentic (how many messages to open depends on
what's there), tool-scoped to only Procare's own search tool (never the general
search_emails), 24h window enforced server-side in gmail_server.py.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from anthropic import AsyncAnthropic

from personal_agent.agents.base import MalformedReportError
from personal_agent.mcp_client import list_anthropic_tools, mcp_session, parse_tool_result

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You are the school sub-agent in a morning digest system, covering \
Procare (son's school/daycare app) notifications. Using search_procare_emails and \
get_email, review today's Procare messages and identify anything he needs to bring, \
know, or prepare — permission slips, items to pack, schedule changes, closures, picture \
day, etc. The server already scopes search_procare_emails to the last 24 hours and to \
Procare's own sender — don't add your own filters. Don't open every message — read the \
subject/snippet first, and only call get_email when that isn't enough to know what's \
needed. If there's genuinely nothing actionable, say so plainly rather than padding the \
report with routine confirmations. When done, call submit_school_report exactly once."""

_SUBMIT_TOOL = {
    "name": "submit_school_report",
    "description": "Submit your final Procare/school summary. Call this exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": (
                    "One-sentence takeaway, e.g. 'Nothing needed today' or "
                    "'Bring a jacket for the field trip.'"
                ),
            },
            "action_needed": {
                "type": "boolean",
                "description": "True if there's something to prepare/bring/remember today.",
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["subject", "summary"],
                },
                "description": "Only include items worth mentioning — omit routine, no-action notices.",
            },
        },
        "required": ["headline", "action_needed", "items"],
    },
}

_KICKOFF_MESSAGE = "Review today's Procare messages and submit your report."
_MAX_TOOL_ROUNDS = 6

_GMAIL_SERVER_ARGS = ["-m", "personal_agent.mcp_servers.gmail_server"]
# Tool-scoped in code, not by prompting alone: this agent never sees the general
# search_emails tool, only Procare's own — same principle as the email agent excluding
# "send" tools.
_EXCLUDED_TOOLS = {"search_emails"}


class SchoolAgentError(MalformedReportError):
    """Raised when the model's report is malformed, truncated, or never submitted."""


def _validate_report(data: dict) -> None:
    if not isinstance(data.get("headline"), str) or not data["headline"]:
        raise SchoolAgentError(f"malformed report: bad headline: {data!r}")
    if not isinstance(data.get("action_needed"), bool):
        raise SchoolAgentError(f"malformed report: bad action_needed: {data!r}")
    items = data.get("items")
    if not isinstance(items, list):
        raise SchoolAgentError(f"malformed report: bad items: {data!r}")
    for item in items:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(k), str) and item[k] for k in ("subject", "summary")
        ):
            raise SchoolAgentError(f"malformed report: bad item entry: {item!r}")


async def run_school_agent(*, model: str = DEFAULT_MODEL, client: Any | None = None) -> tuple[dict, dict]:
    """Run the school agent's tool-use loop once. Returns (report, usage)."""
    anthropic_client = client or AsyncAnthropic()

    async with mcp_session(command=sys.executable, args=_GMAIL_SERVER_ARGS) as session:
        gmail_tools = await list_anthropic_tools(session, exclude=_EXCLUDED_TOOLS)
        tools = [*gmail_tools, _SUBMIT_TOOL]

        messages: list[dict] = [{"role": "user", "content": _KICKOFF_MESSAGE}]
        llm_calls = 0
        input_tokens = 0
        output_tokens = 0

        for _round in range(1, _MAX_TOOL_ROUNDS + 1):
            response = await anthropic_client.messages.create(
                model=model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            llm_calls += 1
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            submit_blocks = [b for b in tool_use_blocks if b.name == "submit_school_report"]
            if submit_blocks and response.stop_reason == "max_tokens":
                raise SchoolAgentError("response hit max_tokens while submitting — possibly truncated")
            if submit_blocks:
                report = submit_blocks[0].input
                _validate_report(report)
                usage = {
                    "mode": "agentic",
                    "model": model,
                    "llm_calls": llm_calls,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
                return report, usage

            if not tool_use_blocks:
                break  # model replied with text only, never submitted — treat as a failure below

            tool_results = []
            for block in tool_use_blocks:
                try:
                    mcp_result = await session.call_tool(block.name, block.input)
                    output = parse_tool_result(mcp_result)
                except Exception as exc:
                    output = {"error": str(exc)}
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(output)}
                )
            messages.append({"role": "user", "content": tool_results})

    raise SchoolAgentError("school agent did not submit a report within the round limit")
