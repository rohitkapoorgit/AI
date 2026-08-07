"""Phase 2.2 — real-time voice pipeline using Pipecat's native AnthropicLLMService.

Second, more capable step after Phase 2.1 (voice_bot_claude_agent.py): same audio plumbing
(Deepgram STT/TTS, local WebRTC), but Claude is called through Pipecat's own LLM node and
function-calling instead of ClaudeToolAgent, which gets per-token streaming to TTS and native
mid-generation barge-in handling that Phase 2.1's request/response wrapper doesn't have. Reuses
the exact same SYSTEM_PROMPT and TOOL_SCHEMAS from agent/claude_agent.py and scheduling/service.py
(registered as Pipecat FunctionSchemas), dispatching to the same SchedulingService — so the two
phases can't drift into different prompts or tool definitions even though the LLM-calling
mechanics differ. See "The architectural shift" in docs/PHASE2_VOICE.md for the full trade-off.

Observability: same trace/dashboard as Phase 2.1 (pipeline/trace.py, scripts/trace_dashboard.py),
same event names (stt_final, llm_call, tool_call, turn_summary), but measured differently because
the architecture genuinely differs. ClaudeToolAgent's hand-rolled loop let Phase 2.1 time each raw
Claude API call separately; AnthropicLLMService manages its own internal tool-calling loop, so we
can't see individual API calls from outside it — only the one LLMFullResponseStartFrame ->
LLMFullResponseEndFrame span that wraps the whole turn, tool calls included. So here, "llm_call"
is one span per turn (not one per API round-trip), and its "latency_s" is that span's duration
*minus* however much of it was spent inside tool handlers — an approximation of "LLM thinking +
streaming time," not a raw API call duration. Tool call timing itself is exact (measured directly
in each FunctionSchema handler, same as Phase 2.1).
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndTaskFrame,
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams

from voice_agent.agent.claude_agent import DEFAULT_MODEL, SYSTEM_PROMPT
from voice_agent.pipeline.observability import MetricsFrameLogger
from voice_agent.pipeline.trace import record_event, reset_trace
from voice_agent.scheduling.factory import build_backend
from voice_agent.scheduling.service import TOOL_SCHEMAS, SchedulingService

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_REPO_ROOT / ".env", override=True)

_END_CALL_DESCRIPTION = (
    "Call this once the conversation is complete and it's time to hang up — e.g. after the "
    "appointment is booked and the caller has confirmed or declined confirmation, or if the "
    "caller wants to stop."
)
_KICKOFF_MESSAGE = "(The caller has just connected. Greet them and ask how you can help.)"


class SttObserver(FrameProcessor):
    """Passthrough logger for STT frames.

    Unlike Phase 2.1's ClaudeAgentProcessor (which owns turn-taking and deliberately swallows
    transcription frames), this observer must forward everything unchanged — user_aggregator
    downstream still needs these frames to do its job.
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterimTranscriptionFrame):
            logger.debug(f"[STT interim] {frame.text!r}")
        elif isinstance(frame, TranscriptionFrame) and frame.text.strip():
            logger.info(f"[STT final] Caller said: {frame.text!r}")
            record_event("stt_final", text=frame.text)
        await self.push_frame(frame, direction)


