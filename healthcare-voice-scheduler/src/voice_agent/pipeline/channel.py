"""Interface boundary between the call runner and the audio world.

Phase 1 implements this with ConsoleVoiceChannel (stdin/stdout text),
standing in for telephony + STT + TTS together so the agent and scheduling
tools can be exercised end-to-end with zero external accounts. The real
implementation is the Pipecat pipeline (Twilio Media Streams -> Deepgram STT
-> ... -> Cartesia TTS) added in a later build step, behind this same
interface.
"""

from abc import ABC, abstractmethod


class VoiceChannel(ABC):
    @abstractmethod
    async def listen(self) -> str | None:
        """Return the caller's next utterance as text, or None on hangup."""
        raise NotImplementedError

    @abstractmethod
    async def speak(self, text: str) -> None:
        """Deliver the agent's reply to the caller."""
        raise NotImplementedError
