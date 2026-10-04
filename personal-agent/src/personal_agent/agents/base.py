"""Shared agent result type and the timeout/retry wrapper every sub-agent runs under.

Every sub-agent returns an AgentResult (never raises past this layer), so the future
orchestrator can run several agents concurrently and treat success/failure uniformly —
one agent's exception must not take down the others. See README.md "Failure & retry design".
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from anthropic import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from personal_agent.observability import estimate_cost_usd

logger = logging.getLogger(__name__)


class MalformedReportError(RuntimeError):
    """A sub-agent's submit_*_report tool call didn't match its expected shape.

    Observed in testing to be a real, if occasional, one-off — not a structural bug in
    our code — so it's worth a fresh attempt rather than failing immediately. Each
    agent's own *AgentError subclasses this; see their _validate_report functions.
    """


# Errors worth retrying: transient network/API hiccups, plus a malformed report (see
# MalformedReportError above). Anything else (bad auth, our own logic errors) fails
# fast — retrying won't fix it.
RETRYABLE_EXCEPTIONS = (
    asyncio.TimeoutError,
    httpx.TimeoutException,
    httpx.ConnectError,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
    MalformedReportError,
)


@dataclass
class AgentResult:
    agent: str
    status: str  # "ok" | "error"
    data: dict[str, Any] | None = None
    error: str | None = None
    latency_s: float = 0.0
    attempts: int = 0
    # Observability, mainly to compare Phase 1 (agentic tool-loop) vs. Phase 2
    # (deterministic fetch + single LLM synthesis) sub-agent implementations.
    mode: str = "agentic"  # "agentic" | "deterministic"
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


async def run_with_retry(
    agent_name: str,
    coro_factory: Callable[[], Awaitable[tuple[dict, dict]]],
    *,
    timeout_s: float = 30.0,
    max_attempts: int = 3,
    backoff_s: float = 2.0,
) -> AgentResult:
    """Run one sub-agent's coroutine under a timeout, retrying only transient failures.

    `coro_factory` is called fresh on each attempt (a coroutine object can only be
    awaited once) and must return `(data, usage)` on success, or raise. `usage` is a
    dict with `model`, `llm_calls`, `input_tokens`, `output_tokens`, and `mode` — see
    each agent's own return statement for exactly what it reports.
    """
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        started_at = time.monotonic()
        try:
            data, usage = await asyncio.wait_for(coro_factory(), timeout=timeout_s)
            return AgentResult(
                agent=agent_name,
                status="ok",
                data=data,
                latency_s=time.monotonic() - started_at,
                attempts=attempt,
                mode=usage.get("mode", "agentic"),
                llm_calls=usage.get("llm_calls", 0),
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                estimated_cost_usd=estimate_cost_usd(
                    usage.get("model"), usage.get("input_tokens", 0), usage.get("output_tokens", 0)
                ),
            )
        except RETRYABLE_EXCEPTIONS as exc:
            last_error = exc
            logger.warning(
                "%s attempt %d/%d failed (retryable): %s", agent_name, attempt, max_attempts, exc
            )
            if attempt < max_attempts:
                await asyncio.sleep(backoff_s * attempt)
        except Exception as exc:
            logger.error("%s failed (non-retryable): %s", agent_name, exc)
            return AgentResult(
                agent=agent_name,
                status="error",
                error=str(exc),
                latency_s=time.monotonic() - started_at,
                attempts=attempt,
            )

    return AgentResult(
        agent=agent_name,
        status="error",
        error=str(last_error),
        attempts=max_attempts,
    )