class LlmSpanObserver(FrameProcessor):
    """Times each LLMFullResponseStartFrame -> LLMFullResponseEndFrame span and accumulates the
    TextFrames in between to reconstruct the reply text (native streaming doesn't hand back one
    atomic string the way ClaudeToolAgent.run() does). Sits between `llm` and `tts` in the
    pipeline so it sees both the response frames and the TextFrames flowing past.

    Tool calls fire *during* a span (function handlers run mid-generation), not after it, so
    _scheduling_tool/_end_call_tool report into this observer via record_tool_call()/
    mark_end_call() rather than this class discovering them itself.
    """

    def __init__(self):
        super().__init__()
        self._span_started_at: float | None = None
        self._reply_chunks: list[str] = []
        self._tool_calls_this_turn: list[dict] = []
        self._end_call_requested = False

    def record_tool_call(self, entry: dict) -> None:
        self._tool_calls_this_turn.append(entry)

    def mark_end_call(self) -> None:
        self._end_call_requested = True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._span_started_at = time.monotonic()
            self._reply_chunks = []
            self._tool_calls_this_turn = []
            self._end_call_requested = False
        elif isinstance(frame, TextFrame) and self._span_started_at is not None:
            self._reply_chunks.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame) and self._span_started_at is not None:
            self._finish_span()

        await self.push_frame(frame, direction)

    def _finish_span(self) -> None:
        elapsed = time.monotonic() - self._span_started_at
        reply_text = "".join(self._reply_chunks)
        tool_total = sum(c["latency_s"] for c in self._tool_calls_this_turn)
        llm_only = max(elapsed - tool_total, 0.0)

        logger.info(f"[LLM] span: {elapsed:.2f}s (of which {tool_total:.2f}s was tool time)")
        record_event("llm_call", round=1, latency_s=elapsed)

        for call in self._tool_calls_this_turn:
            logger.info(
                f"[Tool] {call['name']}({call['input']}) -> {call['output']} "
                f"[{call['latency_s']:.2f}s]"
            )
            record_event("tool_call", **call)

        logger.info(
            f"[Turn] total {elapsed:.2f}s = {llm_only:.2f}s LLM + {tool_total:.2f}s tools "
            f"({len(self._tool_calls_this_turn)} call(s)). end_call={self._end_call_requested}. "
            f"Reply: {reply_text!r}"
        )
        record_event(
            "turn_summary",
            total_s=elapsed,
            llm_s=llm_only,
            tool_s=tool_total,
            overhead_s=0.0,
            llm_call_count=1,
            tool_call_count=len(self._tool_calls_this_turn),
            end_call=self._end_call_requested,
            reply=reply_text,
        )
        self._span_started_at = None


def _build_tools(service: SchedulingService, observer: LlmSpanObserver) -> list[FunctionSchema]:
    tools = [_scheduling_tool(schema, service, observer) for schema in TOOL_SCHEMAS]
    tools.append(_end_call_tool(observer))
    return tools


def _scheduling_tool(
    schema: dict, service: SchedulingService, observer: LlmSpanObserver
) -> FunctionSchema:
    method = getattr(service, schema["name"])

    async def handler(params: FunctionCallParams) -> None:
        started_at = time.monotonic()
        output = await method(**params.arguments)
        observer.record_tool_call(
            {
                "name": schema["name"],
                "input": params.arguments,
                "output": output,
                "latency_s": time.monotonic() - started_at,
            }
        )
        await params.result_callback(output)

    return FunctionSchema(
        name=schema["name"],
        description=schema["description"],
        properties=schema["input_schema"].get("properties", {}),
        required=schema["input_schema"].get("required", []),
        handler=handler,
    )


def _end_call_tool(observer: LlmSpanObserver) -> FunctionSchema:
    async def handler(params: FunctionCallParams) -> None:
        observer.mark_end_call()
        await params.llm.push_frame(TTSSpeakFrame("Goodbye!"))
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await params.result_callback(None)

    return FunctionSchema(
        name="end_call",
        description=_END_CALL_DESCRIPTION,
        properties={},
        required=[],
        handler=handler,
    )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    logger.info("Starting voice bot (Phase 2.2 -- native AnthropicLLMService)")
    reset_trace()

    backend = build_backend()
    service = SchedulingService(backend)
    llm_span_observer = LlmSpanObserver()

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = DeepgramTTSService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    llm = AnthropicLLMService(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        model=DEFAULT_MODEL,
        settings=AnthropicLLMService.Settings(system_instruction=SYSTEM_PROMPT),
    )

    context = LLMContext(tools=_build_tools(service, llm_span_observer))
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            SttObserver(),
            user_aggregator,
            llm,
            llm_span_observer,
            tts,
            transport.output(),
            assistant_aggregator,
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
        context.add_message({"role": "user", "content": _KICKOFF_MESSAGE})
        await task.queue_frames([LLMRunFrame()])

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
        "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
