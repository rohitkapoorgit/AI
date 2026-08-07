"""Chooses which SchedulingBackend implementation to construct, via the SCHEDULING_BACKEND env
var ("mock", the default, or "sandbox"). Every composition root (scripts/run_console_call.py,
pipeline/voice_bot_claude_agent.py, pipeline/voice_bot_native_llm.py) calls this instead of
hardcoding MockSchedulingBackend(), so there's exactly one place this decision is made and one
toggle that applies everywhere. See docs/PHASE2_3_SCHEDULING_BACKEND.md.
"""

import os

from voice_agent.scheduling.backend import SchedulingBackend
from voice_agent.scheduling.mock_backend import MockSchedulingBackend
from voice_agent.scheduling.sandbox_backend import SandboxSchedulingBackend

_CHOICES = {
    "mock": MockSchedulingBackend,
    "sandbox": SandboxSchedulingBackend,
}


def build_backend() -> SchedulingBackend:
    choice = os.environ.get("SCHEDULING_BACKEND", "mock").strip().lower()
    try:
        backend_cls = _CHOICES[choice]
    except KeyError:
        raise ValueError(
            f"Unknown SCHEDULING_BACKEND={choice!r}; expected one of {sorted(_CHOICES)}"
        ) from None
    return backend_cls()
