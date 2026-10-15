"""에이전트 등록·조회."""
from __future__ import annotations

from pathlib import Path

from knowmate.agents.base import AgentBackend
from knowmate.agents.knowledge_agent import KnowledgeAgent
from knowmate.agents.mes_agent import MesAgent


class AgentRegistry:
    def __init__(self, config: dict | None = None, data_dir: Path | None = None) -> None:
        """요청별 설정과 데이터 경로로 에이전트를 만든다."""
        self._agents: dict[str, AgentBackend] = {
            "knowledge": KnowledgeAgent(config=config, data_dir=data_dir),
            "mes":       MesAgent(),
        }

    def get(self, mode: str) -> AgentBackend:
        """mode에 해당하는 에이전트를 반환한다. 없으면 knowledge 반환."""
        return self._agents.get(mode, self._agents["knowledge"])

    def close(self) -> None:
        """요청 스레드에서 에이전트의 소유 리소스를 해제한다."""
        agents, self._agents = self._agents, {}
        agent = agents.get("knowledge")
        if agent is not None:
            agent.close()
