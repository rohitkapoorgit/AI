"""Runs the full pipeline: all sub-agents concurrently, then the composer — everything
except real delivery (not built yet; this just prints the digest).

Usage:
    python scripts/run_digest.py
"""

from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

# Must run before importing personal_agent modules — several read config from os.environ
# at module import time, so .env has to be loaded first, not inside main().
load_dotenv()

from personal_agent.orchestrator import run_full_digest  # noqa: E402


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    run = await run_full_digest()

    print(f"\n{'agent':<10}{'status':<8}{'mode':<12}{'attempts':<9}{'latency_s':<11}cost_usd")
    for r in run.results:
        print(f"{r.agent:<10}{r.status:<8}{r.mode:<12}{r.attempts:<9}{r.latency_s:<11.2f}{r.estimated_cost_usd:.4f}")

    if run.composer_result:
        c = run.composer_result
        print(f"{'composer':<10}{c.status:<8}{c.mode:<12}{c.attempts:<9}{c.latency_s:<11.2f}{c.estimated_cost_usd:.4f}")

    total_cost = sum(r.estimated_cost_usd for r in run.results) + (
        run.composer_result.estimated_cost_usd if run.composer_result else 0.0
    )
    print(f"\ntotal estimated cost: ${total_cost:.4f}")

    if run.alert:
        print("\nALERT: too few agents succeeded — no digest composed. Check manually.")
    elif run.digest_text:
        print("\n----- DIGEST -----\n")
        print(run.digest_text)
    else:
        print(f"\nComposer failed: {run.composer_result.error if run.composer_result else 'unknown'}")


if __name__ == "__main__":
    asyncio.run(main())
