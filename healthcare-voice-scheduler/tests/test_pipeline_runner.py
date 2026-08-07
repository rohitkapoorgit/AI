import pytest

from voice_agent.agent.orchestrator import AgentOrchestrator, AgentResult, Turn
from voice_agent.pipeline.channel import VoiceChannel
from voice_agent.pipeline.runner import run_call


class ScriptedChannel(VoiceChannel):
    """Feeds pre-scripted caller lines and records what the agent said."""

    def __init__(self, lines: list[str]):
        self._lines = list(lines)
        self.spoken: list[str] = []

    async def listen(self) -> str | None:
        if not self._lines:
            return None
        return self._lines.pop(0)

    async def speak(self, text: str) -> None:
        self.spoken.append(text)


class ScriptedAgent(AgentOrchestrator):
    """Replays a fixed script of replies, so run_call's loop mechanics (greet,
    speak, hang up on end_call or on hangup) can be tested without a real LLM.
    Business logic (tool dispatch) is covered by test_scheduling_service.py
    and test_claude_agent.py instead.
    """

    def __init__(self, replies: list[tuple[str, bool]]):
        self._replies = list(replies)

    async def run(self, history: list[Turn], user_text: str) -> AgentResult:
        reply_text, end_call = self._replies.pop(0)
        return AgentResult(reply_text=reply_text, end_call=end_call)


@pytest.mark.asyncio
async def test_runner_speaks_greeting_then_replies_until_end_call():
    agent = ScriptedAgent(
        [
            ("Thanks for calling, how can I help?", False),
            ("Booked! Should I confirm?", False),
            ("You're all set. Goodbye!", True),
        ]
    )
    channel = ScriptedChannel(["I need a doctor", "yes"])

    history = await run_call(agent, channel)

    assert channel.spoken == [
        "Thanks for calling, how can I help?",
        "Booked! Should I confirm?",
        "You're all set. Goodbye!",
    ]
    assert history[-1] == Turn(role="assistant", text="You're all set. Goodbye!")


@pytest.mark.asyncio
async def test_runner_ends_immediately_if_greeting_ends_call():
    agent = ScriptedAgent([("Sorry, we're closed. Goodbye!", True)])
    channel = ScriptedChannel(["hello"])  # never consumed

    history = await run_call(agent, channel)

    assert channel.spoken == ["Sorry, we're closed. Goodbye!"]
    assert history == [Turn(role="assistant", text="Sorry, we're closed. Goodbye!")]


@pytest.mark.asyncio
async def test_hangup_before_agent_ends_call_stops_loop():
    agent = ScriptedAgent([("Thanks for calling, how can I help?", False)])
    channel = ScriptedChannel([])  # listen() returns None immediately (hangup)

    history = await run_call(agent, channel)

    assert channel.spoken == ["Thanks for calling, how can I help?"]
    assert history[-1] == Turn(role="assistant", text="Thanks for calling, how can I help?")
