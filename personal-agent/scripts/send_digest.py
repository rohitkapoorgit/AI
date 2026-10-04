"""Runs the full pipeline (all sub-agents + composer) and sends the result by email.

This is the script a daily cron/launchd job would actually call.

Usage:
    python scripts/send_digest.py
(reads DIGEST_RECIPIENT from .env; first run opens a browser for the gmail.send consent)
"""

from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

# Must run before importing personal_agent modules — several read config (API keys,
# recipient addresses, file paths) from os.environ at module import time, so .env has to
# be loaded first, not inside main(). This bit us once already: DIGEST_RECIPIENT came
# back None on the first real run because delivery.py read it at import time, before
# load_dotenv() (then inside main()) had run.
load_dotenv()

from personal_agent.delivery import send_digest_run  # noqa: E402
from personal_agent.orchestrator import run_full_digest  # noqa: E402


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    run = await run_full_digest()
    result = send_digest_run(run)

    if run.alert:
        print(f"Sent ALERT email (id={result.get('id')}) — see logs for which agents failed.")
    elif run.digest_text:
        print(f"Sent digest email (id={result.get('id')}).")
    else:
        print(f"Sent composer-failure ALERT email (id={result.get('id')}).")


if __name__ == "__main__":
    asyncio.run(main())
