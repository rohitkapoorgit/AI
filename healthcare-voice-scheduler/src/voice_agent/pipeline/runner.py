"""Ties an AgentOrchestrator to a VoiceChannel for one call.

This is the Phase 1 stand-in for the Pipecat pipeline: same shape (get
caller input -> run agent -> speak reply -> repeat until hangup/end_call),
minus audio framing, VAD, and streaming. Swapping ConsoleVoiceChannel for the
real Pipecat/Twilio pipeline later doesn't change this loop.
"""

from voice_agent.agent.orchestrator import AgentOrchestrator, Turn
from voice_agent.pipeline.channel import VoiceChannel


async def run_call(agent: AgentOrchestrator, channel: VoiceChannel) -> list[Turn]:
    history: list[Turn] = []

    greeting = await agent.run(history, "")
    await channel.speak(greeting.reply_text)
    history.append(Turn(role="assistant", text=greeting.reply_text))
    if greeting.end_call:
        return history

    while True:
        user_text = await channel.listen()
        if user_text is None:
            break
        history.append(Turn(role="user", text=user_text))

        result = await agent.run(history, user_text)
        await channel.speak(result.reply_text)
        history.append(Turn(role="assistant", text=result.reply_text))

        if result.end_call:
            break

    return history
