"""Shared observability FrameProcessors used by both Phase 2.1 and 2.2 bots."""

from loguru import logger
from pipecat.frames.frames import Frame, MetricsFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


def _format_metrics(metrics: list) -> str:
    lines = []
    for metric in metrics:
        lines.append(type(metric).__name__)
        for field, value in vars(metric).items():
            lines.append(f"\t{field}={value!r}")
    return "\n".join(lines)


class MetricsFrameLogger(FrameProcessor):
    """Logs Pipecat's own per-service metrics (STT/LLM/TTS TTFB, usage) as they arrive.

    Only covers native Pipecat services (Deepgram STT/TTS always; AnthropicLLMService too, in
    Phase 2.2). In Phase 2.1, ClaudeToolAgent isn't a native service, so its latency is timed by
    hand instead (agent/claude_agent.py, surfaced via AgentResult.llm_calls/tool_calls).
    """

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            logger.info(f"[Metrics] {frame.name}\n{_format_metrics(frame.data)}")
        await self.push_frame(frame, direction)  # ALWAYS push every frame, handled or not
