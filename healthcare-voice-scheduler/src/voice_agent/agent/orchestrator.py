"""Interface boundary between the voice pipeline and the dialogue agent.

Phase 1 implements this with a single Claude tool-calling loop
(ClaudeToolAgent). If Phase 2 evaluation shows a genuine need for
multi-step or multi-agent decomposition, a new implementation of this
same interface can replace ClaudeToolAgent without touching the voice
pipeline, tools, or scheduling backend.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    text: str


@dataclass
class AgentResult:
    reply_text: str
    tool_calls: list[dict] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)
    end_call: bool = False


class AgentOrchestrator(ABC):
    @abstractmethod
    async def run(self, history: list[Turn], user_text: str) -> AgentResult:
        """Process one caller turn and return the agent's reply."""
        raise NotImplementedError
