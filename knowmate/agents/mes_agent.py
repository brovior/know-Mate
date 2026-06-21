"""MES 에이전트 stub — "준비 중" 블록만 반환한다."""
from knowmate.agents.base import Block, TextBlock


class MesAgent:
    def handle(self, query: str, context: dict) -> list[Block]:
        """MES 에이전트는 아직 구현되지 않았다."""
        return [TextBlock(type="text", content="MES 에이전트는 현재 준비 중입니다.")]
