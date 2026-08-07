"""Text-based mock VoiceChannel for local dev: stdin in, stdout out."""

import asyncio

from voice_agent.pipeline.channel import VoiceChannel

_HANGUP_WORDS = {"bye", "goodbye", "hang up", "hangup"}


class ConsoleVoiceChannel(VoiceChannel):
    async def listen(self) -> str | None:
        try:
            text = await asyncio.to_thread(input, "You: ")
        except EOFError:
            return None
        if text.strip().lower() in _HANGUP_WORDS:
            return None
        return text

    async def speak(self, text: str) -> None:
        print(f"Agent: {text}")
