"""질문 스레드 전용 읽기 파이프라인을 사용하는 지식검색 에이전트."""
import logging
import time
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any

from knowmate.agents.base import Block, SourceItem, SourcesBlock, TextBlock
from knowmate.rag.embedding_cancel import (
    EmbeddingCancelledError,
    embedding_cancellation_scope,
)

logger = logging.getLogger(__name__)

# 메일로 취급하는 source_type (emails 테이블 청크 판별용)
MAIL_SOURCE_TYPES = {"knox", "outlook", "eml"}


def _mock_blocks(query: str) -> list[Block]:
    """RAG 파이프라인 미초기화 시 반환할 안내 블록."""
    body = (
        f'"{query}"에 대한 검색을 시도했으나 RAG 파이프라인이 초기화되지 않았습니다.\n\n'
        "인덱싱 후 재시도해 주세요. 사이드바의 [인덱싱 시작] 버튼을 눌러 문서를 인덱싱하면 "
        "실제 문서를 기반으로 답변을 받을 수 있습니다."
    )
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


def _build_pipeline(config: dict | None = None, data_dir: Path | None = None) -> dict[str, Any]:
    """기존 인덱스만 여는 요청 전용 검색 파이프라인을 생성한다."""
    from knowmate.config import get_config, get_data_dir
    from knowmate.llm.client import get_llm_client
    from knowmate.rag.embedding import get_embedding_client
    from knowmate.rag.query_reader import QueryIndexUnavailable, QueryReader
    from knowmate.rag.retriever import Retriever
    from knowmate.secure.crypto import get_crypto_manager

    cfg = deepcopy(config if config is not None else get_config())
    root = Path(data_dir) if data_dir is not None else get_data_dir()
    mode = cfg.get("embedding", {}).get(
        "mode", "fake" if cfg.get("extractor", "fake") == "fake" else "api",
    )
    if mode not in {"api", "fake"}:
        raise QueryIndexUnavailable("embedding_mode_not_supported")
    reader = QueryReader(root / "index")
    if reader.table is None and reader.email_indexer is None:
        reader.close()
        raise QueryIndexUnavailable("index_empty")
    embed_client = None
    try:
        crypto = get_crypto_manager(cfg, data_dir=root, create_if_missing=False)
        embed_client = get_embedding_client(cfg)
        search = cfg.get("search", {})
        retriever = Retriever(
            indexer=reader,
            embed_client=embed_client,
            top_k=search.get("top_k_max", 10),
            score_threshold=search.get("score_threshold", 0.4),
            crypto=crypto,
            rerank_enabled=search.get("rerank_enabled", False),
            email_indexer=reader.email_indexer,
            config=cfg,
        )
        return {"reader": reader, "embed_client": embed_client,
                "retriever": retriever, "llm": get_llm_client(cfg)}
    except BaseException:
        reader.close()
        if embed_client is not None:
            embed_client.close()
        raise


class KnowledgeAgent:
    def __init__(self, config: dict | None = None, data_dir: Path | None = None) -> None:
        """KnowledgeAgent를 초기화한다. 파이프라인은 첫 요청 시 지연 초기화한다."""
        self._pipeline: dict[str, Any] | None = None
        self._config = deepcopy(config) if config is not None else None
        self._data_dir = Path(data_dir) if data_dir is not None else None

    def _get_pipeline(self) -> dict[str, Any]:
        """파이프라인을 반환한다. 아직 초기화되지 않았으면 빌드한다."""
        if self._pipeline is None:
            self._pipeline = _build_pipeline(self._config, self._data_dir)
        return self._pipeline

    def close(self) -> None:
        """이 요청이 생성한 리소스를 소유 스레드에서 해제한다."""
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            for key in ("retriever", "embed_client", "reader"):
                resource = pipeline.pop(key, None)
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as exc:  # noqa: BLE001 - isolate search/resource failures without exposing payloads
                        logger.warning("질문 리소스 해제 실패: error_type=%s", type(exc).__name__)
            pipeline.clear()
        self._config = None
        self._data_dir = None

    def handle(self, query: str, context: dict) -> list[Block]:
        """선택 범위와 취소 상태를 확인하고 요청 전용 RAG를 실행한다."""
        cancellation = context.get("embedding_cancellation")
        with embedding_cancellation_scope(cancellation) if cancellation is not None else nullcontext():
            return self._handle(query, context)

    def _handle(self, query: str, context: dict) -> list[Block]:
        cancellation = context.get("embedding_cancellation")
        check = context.get("cancel_check") or (cancellation.check if cancellation is not None else lambda: None)
        check()
        scopes = context.get("scopes") or []
        if not scopes:
            return [{"type": "text", "content": "검색 범위를 하나 이상 선택해주세요. (내 PC 문서 또는 공유 폴더)"}]
        from knowmate.rag.query_reader import QueryIndexUnavailable
        try:
            pipeline = self._get_pipeline()
        except EmbeddingCancelledError:
            raise
        except QueryIndexUnavailable as exc:
            check()
            if exc.reason == "embedding_mode_not_supported":
                return [{"type": "text", "content": "질문 검색은 API 또는 fake 임베딩 모드를 지원합니다. 임베딩 설정을 확인해 주세요."}]
            return _mock_blocks(query)
        except Exception as exc:  # noqa: BLE001 - isolate search/resource failures without exposing payloads
            check()
            logger.warning("RAG 파이프라인 초기화 실패: error_type=%s", type(exc).__name__)
            return _mock_blocks(query)
        check()
        from knowmate.rag.retriever import Retriever
        if isinstance(pipeline["retriever"], Retriever):
            pipeline["retriever"].set_cancel_check(check)
        retrieve_t0 = time.perf_counter()
        try:
            chunks = pipeline["retriever"].search(query, scopes=scopes)
        except EmbeddingCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate search/resource failures without exposing payloads
            check()
            logger.warning("검색 실패: error_type=%s", type(exc).__name__)
            return [{"type": "text", "content": "문서 검색에 실패했습니다. 잠시 후 다시 시도해 주세요."}]
        check()
        retrieve_sec = time.perf_counter() - retrieve_t0

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

        llm_t0 = time.perf_counter()
        check()
        try:
            answer_text = pipeline["llm"].answer(query, [_chunk_context(c) for c in chunks])
        except EmbeddingCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate search/resource failures without exposing payloads
            check()
            logger.warning("답변 생성 실패: error_type=%s", type(exc).__name__)
            return [{"type": "text", "content": "답변 생성에 실패했습니다. 잠시 후 다시 시도해 주세요."}]
        check()
        llm_sec = time.perf_counter() - llm_t0

        # 질의 한 건당 한 줄. 검색(임베딩+벡터검색)과 LLM 생성 중 어디가 체감
        # 지연의 대부분인지 이 값 없이는 가를 수 없다. 질의문과 본문은 남기지
        # 않는다 — 길이와 건수만 기록한다(CLAUDE.md §2-1).
        logger.info(
            "[query] 총 %.2fs: retrieve=%.2fs llm=%.2fs (query_len=%d hits=%d)",
            retrieve_sec + llm_sec, retrieve_sec, llm_sec, len(query), len(chunks),
        )

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
