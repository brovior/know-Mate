"""에이전트 등록·조회."""
from __future__ import annotations

from knowmate.agents.base import AgentBackend
from knowmate.agents.knowledge_agent import KnowledgeAgent
from knowmate.agents.mes_agent import MesAgent


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentBackend] = {
            "knowledge": KnowledgeAgent(),
            "mes":       MesAgent(),
        }

    def get(self, mode: str) -> AgentBackend:
        """mode에 해당하는 에이전트를 반환한다. 없으면 knowledge 반환."""
        return self._agents.get(mode, self._agents["knowledge"])
