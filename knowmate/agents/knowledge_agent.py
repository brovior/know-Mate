"""지식검색 에이전트 — Phase 2 RAG 파이프라인 연결 (실패 시 mock fallback)."""
import logging
import os
from pathlib import Path
from typing import Any

from knowmate.agents.base import Block, TextBlock, SourcesBlock, SourceItem

logger = logging.getLogger(__name__)

# 메일로 취급하는 source_type (emails 테이블 청크 판별용)
MAIL_SOURCE_TYPES = {"knox", "outlook", "eml"}


def _mock_blocks(query: str, error: str = "") -> list[Block]:
    """RAG 파이프라인 미초기화 시 반환할 안내 블록."""
    body = (
        f'"{query}"에 대한 검색을 시도했으나 RAG 파이프라인이 초기화되지 않았습니다.\n\n'
        "인덱싱 후 재시도해 주세요. 사이드바의 [인덱싱 시작] 버튼을 눌러 문서를 인덱싱하면 "
        "실제 문서를 기반으로 답변을 받을 수 있습니다."
    )
    if error:
        body += f"\n\n[디버그] 오류 내용:\n{error}"
    text: TextBlock = {"type": "text", "content": body}
    return [text]


def _to_source_item(chunk: dict[str, Any]) -> SourceItem:
    """청크 dict를 SourceItem TypedDict로 변환한다."""
    file_path = chunk.get("file_path", "")
    file_type = chunk.get("file_type", "")
    source_type = chunk.get("source_type", "")
    score = float(chunk.get("score", 0.0))

    # emails 테이블 청크: source_type이 메일 계열 또는 file_type="msg"/"eml"
    is_mail = source_type in MAIL_SOURCE_TYPES or file_type in {"msg", "eml"}

    if is_mail:
        badge = "메일"
        title = chunk.get("subject") or "(제목 없음)"
        sender = chunk.get("sender", "")
        mail_date = chunk.get("mail_date", "")
        subtitle = f"{sender} · {mail_date}".strip(" ·")
        # 출처 카드 클릭용 경로: source_file (원본 .mysingle)
        path = chunk.get("source_file") or file_path
    else:
        badge = "문서"
        title = Path(file_path).name if file_path else "(알 수 없음)"
        subtitle = str(Path(file_path).parent) if file_path else ""
        path = file_path

    return SourceItem(badge=badge, title=title, subtitle=subtitle, score=score, path=path)


def _dedupe_sources(chunks: list[dict[str, Any]]) -> list[SourceItem]:
    """청크 리스트를 파일/메일 단위로 중복 제거해 SourceItem 리스트를 반환한다.

    같은 문서·메일의 여러 청크는 가장 높은 score를 가진 청크 하나로 표시한다.
    출력은 score 내림차순 정렬.
    """
    best_by_key: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        # 메일은 source_file, 문서는 file_path 기준으로 dedup
        source_type = chunk.get("source_type", "")
        if source_type in MAIL_SOURCE_TYPES:
            key = chunk.get("source_file") or chunk.get("file_path", "")
        else:
            key = chunk.get("file_path", "")
        prev = best_by_key.get(key)
        if prev is None or float(chunk.get("score", 0.0)) > float(prev.get("score", 0.0)):
            best_by_key[key] = chunk
    items = [_to_source_item(c) for c in best_by_key.values()]
    items.sort(key=lambda it: it["score"], reverse=True)
    return items


def _build_pipeline() -> dict[str, Any]:
    """RAG 파이프라인 컴포넌트를 생성해 dict로 반환한다."""
    from knowmate.config import get_config, get_data_dir
    from knowmate.rag.embedding import get_embedding_client
    from knowmate.rag.indexer import Indexer
    from knowmate.rag.retriever import Retriever
    from knowmate.llm.client import get_llm_client
    from knowmate.secure import get_extractor
    from knowmate.secure.crypto import get_crypto_manager

    cfg = get_config()
    chunking = cfg.get("chunking", {})
    search = cfg.get("search", {})

    db_path = str(get_data_dir() / "index")
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

    # 메일 인덱서 (mail.enabled 무관하게 항상 초기화 — 기존 emails 테이블 검색 지원)
    from knowmate.rag.email_indexer import EmailIndexer
    email_indexer = EmailIndexer(
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
        rerank_enabled=search.get("rerank_enabled", False),
        email_indexer=email_indexer,
    )
    llm = get_llm_client(cfg)
    extractor = get_extractor(cfg.get("extractor", "fake"))

    return {
        "indexer": indexer,
        "email_indexer": email_indexer,
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
            import traceback
            detail = traceback.format_exc()
            logger.warning("RAG 파이프라인 초기화 실패: %s\n%s", exc, detail)
            return _mock_blocks(query, error=detail)

        scopes = context.get("scopes") or []
        if not scopes:
            return [{"type": "text", "content": "검색 범위를 하나 이상 선택해주세요. (내 PC 문서 또는 공유 폴더)"}]

        try:
            chunks = pipeline["retriever"].search(query, scopes=scopes)
        except Exception as exc:
            logger.warning("검색 실패: %s", exc)
            chunks = []

        def _chunk_context(c: dict) -> str:
            text = c.get("text", "")
            if c.get("source_type") in MAIL_SOURCE_TYPES:
                header = (
                    f"[메일] 제목: {c.get('subject', '')} | "
                    f"발신: {c.get('sender', '')} | "
                    f"수신: {c.get('recipients', '')} | "
                    f"날짜: {c.get('mail_date', '')}\n"
                )
                return header + text
            fp = c.get("file_path", "")
            if fp:
                p = Path(fp)
                return f"[문서] 파일명: {p.name} | 폴더: {p.parent.name}\n" + text
            return text

        answer_text = pipeline["llm"].answer(query, [_chunk_context(c) for c in chunks])

        blocks: list[Block] = [TextBlock(type="text", content=answer_text)]

        if chunks:
            items = _dedupe_sources(chunks)  # 파일 단위 중복 제거 (문서당 1줄)
            sources: SourcesBlock = {
                "type": "sources",
                "title": f"관련 문서 {len(items)}건 · 근거 자료",
                "items": items,
            }
            blocks.append(sources)

        return blocks
