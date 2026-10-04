"""Manual smoke test for the school agent.

Usage:
    python scripts/run_school_agent.py
(reads ANTHROPIC_API_KEY from .env; Gmail auth uses the already-cached .secrets/ token)
"""

from __future__ import annotations

import asyncio
import json
import logging

from dotenv import load_dotenv

# Must run before importing personal_agent modules — several read config from os.environ
# at module import time, so .env has to be loaded first, not inside main().
load_dotenv()

from personal_agent.agents.base import run_with_retry  # noqa: E402
from personal_agent.agents.school_agent import run_school_agent  # noqa: E402


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = await run_with_retry("school", run_school_agent, timeout_s=60.0)
    print(json.dumps(result.__dict__, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
