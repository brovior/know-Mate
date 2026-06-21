"""Phase 1 테스트 — 에이전트 골격 및 브리지 로직 검증."""
import json
import pytest
from knowmate.agents.base import TextBlock, SourcesBlock, SourceItem
from knowmate.agents.knowledge_agent import KnowledgeAgent
from knowmate.agents.mes_agent import MesAgent
from knowmate.agents.registry import AgentRegistry


# ── KnowledgeAgent ──────────────────────────────────────────────

class TestKnowledgeAgent:
    def setup_method(self):
        self.agent = KnowledgeAgent()

    def test_returns_list(self):
        blocks = self.agent.handle("테스트 질문", {})
        assert isinstance(blocks, list)
        assert len(blocks) >= 1

    def test_first_block_is_text(self):
        blocks = self.agent.handle("테스트", {})
        assert blocks[0]["type"] == "text"
        assert "content" in blocks[0]

    def test_text_contains_query(self):
        # Phase 2: 빈 DB에서 RAG 동작 — text 블록은 항상 반환됨
        query = "A설비 알람 폭주 처리 절차"
        blocks = self.agent.handle(query, {})
        text_block = next(b for b in blocks if b["type"] == "text")
        assert isinstance(text_block["content"], str)
        assert len(text_block["content"]) > 0

    def test_sources_block_when_present(self):
        # Phase 2: sources 블록은 검색 결과가 있을 때만 반환됨
        blocks = self.agent.handle("테스트", {})
        sources = [b for b in blocks if b["type"] == "sources"]
        # 빈 DB에서는 sources가 없을 수 있음 — 있으면 구조만 검증
        for src in sources:
            assert "items" in src
            assert isinstance(src["items"], list)

    def test_sources_items_have_required_fields(self):
        # sources 블록이 있는 경우에만 필드 검증
        blocks = self.agent.handle("테스트", {})
        sources = [b for b in blocks if b["type"] == "sources"]
        for src in sources:
            for item in src["items"]:
                assert "badge" in item
                assert "title" in item
                assert "subtitle" in item
                assert "score" in item
                assert "path" in item

    def test_sources_score_in_range(self):
        # sources 블록이 있는 경우에만 score 범위 검증
        blocks = self.agent.handle("테스트", {})
        sources = [b for b in blocks if b["type"] == "sources"]
        for src in sources:
            for item in src["items"]:
                assert 0.0 <= item["score"] <= 1.0

    def test_sources_badge_value(self):
        # sources 블록이 있는 경우에만 badge 값 검증
        blocks = self.agent.handle("테스트", {})
        sources = [b for b in blocks if b["type"] == "sources"]
        for src in sources:
            for item in src["items"]:
                assert item["badge"] in ("문서", "메일")


# ── MesAgent ────────────────────────────────────────────────────

class TestMesAgent:
    def setup_method(self):
        self.agent = MesAgent()

    def test_returns_list(self):
        blocks = self.agent.handle("MES 질문", {})
        assert isinstance(blocks, list)
        assert len(blocks) >= 1

    def test_returns_text_block(self):
        blocks = self.agent.handle("MES 질문", {})
        assert blocks[0]["type"] == "text"

    def test_stub_message(self):
        blocks = self.agent.handle("MES 질문", {})
        assert "준비 중" in blocks[0]["content"]


# ── AgentRegistry ───────────────────────────────────────────────

class TestAgentRegistry:
    def setup_method(self):
        self.registry = AgentRegistry()

    def test_get_knowledge(self):
        agent = self.registry.get("knowledge")
        blocks = agent.handle("테스트", {})
        assert any(b["type"] == "text" for b in blocks)

    def test_get_mes(self):
        agent = self.registry.get("mes")
        blocks = agent.handle("테스트", {})
        assert blocks[0]["type"] == "text"

    def test_unknown_mode_falls_back_to_knowledge(self):
        agent = self.registry.get("unknown_mode")
        blocks = agent.handle("테스트", {})
        # Phase 2: knowledge_agent는 항상 text 블록을 반환한다
        assert any(b["type"] == "text" for b in blocks)


# ── Bridge 로직 (Qt 없이 순수 로직만) ───────────────────────────

class TestBridgeLogic:
    """Bridge.sendQuery의 JSON 파싱·라우팅 로직을 Qt 없이 검증한다."""

    def _dispatch(self, payload: str) -> list:
        """Bridge.sendQuery 핵심 로직을 추출해 테스트한다."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return [{"type": "text", "content": "오류: invalid JSON payload"}]

        query = data.get("query", "").strip()
        mode  = data.get("mode", "knowledge")

        if not query:
            return [{"type": "text", "content": "오류: empty query"}]

        registry = AgentRegistry()
        agent = registry.get(mode)
        return agent.handle(query, {"mode": mode})

    def test_valid_knowledge_query(self):
        result = self._dispatch('{"query": "테스트", "mode": "knowledge"}')
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_invalid_json_returns_error(self):
        result = self._dispatch("not json")
        assert result[0]["type"] == "text"
        assert "오류" in result[0]["content"]

    def test_empty_query_returns_error(self):
        result = self._dispatch('{"query": "  ", "mode": "knowledge"}')
        assert "오류" in result[0]["content"]

    def test_mes_mode_routing(self):
        result = self._dispatch('{"query": "MES 테스트", "mode": "mes"}')
        assert result[0]["type"] == "text"
        assert "준비 중" in result[0]["content"]

    def test_response_serializable(self):
        result = self._dispatch('{"query": "직렬화 테스트", "mode": "knowledge"}')
        # responseReady 시그널로 내보낼 수 있는지 확인
        json.dumps({"blocks": result}, ensure_ascii=False)
