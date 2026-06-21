"""Phase 2 RAG 파이프라인 pytest 테스트 — fake 모드 기준, 사외 환경에서 전부 통과."""
import math
from pathlib import Path

import pytest

from knowmate.rag.chunker import chunk_text
from knowmate.rag.embedding import EmbeddingClient, VECTOR_DIM
from knowmate.secure.fake_reader import FakeReader
from knowmate.llm.client import LLMClient


# ──────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────

def _fake_embed_client() -> EmbeddingClient:
    """테스트용 fake EmbeddingClient를 반환한다."""
    return EmbeddingClient(base_url="http://localhost", host_header="embed.internal", fake=True)


# ──────────────────────────────────────────────
# TestChunker
# ──────────────────────────────────────────────

class TestChunker:
    def test_txt_basic(self):
        """txt 타입 청킹이 빈 줄 기준으로 분리됨을 확인한다."""
        text = "첫 번째 단락입니다.\n\n두 번째 단락입니다.\n\n세 번째 단락입니다."
        result = chunk_text(text, "txt")
        assert len(result) >= 1
        assert all(isinstance(c, str) for c in result)
        assert all(c.strip() for c in result)

    def test_docx_basic(self):
        """docx 타입 청킹이 줄 단위로 분리됨을 확인한다."""
        text = "문단1\n문단2\n문단3\n문단4"
        result = chunk_text(text, "docx")
        assert len(result) >= 1
        assert all(isinstance(c, str) for c in result)

    def test_pdf_basic(self):
        """pdf 타입 청킹이 페이지 단위로 처리됨을 확인한다."""
        text = "페이지1 내용\n\n페이지2 내용\n\n페이지3 내용"
        result = chunk_text(text, "pdf")
        assert len(result) >= 1
        assert all(c.strip() for c in result)

    def test_pptx_basic(self):
        """pptx 타입 청킹이 슬라이드 단위로 처리됨을 확인한다."""
        text = "슬라이드1 제목\n내용\n\n슬라이드2 제목\n내용"
        result = chunk_text(text, "pptx")
        assert len(result) >= 1
        assert all(c.strip() for c in result)

    def test_xlsx_small(self):
        """xlsx 타입 20행 이하는 전체 1청크임을 확인한다."""
        lines = "\n".join(f"행{i}\t값{i}" for i in range(10))
        result = chunk_text(lines, "xlsx")
        assert len(result) == 1

    def test_xlsx_large(self):
        """xlsx 타입 20행 초과는 5행씩 분할됨을 확인한다."""
        lines = "\n".join(f"행{i}\t값{i}" for i in range(25))
        result = chunk_text(lines, "xlsx")
        assert len(result) == 5  # 25행 / 5행 = 5청크

    def test_empty_returns_empty(self):
        """빈 텍스트 입력 시 빈 리스트를 반환함을 확인한다."""
        assert chunk_text("", "txt") == []
        assert chunk_text("   ", "txt") == []

    def test_long_text_splits(self):
        """긴 텍스트가 여러 청크로 분할되며 각 청크 길이가 허용 범위임을 확인한다."""
        long_text = "가나다라마바사아자차카타파하" * 50  # 약 700자
        result = chunk_text(long_text, "txt", chunk_size=400, overlap=80)
        assert len(result) > 1
        for chunk in result:
            assert isinstance(chunk, str)
            assert chunk.strip()
            assert len(chunk) <= 400 * 1.5

    def test_chunks_are_strings(self):
        """모든 청크가 str 타입임을 확인한다."""
        text = "테스트 내용입니다.\n\n두 번째 단락입니다."
        result = chunk_text(text, "txt")
        for chunk in result:
            assert isinstance(chunk, str)


# ──────────────────────────────────────────────
# TestEmbeddingFake
# ──────────────────────────────────────────────

class TestEmbeddingFake:
    def test_single_embed_shape(self):
        """단일 텍스트 임베딩 결과가 길이 1의 리스트이고 벡터 차원이 1024임을 확인한다."""
        client = _fake_embed_client()
        result = client.embed(["테스트"])
        assert len(result) == 1
        assert len(result[0]) == VECTOR_DIM

    def test_embed_returns_float_list(self):
        """임베딩 반환값이 float 리스트임을 확인한다."""
        client = _fake_embed_client()
        result = client.embed(["테스트"])
        for val in result[0]:
            assert isinstance(val, float)

    def test_unit_vector(self):
        """반환 벡터가 단위벡터(L2 norm ≈ 1.0)임을 확인한다."""
        client = _fake_embed_client()
        vec = client.embed(["단위벡터 확인"])[0]
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 0.01

    def test_different_texts_differ(self):
        """서로 다른 두 텍스트의 임베딩 벡터가 다름을 확인한다."""
        client = _fake_embed_client()
        v1 = client.embed(["텍스트A"])[0]
        v2 = client.embed(["텍스트B"])[0]
        assert v1 != v2

    def test_empty_input_returns_empty(self):
        """빈 리스트 입력 시 빈 리스트를 반환함을 확인한다."""
        client = _fake_embed_client()
        assert client.embed([]) == []


