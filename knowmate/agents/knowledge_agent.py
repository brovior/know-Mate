"""지식검색 에이전트 — Phase 2 RAG 파이프라인 연결 (실패 시 mock fallback)."""
import logging
import os
from pathlib import Path
from typing import Any

from knowmate.agents.base import Block, TextBlock, SourcesBlock, SourceItem

logger = logging.getLogger(__name__)


def _mock_blocks(query: str) -> list[Block]:
    """RAG 파이프라인 미초기화 시 반환할 안내 블록."""
    text: TextBlock = {
        "type": "text",
        "content": (
            f'"{query}"에 대한 검색을 시도했으나 RAG 파이프라인이 초기화되지 않았습니다.\n\n'
            "인덱싱 후 재시도해 주세요. 사이드바의 [인덱싱 시작] 버튼을 눌러 문서를 인덱싱하면 "
            "실제 문서를 기반으로 답변을 받을 수 있습니다."
        ),
    }
    return [text]


def _to_source_item(chunk: dict[str, Any]) -> SourceItem:
    """청크 dict를 SourceItem TypedDict로 변환한다."""
    file_path = chunk.get("file_path", "")
    file_type = chunk.get("file_type", "")
    badge = "메일" if file_type in {"msg", "eml"} else "문서"
    title = Path(file_path).name if file_path else "(알 수 없음)"
    subtitle = str(Path(file_path).parent) if file_path else ""
    score = float(chunk.get("score", 0.0))
    return SourceItem(badge=badge, title=title, subtitle=subtitle, score=score, path=file_path)


def _build_pipeline() -> dict[str, Any]:
    """RAG 파이프라인 컴포넌트를 생성해 dict로 반환한다."""
    from knowmate.config import get_config
    from knowmate.rag.embedding import get_embedding_client
    from knowmate.rag.indexer import Indexer
    from knowmate.rag.retriever import Retriever
    from knowmate.llm.client import get_llm_client
    from knowmate.secure import get_extractor
    from knowmate.secure.crypto import get_crypto_manager

    cfg = get_config()
    chunking = cfg.get("chunking", {})
    search = cfg.get("search", {})

    db_path = os.path.join(
        os.environ.get("APPDATA", "."), "KnowMate", "index"
    )
    os.makedirs(db_path, exist_ok=True)

    # crypto는 Indexer와 Retriever가 공유한다
    crypto = get_crypto_manager(cfg)

    embed_client = get_embedding_client(cfg)
    indexer = Indexer(
        db_path=db_path,
        embed_client=embed_client,
        chunk_size=chunking.get("chunk_size", 400),
        overlap=chunking.get("overlap", 80),
        batch_size=cfg.get("embedding", {}).get("batch_size", 32),
        crypto=crypto,
    )
    retriever = Retriever(
        indexer=indexer,
        embed_client=embed_client,
        top_k=search.get("top_k_max", 10),
        score_threshold=search.get("score_threshold", 0.4),
        crypto=crypto,
    )
    llm = get_llm_client(cfg)
    extractor = get_extractor(cfg.get("extractor", "fake"))

    return {
        "indexer": indexer,
        "retriever": retriever,
        "llm": llm,
        "extractor": extractor,
    }


class KnowledgeAgent:
    def __init__(self) -> None:
        """KnowledgeAgent를 초기화한다. 파이프라인은 첫 요청 시 지연 초기화한다."""
        self._pipeline: dict[str, Any] | None = None

    def _get_pipeline(self) -> dict[str, Any]:
        """파이프라인을 반환한다. 아직 초기화되지 않았으면 빌드한다."""
        if self._pipeline is None:
            self._pipeline = _build_pipeline()
        return self._pipeline

    def handle(self, query: str, context: dict) -> list[Block]:
        """RAG 검색 결과를 블록으로 반환한다. 파이프라인 초기화 실패 시 mock 반환."""
        try:
            pipeline = self._get_pipeline()
        except Exception as exc:
            logger.warning("RAG 파이프라인 초기화 실패: %s", exc)
            return _mock_blocks(query)

        scopes = context.get("scopes", ["local", "shared"])

        try:
            chunks = pipeline["retriever"].search(query, scopes=scopes)
        except Exception as exc:
            logger.warning("검색 실패: %s", exc)
            chunks = []

        answer_text = pipeline["llm"].answer(query, [c.get("text", "") for c in chunks])

        blocks: list[Block] = [TextBlock(type="text", content=answer_text)]

        if chunks:
            items = [_to_source_item(c) for c in chunks]
            sources: SourcesBlock = {
                "type": "sources",
                "title": f"관련 문서 {len(items)}건 · 근거 자료",
                "items": items,
            }
            blocks.append(sources)

        return blocks
