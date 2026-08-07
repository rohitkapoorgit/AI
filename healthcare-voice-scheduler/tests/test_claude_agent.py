from datetime import datetime
from types import SimpleNamespace

import pytest

from voice_agent.agent.claude_agent import ClaudeToolAgent
from voice_agent.scheduling.mock_backend import MockSchedulingBackend
from voice_agent.scheduling.service import SchedulingService

FIXED_NOW = datetime(2026, 8, 3, 8, 0)  # a Monday  # noqa: DTZ001 (naive, matches domain model)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_block(name: str, input_: dict, block_id: str = "tool_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=input_, id=block_id)


class FakeMessages:
    def __init__(self, responses: list[SimpleNamespace]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs) -> SimpleNamespace:
        # snapshot messages: it's the agent's live list, which keeps mutating after this call
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses: list[SimpleNamespace]):
        self.messages = FakeMessages(responses)


def _make_service() -> SchedulingService:
    return SchedulingService(MockSchedulingBackend(clock=lambda: FIXED_NOW))


@pytest.mark.asyncio
async def test_text_only_response_returns_immediately():
    client = FakeClient([SimpleNamespace(content=[_text_block("Hi there!")])])
    agent = ClaudeToolAgent(_make_service(), client=client)

    result = await agent.run([], "hello")

    assert result.reply_text == "Hi there!"
    assert result.end_call is False
    assert result.tool_calls == []
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_tool_call_dispatches_to_service_and_continues():
    responses = [
        SimpleNamespace(content=[_tool_block("search_doctors", {"specialty": "pediatrics"})]),
        SimpleNamespace(content=[_text_block("I found Dr. Webb.")]),
    ]
    client = FakeClient(responses)
    agent = ClaudeToolAgent(_make_service(), client=client)

    result = await agent.run([], "I need a pediatrician")

    assert result.reply_text == "I found Dr. Webb."
    assert result.tool_calls[0]["name"] == "search_doctors"
    assert result.tool_calls[0]["output"]["doctors"][0]["name"] == "Dr. Marcus Webb"
    assert len(client.messages.calls) == 2

    # observability: per-round LLM timing and per-call tool timing are both recorded
    assert [c["round"] for c in result.llm_calls] == [1, 2]
    assert all(c["latency_s"] >= 0 for c in result.llm_calls)
    assert result.tool_calls[0]["latency_s"] >= 0

    second_call_messages = client.messages.calls[1]["messages"]
    tool_result_message = second_call_messages[-1]
    assert tool_result_message["role"] == "user"
    assert tool_result_message["content"][0]["type"] == "tool_result"
    assert tool_result_message["content"][0]["tool_use_id"] == "tool_1"


@pytest.mark.asyncio
async def test_end_call_tool_ends_the_call_without_another_round_trip():
    responses = [
        SimpleNamespace(content=[_text_block("Goodbye!"), _tool_block("end_call", {})]),
    ]
    client = FakeClient(responses)
    agent = ClaudeToolAgent(_make_service(), client=client)

    result = await agent.run([], "bye")

    assert result.end_call is True
    assert result.reply_text == "Goodbye!"
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_first_call_with_empty_user_text_sends_kickoff_message():
    client = FakeClient([SimpleNamespace(content=[_text_block("Hello, thanks for calling!")])])
    agent = ClaudeToolAgent(_make_service(), client=client)

    await agent.run([], "")

    first_call_messages = client.messages.calls[0]["messages"]
    assert first_call_messages[0]["role"] == "user"
    assert "just connected" in first_call_messages[0]["content"]


@pytest.mark.asyncio
async def test_empty_final_response_gets_a_filler_instead_of_dead_air():
    responses = [SimpleNamespace(content=[])]
    client = FakeClient(responses)
    agent = ClaudeToolAgent(_make_service(), client=client)

    result = await agent.run([], "Jane Doe")

    assert result.reply_text == "One moment."
    assert result.end_call is False


@pytest.mark.asyncio
async def test_empty_response_ending_call_says_goodbye_not_nothing():
    responses = [SimpleNamespace(content=[_tool_block("end_call", {})])]
    client = FakeClient(responses)
    agent = ClaudeToolAgent(_make_service(), client=client)

    result = await agent.run([], "bye")

    assert result.reply_text == "Goodbye!"
    assert result.end_call is True


@pytest.mark.asyncio
async def test_exceeding_max_tool_rounds_returns_without_ending_call():
    responses = [
        SimpleNamespace(content=[_tool_block("search_doctors", {}, block_id=f"tool_{i}")])
        for i in range(6)
    ]
    client = FakeClient(responses)
    agent = ClaudeToolAgent(_make_service(), client=client)

    result = await agent.run([], "find me anyone")

    assert result.end_call is False
    assert len(client.messages.calls) == 6
    assert len(result.tool_calls) == 6
