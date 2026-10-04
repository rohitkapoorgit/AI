"""Manual smoke test for the BART agent.

Usage:
    python scripts/run_bart_agent.py
(reads ANTHROPIC_API_KEY / BART_API_KEY from .env, see .env.example)
"""

from __future__ import annotations

import asyncio
import json
import logging

from dotenv import load_dotenv

# Must run before importing personal_agent modules — several read config from os.environ
# at module import time, so .env has to be loaded first, not inside main().
load_dotenv()

from personal_agent.agents.bart_agent import run_bart_agent  # noqa: E402
from personal_agent.agents.base import run_with_retry  # noqa: E402


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = await run_with_retry("bart", run_bart_agent)
    print(json.dumps(result.__dict__, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