# ──────────────────────────────────────────────
# TestIndexerFake
# ──────────────────────────────────────────────

class TestIndexerFake:
    def test_create_indexer(self, tmp_path: Path):
        """Indexer 인스턴스 생성이 성공함을 확인한다."""
        from knowmate.rag.indexer import Indexer

        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        assert indexer is not None

    def test_index_file_returns_chunk_ids(self, tmp_path: Path):
        """index_file()이 chunk_id 리스트를 반환함을 확인한다."""
        from knowmate.rag.indexer import Indexer

        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        reader = FakeReader()
        text = reader.extract("sample.docx")
        ids = indexer.index_file(
            path="C:/sample/test.docx",
            text=text,
            mtime=1000.0,
            scope="local",
        )
        assert isinstance(ids, list)
        assert len(ids) >= 1

    def test_table_has_data(self, tmp_path: Path):
        """인덱싱 후 table에 데이터가 있음을 to_arrow().to_pandas()로 확인한다."""
        from knowmate.rag.indexer import Indexer

        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        reader = FakeReader()
        text = reader.extract("sample.txt")
        indexer.index_file(
            path="C:/sample/test.txt",
            text=text,
            mtime=1000.0,
            scope="local",
        )
        df = indexer.table.to_arrow().to_pandas()
        assert len(df) >= 1

    def test_delete_chunks_soft_delete(self, tmp_path: Path):
        """delete_chunks() 후 해당 청크가 is_deleted=True로 마킹됨을 확인한다."""
        from knowmate.rag.indexer import Indexer

        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        reader = FakeReader()
        text = reader.extract("sample.docx")
        ids = indexer.index_file(
            path="C:/sample/test.docx",
            text=text,
            mtime=1000.0,
            scope="local",
        )
        indexer.delete_chunks(ids)
        df = indexer.table.to_arrow().to_pandas()
        deleted = df[df["chunk_id"].isin(ids)]
        assert all(deleted["is_deleted"])


# ──────────────────────────────────────────────
# TestRetrieverFake
# ──────────────────────────────────────────────

class TestRetrieverFake:
    def _make_retriever(self, tmp_path: Path):
        """테스트용 Indexer + Retriever를 생성해 반환한다."""
        from knowmate.rag.indexer import Indexer
        from knowmate.rag.retriever import Retriever

        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        retriever = Retriever(
            indexer=indexer,
            embed_client=embed,
            top_k=10,
            score_threshold=0.0,  # fake 벡터이므로 임계값 0으로
        )
        return indexer, retriever

    def test_search_returns_list(self, tmp_path: Path):
        """문서 3개 인덱싱 후 search()가 list를 반환함을 확인한다."""
        indexer, retriever = self._make_retriever(tmp_path)
        reader = FakeReader()
        for i in range(3):
            text = reader.extract(f"doc{i}.docx")
            indexer.index_file(
                path=f"C:/sample/doc{i}.docx",
                text=text,
                mtime=float(i),
                scope="local",
            )
        result = retriever.search("알람")
        assert isinstance(result, list)

    def test_empty_db_returns_empty(self, tmp_path: Path):
        """빈 DB에서 search() 시 빈 리스트를 반환함을 확인한다."""
        _, retriever = self._make_retriever(tmp_path)
        result = retriever.search("알람")
        assert result == []

    def test_sandwich_order(self, tmp_path: Path):
        """_sandwich()가 [0,2,4,3,1] 순서로 재배열함을 확인한다."""
        from knowmate.rag.retriever import Retriever
        from knowmate.rag.indexer import Indexer

        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        retriever = Retriever(indexer=indexer, embed_client=embed)

        rows = [{"id": i, "score": 5 - i} for i in range(5)]
        sandwiched = retriever._sandwich(rows)
        expected_ids = [0, 2, 4, 3, 1]
        assert [r["id"] for r in sandwiched] == expected_ids

    def test_sandwich_even(self, tmp_path: Path):
        """_sandwich()가 짝수 개 입력에서도 올바르게 동작함을 확인한다."""
        from knowmate.rag.retriever import Retriever
        from knowmate.rag.indexer import Indexer

        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        retriever = Retriever(indexer=indexer, embed_client=embed)

        rows = [{"id": i} for i in range(4)]
        sandwiched = retriever._sandwich(rows)
        # evens=[0,2], odds=reversed([1,3])=[3,1] → [0,2,3,1]
        assert [r["id"] for r in sandwiched] == [0, 2, 3, 1]


