"""Orchestrator — runs every registered sub-agent concurrently under run_with_retry,
so one agent's failure or slowness can't affect another. Plain code, not an LLM: which
agents to run and how to retry them is fixed control flow, not a judgment call. See
README.md "Orchestrator: plain code, not an LLM".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from personal_agent.agents.bart_agent import run_bart_agent
from personal_agent.agents.base import AgentResult, run_with_retry
from personal_agent.agents.composer import run_composer
from personal_agent.agents.email_agent import run_email_agent
from personal_agent.agents.school_agent import run_school_agent
from personal_agent.agents.weather_agent import run_weather_agent

logger = logging.getLogger(__name__)

# Each entry is (agent_name, coroutine_factory, timeout_s). Add new sub-agents here as
# they're built — nothing else in this module needs to change for a new agent to join
# the concurrent run. Email/school get a longer timeout: both are genuinely open-ended
# (up to 6-8 tool-use rounds depending on mailbox content), unlike weather/BART's fixed
# 1-2 tool calls.
AGENTS: list[tuple[str, object, float]] = [
    ("weather", run_weather_agent, 30.0),
    ("bart", run_bart_agent, 30.0),
    ("email", run_email_agent, 60.0),
    ("school", run_school_agent, 60.0),
]

# Kill switch: if fewer than this many agents succeed, the run is considered too broken
# to compose a useful digest from. Threshold is a placeholder — see README open questions.
MIN_SUCCESSFUL_AGENTS = 3


async def run_digest() -> list[AgentResult]:
    """Run every registered sub-agent concurrently, isolating failures per-agent."""
    results = await asyncio.gather(
        *(run_with_retry(name, factory, timeout_s=timeout_s) for name, factory, timeout_s in AGENTS)
    )
    for result in results:
        logger.info(
            "%s: status=%s attempts=%d latency=%.2fs mode=%s llm_calls=%d "
            "tokens=%d in / %d out cost=$%.4f",
            result.agent,
            result.status,
            result.attempts,
            result.latency_s,
            result.mode,
            result.llm_calls,
            result.input_tokens,
            result.output_tokens,
            result.estimated_cost_usd,
        )
    return list(results)


def should_alert_instead_of_digest(results: list[AgentResult]) -> bool:
    """True if too few agents succeeded to bother composing a digest — see kill switch."""
    succeeded = sum(1 for r in results if r.status == "ok")
    return succeeded < MIN_SUCCESSFUL_AGENTS


@dataclass
class DigestRun:
    results: list[AgentResult]
    composer_result: AgentResult | None  # None when the kill switch skipped composing
    alert: bool  # True if too few agents succeeded — no digest was composed

    @property
    def digest_text(self) -> str | None:
        if self.composer_result and self.composer_result.status == "ok":
            return self.composer_result.data["digest"]
        return None


async def run_full_digest() -> DigestRun:
    """Run every sub-agent, then compose — unless the kill switch says not to bother."""
    results = await run_digest()

    if should_alert_instead_of_digest(results):
        succeeded = sum(1 for r in results if r.status == "ok")
        logger.warning(
            "Only %d/%d agents succeeded (need %d) — skipping composer, alert instead.",
            succeeded,
            len(results),
            MIN_SUCCESSFUL_AGENTS,
        )
        return DigestRun(results=results, composer_result=None, alert=True)

    composer_result = await run_with_retry("composer", lambda: run_composer(results), timeout_s=30.0)
    return DigestRun(results=results, composer_result=composer_result, alert=False)
