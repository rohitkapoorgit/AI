"""Phase 2.1 — plug the existing, tested ClaudeToolAgent directly into a Pipecat pipeline.

Simpler and more conservative than Phase 2.2 (voice_bot_native_llm.py): reuses the exact same
agent, tool-dispatch, and system prompt already proven by scripts/run_console_call.py and
tests/test_claude_agent.py (17 passing tests), at the cost of two things Pipecat's native
AnthropicLLMService gives for free — per-token streaming to TTS (ClaudeAgentProcessor waits for
ClaudeToolAgent's full reply before speaking any of it) and native mid-generation barge-in. See
"The architectural shift" in docs/PHASE2_VOICE.md for the full trade-off writeup. The point of
building this first is to validate the audio plumbing (Deepgram STT/TTS, Pipecat wiring) against
an agent whose behavior is already known-good, before trusting Phase 2.2's different mechanics.

Because ClaudeToolAgent keeps its own internal conversation state (see agent/claude_agent.py's
module docstring), this pipeline skips Pipecat's LLMContext/LLMContextAggregatorPair entirely —
there's no native LLM node here for them to feed into. Turn-taking is instead driven directly by
Deepgram STT's own finalized-transcript signal (TranscriptionFrame.finalized), not Pipecat's
VAD-based aggregator logic.

Logging: every stage narrates what it's doing at INFO level (what the caller said, each Claude API
round-trip and each tool call with their own latency, what's about to be spoken) so a live test
call can be followed entirely from the terminal, with a per-turn summary breaking total time into
LLM time / tool time / overhead. All of this timing only covers our own processing — nothing here
starts a clock until a finalized transcript arrives, so caller think-time is never counted. Interim
(still-being-transcribed) STT output is DEBUG-only — pass -v to the dev runner to see it. Per-LLM-
call and per-tool-call timing is measured inside ClaudeToolAgent itself (agent/claude_agent.py) and
exposed via AgentResult.llm_calls/tool_calls, since neither is a native Pipecat service Pipecat can
instrument on its own; STT/TTS timing comes from Pipecat's own metrics via MetricsFrameLogger.
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndTaskFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams

from voice_agent.agent.claude_agent import ClaudeToolAgent
from voice_agent.agent.orchestrator import Turn
from voice_agent.pipeline.observability import MetricsFrameLogger
from voice_agent.pipeline.trace import record_event, reset_trace
from voice_agent.scheduling.factory import build_backend
from voice_agent.scheduling.service import SchedulingService

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_REPO_ROOT / ".env", override=True)

_FALLBACK_ERROR_REPLY = "Sorry, I'm having trouble right now — could you say that again?"


class ClaudeAgentProcessor(FrameProcessor):
    """Bridges Pipecat's frame stream to ClaudeToolAgent.run().

    Reacts only to finalized TranscriptionFrames — InterimTranscriptionFrame and unfinalized
    TranscriptionFrames are dropped, not forwarded. Letting raw caller speech reach `tts`
    downstream would make the bot echo the caller's own words back at them, so neither
    transcription frame type is ever pushed onward; only our own reply (as a TTSSpeakFrame) is.
    """

    def __init__(self, agent: ClaudeToolAgent):
        super().__init__()
        self._agent = agent
        self._history: list[Turn] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterimTranscriptionFrame):
            logger.debug(f"[STT interim] {frame.text!r}")
            return  # never forward — partial text isn't something to act on or speak back

        if isinstance(frame, TranscriptionFrame):
            if frame.text.strip():
                logger.info(f"[STT final] Caller said: {frame.text!r}")
                record_event("stt_final", text=frame.text)
                await self._run_turn(frame.text)
            return  # never forward raw transcripts to tts

        await self.push_frame(frame, direction)

    async def speak_greeting(self) -> None:
        await self._run_turn("")

    async def _run_turn(self, user_text: str) -> None:
        if user_text:
            self._history.append(Turn(role="user", text=user_text))

        logger.info(f"[Agent] Calling ClaudeToolAgent (user_text={user_text!r})")
        turn_started_at = time.monotonic()
        try:
            result = await self._agent.run(self._history, user_text)
        except Exception as exc:  # noqa: BLE001 -- any failure here must still get a spoken reply, not dead air
            logger.exception("[Agent] ClaudeToolAgent.run() raised — speaking a fallback reply")
            record_event("error", message=str(exc))
            await self.push_frame(TTSSpeakFrame(_FALLBACK_ERROR_REPLY))
            return
        turn_elapsed = time.monotonic() - turn_started_at

        for call in result.llm_calls:
            logger.info(f"[LLM] round {call['round']}: {call['latency_s']:.2f}s")
            record_event("llm_call", round=call["round"], latency_s=call["latency_s"])
        for call in result.tool_calls:
            logger.info(
                f"[Tool] {call['name']}({call['input']}) -> {call['output']} "
                f"[{call['latency_s']:.2f}s]"
            )
            record_event(
                "tool_call",
                name=call["name"],
                input=call["input"],
                output=call["output"],
                latency_s=call["latency_s"],
            )

        self._history.append(Turn(role="assistant", text=result.reply_text))

        llm_total = sum(c["latency_s"] for c in result.llm_calls)
        tool_total = sum(c["latency_s"] for c in result.tool_calls)
        overhead = turn_elapsed - llm_total - tool_total
        logger.info(
            f"[Turn] total {turn_elapsed:.2f}s = {llm_total:.2f}s LLM "
            f"({len(result.llm_calls)} call(s)) + {tool_total:.2f}s tools "
            f"({len(result.tool_calls)} call(s)) + {overhead:.2f}s overhead. "
            f"end_call={result.end_call}. Reply: {result.reply_text!r}"
        )

        logger.info(f"[TTS] Speaking: {result.reply_text!r}")
        record_event("tts_speak", text=result.reply_text)

        # turn_summary recorded last -- the dashboard uses it as the "this turn is complete"
        # boundary, so anything meant to belong to this turn must be recorded before it.
        record_event(
            "turn_summary",
            total_s=turn_elapsed,
            llm_s=llm_total,
            tool_s=tool_total,
            overhead_s=overhead,
            llm_call_count=len(result.llm_calls),
            tool_call_count=len(result.tool_calls),
            end_call=result.end_call,
            reply=result.reply_text,
        )

        await self.push_frame(TTSSpeakFrame(result.reply_text))
        if result.end_call:
            logger.info("[Call] end_call requested — hanging up")
            await self.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    logger.info("Starting voice bot (Phase 2.1 -- ClaudeToolAgent)")
    reset_trace()

    backend = build_backend()
    service = SchedulingService(backend)
    agent = ClaudeToolAgent(service)
    claude_processor = ClaudeAgentProcessor(agent)
    logger.debug(f"Constructed SchedulingService ({type(backend).__name__}) and ClaudeToolAgent")

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = DeepgramTTSService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    logger.debug("Constructed DeepgramSTTService and DeepgramTTSService")

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            claude_processor,
            tts,
            transport.output(),
            MetricsFrameLogger(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"[Call] Caller connected: {client}")
        record_event("call_connected")
        await claude_processor.speak_greeting()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("[Call] Caller disconnected")
        record_event("call_disconnected")
        await backend.aclose()
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


async def bot(runner_args: RunnerArguments) -> None:
    """Entry point the Pipecat dev runner calls (see `python -m pipecat.runner.run` in __main__)."""
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