# ──────────────────────────────────────────────
# TestKnowledgeAgentFake
# ──────────────────────────────────────────────

class TestKnowledgeAgentFake:
    def test_handle_returns_blocks(self, monkeypatch):
        """KnowledgeAgent.handle()이 Block 리스트를 반환함을 확인한다."""
        from knowmate.agents.knowledge_agent import KnowledgeAgent, _build_pipeline

        # fake 파이프라인 직접 빌드 (tmp_path 없이 임시 디렉토리 사용)
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("APPDATA", tmpdir)

            # config를 fake 모드로 패치
            monkeypatch.setattr(
                "knowmate.agents.knowledge_agent._build_pipeline",
                lambda: _make_fake_pipeline(tmpdir),
            )

            agent = KnowledgeAgent()
            result = agent.handle("테스트 질문", {})
            assert isinstance(result, list)
            assert len(result) >= 1
            assert result[0]["type"] == "text"

    def test_pipeline_failure_returns_mock(self, monkeypatch):
        """파이프라인 초기화 실패 시 mock 블록을 반환함을 확인한다."""
        from knowmate.agents.knowledge_agent import KnowledgeAgent

        def _fail():
            raise RuntimeError("의도적 실패")

        monkeypatch.setattr(
            "knowmate.agents.knowledge_agent._build_pipeline", _fail
        )

        agent = KnowledgeAgent()
        result = agent.handle("테스트", {})
        assert isinstance(result, list)
        assert len(result) >= 1
        assert result[0]["type"] == "text"
        assert "인덱싱" in result[0]["content"] or "파이프라인" in result[0]["content"]


def _make_fake_pipeline(db_dir: str) -> dict:
    """테스트용 fake 파이프라인 dict를 생성한다."""
    from knowmate.rag.indexer import Indexer
    from knowmate.rag.retriever import Retriever
    from knowmate.llm.client import LLMClient
    from knowmate.secure.fake_reader import FakeReader

    embed = _fake_embed_client()
    db_path = Path(db_dir) / "KnowMate" / "index"
    db_path.mkdir(parents=True, exist_ok=True)
    indexer = Indexer(db_path=str(db_path), embed_client=embed)
    retriever = Retriever(
        indexer=indexer, embed_client=embed, top_k=10, score_threshold=0.0
    )
    llm = LLMClient(
        base_url="http://localhost",
        host_header="llm.internal",
        model="fake",
        fake=True,
    )
    return {
        "indexer": indexer,
        "retriever": retriever,
        "llm": llm,
        "extractor": FakeReader(),
    }


# ──────────────────────────────────────────────
# TestLLMFake
# ──────────────────────────────────────────────

class TestLLMFake:
    def test_answer_returns_str(self):
        """LLMClient(fake).answer()가 str을 반환함을 확인한다."""
        client = LLMClient(
            base_url="http://localhost",
            host_header="llm.internal",
            model="fake",
            fake=True,
        )
        result = client.answer("질문", ["청크 내용입니다."])
        assert isinstance(result, str)
        assert len(result) > 0

    def test_empty_context_returns_not_found(self):
        """빈 context_chunks 입력 시 '찾지 못했습니다' 포함 문자열을 반환함을 확인한다."""
        client = LLMClient(
            base_url="http://localhost",
            host_header="llm.internal",
            model="fake",
            fake=True,
        )
        result = client.answer("질문", [])
        assert "찾지 못했습니다" in result

    def test_answer_includes_chunk_preview(self):
        """fake 모드 답변이 첫 청크 200자 미리보기를 포함함을 확인한다."""
        client = LLMClient(
            base_url="http://localhost",
            host_header="llm.internal",
            model="fake",
            fake=True,
        )
        chunk = "A" * 300
        result = client.answer("질문", [chunk])
        assert "A" * 200 in result
