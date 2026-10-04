"""Email sub-agent — reviews today's Gmail (via a local Gmail MCP server) and returns a
structured summary.

Unlike weather/BART, this one is genuinely agentic: how many emails to look at and which
ones are worth opening depends on what's actually in the inbox today — the model decides,
round by round, rather than following a fixed sequence of tool calls known in advance.

The 24h time window is enforced server-side (gmail_server.py), not left to the prompt —
see that module's SEARCH_WINDOW. Excluding Procare mail (owned by the school sub-agent
instead) is TBD until the real sender address is known — see README open questions.
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

SYSTEM_PROMPT = """You are the email sub-agent in a morning digest system. Using \
search_emails and get_email, review today's email and classify what matters. The server \
already scopes search_emails to the last 24 hours and excludes most promotional/marketing \
mail — don't add your own date or category filter. Focus only on legitimate mail: personal \
correspondence, account/security notices, receipts, and anything actionable. If something \
still reads as promotional or marketing despite that filtering (a sale, a newsletter, a \
product announcement), ignore it — don't call get_email on it and don't include it in your \
report, even if it looks time-sensitive (a "sale ends today" email is still marketing). \
Don't open every email — read the subject/snippet from search_emails first, and only call \
get_email when that isn't enough to classify it. When done, call submit_email_report \
exactly once."""

_SUBMIT_TOOL = {
    "name": "submit_email_report",
    "description": "Submit your final email summary. Call this exactly once, after reviewing today's email.",
    # No `strict: true` — tested and reverted, see weather_agent.py's comment on the same
    # decision (it made Sonnet 5's generation less reliable on this project, not more).
    # A malformed report (e.g. "items" coming back as a string instead of an array — this
    # did happen once in testing) is instead caught by _validate_report below.
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "One-sentence overall takeaway, e.g. 'Quiet day, one reply needed.'",
            },
            "total_reviewed": {
                "type": "integer",
                "description": "How many emails you looked at (from search_emails results).",
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "from": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": ["urgent", "needs_reply", "fyi", "ignorable"],
                        },
                        "summary": {"type": "string"},
                    },
                    "required": ["subject", "from", "category", "summary"],
                },
                "description": "Only include emails worth mentioning — omit pure ignorable spam/promo.",
            },
        },
        "required": ["headline", "total_reviewed", "items"],
    },
}

_KICKOFF_MESSAGE = "Review today's email and submit your report."
_MAX_TOOL_ROUNDS = 8  # higher than weather/BART — genuinely open-ended, depends on inbox volume

_GMAIL_SERVER_ARGS = ["-m", "personal_agent.mcp_servers.gmail_server"]

_VALID_CATEGORIES = {"urgent", "needs_reply", "fyi", "ignorable"}


class EmailAgentError(MalformedReportError):
    """Raised when the model's report is malformed, truncated, or never submitted."""


def _validate_report(data: dict) -> None:
    if not isinstance(data.get("headline"), str) or not data["headline"]:
        raise EmailAgentError(f"malformed report: bad headline: {data!r}")
    if not isinstance(data.get("total_reviewed"), int):
        raise EmailAgentError(f"malformed report: bad total_reviewed: {data!r}")
    items = data.get("items")
    if not isinstance(items, list):
        raise EmailAgentError(f"malformed report: bad items: {data!r}")
    for item in items:
        if (
            not isinstance(item, dict)
            or not all(isinstance(item.get(k), str) and item[k] for k in ("subject", "from", "summary"))
            or item.get("category") not in _VALID_CATEGORIES
        ):
            raise EmailAgentError(f"malformed report: bad item entry: {item!r}")


async def run_email_agent(*, model: str = DEFAULT_MODEL, client: Any | None = None) -> tuple[dict, dict]:
    """Run the email agent's tool-use loop once. Returns (report, usage)."""
    anthropic_client = client or AsyncAnthropic()

    async with mcp_session(command=sys.executable, args=_GMAIL_SERVER_ARGS) as session:
        gmail_tools = await list_anthropic_tools(session)
        tools = [*gmail_tools, _SUBMIT_TOOL]

        messages: list[dict] = [{"role": "user", "content": _KICKOFF_MESSAGE}]
        llm_calls = 0
        input_tokens = 0
        output_tokens = 0

        for _round in range(1, _MAX_TOOL_ROUNDS + 1):
            response = await anthropic_client.messages.create(
                model=model,
                # A busy inbox needs a longer items array than weather/BART's fixed reports.
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            llm_calls += 1
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            submit_blocks = [b for b in tool_use_blocks if b.name == "submit_email_report"]
            if submit_blocks and response.stop_reason == "max_tokens":
                raise EmailAgentError("response hit max_tokens while submitting — possibly truncated")
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

    raise EmailAgentError("email agent did not submit a report within the round limit")
