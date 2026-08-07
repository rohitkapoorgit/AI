#!/usr/bin/env python3
"""Run an end-to-end call in the terminal: no Twilio/Deepgram/Cartesia accounts
needed, but this DOES call the real Anthropic API. The agent (ClaudeToolAgent)
is real; the phone audio (ConsoleVoiceChannel, stdin/stdout) stays mocked.

The scheduling backend is chosen via SCHEDULING_BACKEND (see
voice_agent.scheduling.factory) -- defaults to MockSchedulingBackend (no
external calls beyond Anthropic). Set SCHEDULING_BACKEND=sandbox to use
SandboxSchedulingBackend instead (real NPPES + FHIR sandbox calls -- see
docs/PHASE2_3_SCHEDULING_BACKEND.md).

Requires ANTHROPIC_API_KEY in .env (see .env.example).

Usage: python scripts/run_console_call.py
       SCHEDULING_BACKEND=sandbox python scripts/run_console_call.py
Type 'bye' to hang up.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from voice_agent.agent.claude_agent import ClaudeToolAgent
from voice_agent.pipeline.console_channel import ConsoleVoiceChannel
from voice_agent.pipeline.runner import run_call
from voice_agent.scheduling.factory import build_backend
from voice_agent.scheduling.service import SchedulingService


async def main() -> None:
    backend = build_backend()
    try:
        service = SchedulingService(backend)
        agent = ClaudeToolAgent(service)
        channel = ConsoleVoiceChannel()
        await run_call(agent, channel)
    finally:
        await backend.aclose()


if __name__ == "__main__":
    asyncio.run(main())
