"""Claude Sonnet 5 tool-calling agent — the production AgentOrchestrator implementation.

The only thing that's "real" here is the LLM call and the tool-calling loop; whichever
SchedulingBackend the injected SchedulingService wraps (mock or real NPPES/FHIR) is opaque to
this class, per the swap-point pattern in docs/ARCHITECTURE.md.

Anthropic's tool-use protocol needs the full wire-format message history, including tool_use /
tool_result content blocks, to stay coherent across turns — more than the simple Turn(role, text)
dataclass in orchestrator.py can represent. So this class keeps its own `_messages` list as the
source of truth for one call, rather than reconstructing state from the `history` argument (the
same "own internal state per call instance" pattern MockAgent used).
"""

import json
import os
import time
from typing import Any

from anthropic import AsyncAnthropic

from voice_agent.agent.orchestrator import AgentOrchestrator, AgentResult, Turn
from voice_agent.scheduling.service import TOOL_SCHEMAS, SchedulingService

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You are a warm, efficient scheduling assistant answering the phone for a \
healthcare clinic. Help the caller find a doctor and book an appointment using the tools \
available to you — never invent doctors, availability, or appointment details; always call a \
tool to look them up.

Conversation rules:
- Keep responses short and conversational — this is a phone call, not a chat window. No lists, \
no markdown, no more than two sentences unless reading back a small set of options.
- Ask one question at a time.
- Before booking, confirm the doctor, time, and the patient's name back to the caller.
- After a successful booking, ask whether to confirm the appointment now.
- Call end_call once the conversation is genuinely finished — after the caller confirms or \
declines confirmation, or if they want to stop for any other reason. Say a brief goodbye first.
- If a tool call fails (e.g. a slot became unavailable), tell the caller plainly and offer to \
try another option."""

_END_CALL_TOOL = {
    "name": "end_call",
    "description": (
        "Call this once the conversation is complete and it's time to hang up — e.g. after the "
        "appointment is booked and the caller has confirmed or declined confirmation, or if the "
        "caller wants to stop."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

_KICKOFF_MESSAGE = "(The caller has just connected. Greet them and ask how you can help.)"
_MAX_TOOL_ROUNDS = 6


class ClaudeToolAgent(AgentOrchestrator):
    def __init__(
        self,
        service: SchedulingService,
        *,
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
    ):
        self._service = service
        self._model = model
        self._client = client or AsyncAnthropic()
        self._messages: list[dict] = []

    async def run(self, history: list[Turn], user_text: str) -> AgentResult:
        self._messages.append({"role": "user", "content": user_text or _KICKOFF_MESSAGE})

        tool_calls_log: list[dict] = []
        llm_calls_log: list[dict] = []
        reply_text = ""

        for round_num in range(1, _MAX_TOOL_ROUNDS + 1):
            started_at = time.monotonic()
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=[*TOOL_SCHEMAS, _END_CALL_TOOL],
                messages=self._messages,
            )
            llm_calls_log.append({"round": round_num, "latency_s": time.monotonic() - started_at})
            self._messages.append({"role": "assistant", "content": response.content})

            reply_text = "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
            ends_call = any(block.name == "end_call" for block in tool_use_blocks)
            scheduling_calls = [block for block in tool_use_blocks if block.name != "end_call"]

            if ends_call or not scheduling_calls:
                if not reply_text:
                    # Claude occasionally ends a turn with no text (e.g. right after a
                    # silent tool call) — dead air is worse on a phone call than a filler.
                    reply_text = "Goodbye!" if ends_call else "One moment."
                return AgentResult(
                    reply_text=reply_text,
                    tool_calls=tool_calls_log,
                    llm_calls=llm_calls_log,
                    end_call=ends_call,
                )

            tool_results = []
            for block in scheduling_calls:
                tool_started_at = time.monotonic()
                output = await getattr(self._service, block.name)(**block.input)
                tool_calls_log.append(
                    {
                        "name": block.name,
                        "input": block.input,
                        "output": output,
                        "latency_s": time.monotonic() - tool_started_at,
                    }
                )
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(output)}
                )
            self._messages.append({"role": "user", "content": tool_results})

        return AgentResult(
            reply_text=reply_text or "Sorry, let me look into that and get back to you.",
            tool_calls=tool_calls_log,
            llm_calls=llm_calls_log,
            end_call=False,
        )
