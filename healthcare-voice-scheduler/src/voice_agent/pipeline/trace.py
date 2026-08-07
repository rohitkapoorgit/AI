"""Structured JSONL event trace for scripts/trace_dashboard.py.

Separate from loguru's human-readable terminal narration in voice_bot_claude_agent.py — this is
the machine-readable feed the dashboard reads. One JSON object per line, each with a timestamp
and a stage tag. Deliberately has no Pipecat import, so trace_dashboard.py (which imports this)
doesn't trip the nltk/cwd gotcha documented in docs/PHASE2_VOICE.md and can run from inside the
repo normally, unlike the bot scripts.
"""

import json
import os
import time
from pathlib import Path

TRACE_FILE = Path(os.environ.get("VOICE_BOT_TRACE_FILE", "/tmp/voice_bot_trace.jsonl"))


def reset_trace() -> None:
    """Start a fresh trace — called once per new call, so each test call's dashboard view
    doesn't show events left over from the previous one."""
    TRACE_FILE.write_text("")


def record_event(stage: str, **fields) -> None:
    event = {"ts": time.time(), "stage": stage, **fields}
    with TRACE_FILE.open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")
