"""Weather sub-agent — checks the NWS forecast for SF and Dublin, CA, returns a structured report.

Tool-use loop: call the model -> execute any tool calls -> feed results back -> repeat,
until the model calls submit_weather_report. Forcing the final answer through a tool call
(rather than parsing free-form prose) is the same trick the healthcare project's `end_call`
tool used — it makes the output reliably structured for the (future) composer agent to consume.
"""

from __future__ import annotations

import json
import os
from typing import Any

from anthropic import AsyncAnthropic

from personal_agent.agents.base import MalformedReportError
from personal_agent.tools import weather

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You are the weather-reporting sub-agent in a morning digest system. \
Check today's forecast for both San Francisco and Dublin, CA using the get_forecast tool \
(call it once per location), then call submit_weather_report exactly once with a concise, \
practical report. Base your advice only on the forecast data you actually looked up — \
don't invent conditions."""

_SUBMIT_TOOL = {
    "name": "submit_weather_report",
    "description": (
        "Submit your final weather report. Call this exactly once, after checking the "
        "forecast for both locations."
    ),
    # No `strict: true` here — tested and reverted. On this schema it made Sonnet 5's
    # generation highly inconsistent (constrained decoding occasionally burned far more
    # output tokens than the unconstrained version, truncating the report before it
    # finished — reproduced at both max_tokens=1024 and 2048, while >10 unconstrained
    # runs stayed under 350 tokens). _validate_report below does the shape-checking
    # instead, at no generation cost.
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "One-sentence overall takeaway, e.g. contrasting the two cities.",
            },
            "locations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "high_f": {"type": "integer"},
                        "low_f": {"type": "integer"},
                        "condition": {"type": "string"},
                        "precipitation_chance": {"type": ["integer", "null"]},
                        "advice": {
                            "type": "string",
                            "description": "e.g. 'bring an umbrella', 'light jacket in the morning'",
                        },
                    },
                    "required": ["name", "condition", "advice"],
                },
            },
        },
        "required": ["headline", "locations"],
    },
}

_KICKOFF_MESSAGE = "Check today's weather for both locations and submit your report."
_MAX_TOOL_ROUNDS = 4


class WeatherAgentError(MalformedReportError):
    """Raised when the model's report is malformed, truncated, or never submitted."""


def _validate_report(data: dict) -> None:
    if not isinstance(data.get("headline"), str) or not data["headline"]:
        raise WeatherAgentError(f"malformed report: bad headline: {data!r}")
    locations = data.get("locations")
    if not isinstance(locations, list) or not locations:
        raise WeatherAgentError(f"malformed report: bad locations: {data!r}")
    for loc in locations:
        if not isinstance(loc, dict) or not all(
            isinstance(loc.get(k), str) and loc[k] for k in ("name", "condition", "advice")
        ):
            raise WeatherAgentError(f"malformed report: bad location entry: {loc!r}")


async def run_weather_agent(*, model: str = DEFAULT_MODEL, client: Any | None = None) -> tuple[dict, dict]:
    """Run the weather agent's tool-use loop once. Returns (report, usage)."""
    anthropic_client = client or AsyncAnthropic()
    messages: list[dict] = [{"role": "user", "content": _KICKOFF_MESSAGE}]
    tools = [weather.TOOL_SCHEMA, _SUBMIT_TOOL]

    llm_calls = 0
    input_tokens = 0
    output_tokens = 0

    for _round in range(1, _MAX_TOOL_ROUNDS + 1):
        response = await anthropic_client.messages.create(
            model=model,
            max_tokens=1536,  # unconstrained reports observed at 100-350 tokens; ample margin
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        llm_calls += 1
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        messages.append({"role": "assistant", "content": response.content})

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        submit_blocks = [b for b in tool_use_blocks if b.name == "submit_weather_report"]
        if submit_blocks and response.stop_reason == "max_tokens":
            # A tool_use block can still be present when generation was cut off mid-JSON.
            # Don't trust a possibly-truncated report as if it were complete.
            raise WeatherAgentError("response hit max_tokens while submitting — possibly truncated")
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
                output = await weather.get_forecast(**block.input)
            except Exception as exc:
                output = {"error": str(exc)}
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(output)}
            )
        messages.append({"role": "user", "content": tool_results})

    raise WeatherAgentError("weather agent did not submit a report within the round limit")
