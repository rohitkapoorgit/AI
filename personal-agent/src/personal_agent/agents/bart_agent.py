"""BART sub-agent — checks real-time departures and service advisories for the
Dublin/Pleasanton -> Embarcadero/Montgomery commute, returns a structured report.

Same tool-use loop pattern as weather_agent.py: real tools do work (get_departures,
get_service_advisories), submit_bart_report is a fake tool used only to force a
schema-validated structured return instead of parsing free-form prose.
"""

from __future__ import annotations

import json
import os
from typing import Any

from anthropic import AsyncAnthropic

from personal_agent.agents.base import MalformedReportError
from personal_agent.tools import bart

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You are the BART sub-agent in a morning digest system, covering the \
Dublin/Pleasanton to Embarcadero/Montgomery commute (Blue Line, direct, no transfer). \
Call get_departures and get_service_advisories (each once), then call submit_bart_report \
exactly once. Only flag an advisory as affecting the commute if it actually names the Blue \
Line or one of these stations — don't alarm about unrelated lines. Base everything on the \
tool data; don't invent delays."""

_SUBMIT_TOOL = {
    "name": "submit_bart_report",
    "description": "Submit your final BART commute report. Call this exactly once.",
    # No `strict: true` — see weather_agent.py's comment on the same decision. Tested
    # here too; _validate_report does the shape-checking instead, at no generation cost.
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "One-sentence takeaway, e.g. 'Normal service, next train in 8 min.'",
            },
            "next_departures": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "minutes": {"type": "string"},
                        "destination": {"type": "string"},
                    },
                    "required": ["minutes", "destination"],
                },
            },
            "delay_affecting_commute": {"type": "boolean"},
            "advice": {
                "type": "string",
                "description": (
                    "e.g. 'Normal service' or 'Leave 15 min early — Blue Line delays reported.'"
                ),
            },
        },
        "required": ["headline", "next_departures", "delay_affecting_commute", "advice"],
    },
}

_KICKOFF_MESSAGE = (
    "Check BART departures and advisories for the Dublin/Pleasanton to "
    "Embarcadero/Montgomery commute, then submit your report."
)
_MAX_TOOL_ROUNDS = 4

_TOOL_IMPLS = {
    "get_departures": bart.get_departures,
    "get_service_advisories": bart.get_service_advisories,
}


class BartAgentError(MalformedReportError):
    """Raised when the model's report is malformed, truncated, or never submitted."""


def _validate_report(data: dict) -> None:
    if not isinstance(data.get("headline"), str) or not data["headline"]:
        raise BartAgentError(f"malformed report: bad headline: {data!r}")
    if not isinstance(data.get("next_departures"), list):
        raise BartAgentError(f"malformed report: bad next_departures: {data!r}")
    for dep in data["next_departures"]:
        if not isinstance(dep, dict) or not all(
            isinstance(dep.get(k), str) and dep[k] for k in ("minutes", "destination")
        ):
            raise BartAgentError(f"malformed report: bad departure entry: {dep!r}")
    if not isinstance(data.get("delay_affecting_commute"), bool):
        raise BartAgentError(f"malformed report: bad delay_affecting_commute: {data!r}")
    if not isinstance(data.get("advice"), str) or not data["advice"]:
        raise BartAgentError(f"malformed report: bad advice: {data!r}")


async def run_bart_agent(*, model: str = DEFAULT_MODEL, client: Any | None = None) -> tuple[dict, dict]:
    """Run the BART agent's tool-use loop once. Returns (report, usage)."""
    anthropic_client = client or AsyncAnthropic()
    messages: list[dict] = [{"role": "user", "content": _KICKOFF_MESSAGE}]
    tools = [bart.GET_DEPARTURES_SCHEMA, bart.GET_ADVISORIES_SCHEMA, _SUBMIT_TOOL]

    llm_calls = 0
    input_tokens = 0
    output_tokens = 0

    for _round in range(1, _MAX_TOOL_ROUNDS + 1):
        response = await anthropic_client.messages.create(
            model=model,
            max_tokens=1536,  # unconstrained reports observed well under this; ample margin
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        llm_calls += 1
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        messages.append({"role": "assistant", "content": response.content})

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        submit_blocks = [b for b in tool_use_blocks if b.name == "submit_bart_report"]
        if submit_blocks and response.stop_reason == "max_tokens":
            raise BartAgentError("response hit max_tokens while submitting — possibly truncated")
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
                output = await _TOOL_IMPLS[block.name](**block.input)
            except Exception as exc:
                output = {"error": str(exc)}
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(output)}
            )
        messages.append({"role": "user", "content": tool_results})

    raise BartAgentError("BART agent did not submit a report within the round limit")
