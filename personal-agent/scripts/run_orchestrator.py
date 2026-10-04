"""Manual smoke test for the orchestrator — runs all registered sub-agents concurrently.

Usage:
    python scripts/run_orchestrator.py
(reads ANTHROPIC_API_KEY / WEATHER_API_CONTACT / BART_API_KEY from .env)
"""

from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

# Must run before importing personal_agent modules — several read config from os.environ
# at module import time, so .env has to be loaded first, not inside main().
load_dotenv()

from personal_agent.orchestrator import run_digest, should_alert_instead_of_digest  # noqa: E402


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    results = await run_digest()

    print(f"\n{'agent':<10}{'status':<8}{'mode':<14}{'attempts':<9}{'latency_s':<11}"
          f"{'llm_calls':<11}{'in_tok':<8}{'out_tok':<8}{'cost_usd':<10}")
    total_latency = 0.0
    total_cost = 0.0
    for r in results:
        print(
            f"{r.agent:<10}{r.status:<8}{r.mode:<14}{r.attempts:<9}{r.latency_s:<11.2f}"
            f"{r.llm_calls:<11}{r.input_tokens:<8}{r.output_tokens:<8}{r.estimated_cost_usd:<10.4f}"
        )
        total_latency += r.latency_s
        total_cost += r.estimated_cost_usd
    print(f"\nsum of per-agent latency: {total_latency:.2f}s (agents ran concurrently, "
          f"so wall-clock was less)")
    print(f"total estimated cost: ${total_cost:.4f}")

    if should_alert_instead_of_digest(results):
        print("\nKILL SWITCH: too few agents succeeded — would send an alert, not a digest.")


if __name__ == "__main__":
    asyncio.run(main())
