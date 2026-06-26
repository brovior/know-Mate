"""벡터 검색 및 권한 필터 Retriever (CLAUDE.md 6-10)."""
import getpass
import logging
from typing import Any

import pandas as pd

from knowmate.rag.embedding import EmbeddingClient
from knowmate.rag.indexer import Indexer

logger = logging.getLogger(__name__)


class Retriever:
    def __init__(
        self,
        indexer: Indexer,
        embed_client: EmbeddingClient,
        top_k: int = 10,
        score_threshold: float = 0.4,
        crypto=None,
        rerank_enabled: bool = False,
        email_indexer=None,
    ) -> None:
        """Retriever를 초기화한다.

        crypto: CryptoManager 또는 FakeCryptoManager 인스턴스.
                None이면 FakeCryptoManager를 사용한다.
        rerank_enabled: True이면 벡터 검색 후 cross-encoder rerank를 수행한다.
                        사내 rerank API가 준비된 시점에 config로 활성화한다.
        email_indexer: EmailIndexer 인스턴스. None이면 메일 검색 비활성.
        """
        self._table = indexer.table
        self._embed = embed_client
        self._top_k = top_k
        self._score_threshold = score_threshold
        self._current_user = getpass.getuser()
        self._rerank_enabled = rerank_enabled
        self._email_indexer = email_indexer

        if crypto is None:
            from knowmate.secure.crypto import FakeCryptoManager
            self._crypto = FakeCryptoManager()
        else:
            self._crypto = crypto

    def search(
        self, query: str, scopes: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """쿼리를 벡터 검색해 권한 필터·복호화·샌드위치 배열 후 청크 dict 리스트를 반환한다."""
        vec = self._embed.embed([query])[0]

        # chunks 테이블 검색
        chunks_rows = self._search_table(self._table, vec, scopes, use_owner_filter=True)

        # emails 테이블 병합 (local scope 포함 시 또는 scopes=None)
        if self._email_indexer and (scopes is None or "local" in scopes):
            try:
                email_rows = self._search_table(
                    self._email_indexer.table, vec, scopes=None, use_owner_filter=False
                )
                for row in email_rows:
                    # emails 테이블에는 file_path가 없으므로 source_file로 채움
                    if "file_path" not in row or not row.get("file_path"):
                        row["file_path"] = row.get("source_file", "")
                chunks_rows = self._merge(chunks_rows, email_rows)
            except Exception as exc:
                logger.warning("[emails] 메일 검색 실패, 문서 결과만 반환: %s", exc)

        if not chunks_rows:
            return []

        if self._rerank_enabled:
            chunks_rows = self._rerank(chunks_rows, query)

        logger.info("검색 결과: query_len=%d hits=%d", len(query), len(chunks_rows))
        return self._sandwich(chunks_rows)

    def _search_table(
        self,
        table,
        vec: list[float],
        scopes: list[str] | None,
        use_owner_filter: bool,
    ) -> list[dict[str, Any]]:
        """단일 테이블에서 벡터 검색 후 점수 필터·권한 필터·복호화를 수행한다."""
        raw = (
            table.search(vec)
            .where("is_deleted = false")
            .limit(self._top_k * 2)
            .to_arrow()
            .to_pandas()
        )

        if raw.empty:
            return []

        # 유사도 점수 계산
        if "_distance" in raw.columns:
            raw = raw.copy()
            raw["score"] = 1.0 - raw["_distance"] / 2.0
            if "file_path" in raw.columns:
                logger.debug(
                    "검색 후보 점수 분포: top5=%s threshold=%.2f",
                    raw.nlargest(5, "score")[["file_path", "score"]].to_dict(orient="records"),
                    self._score_threshold,
                )
            raw = raw[raw["score"] >= self._score_threshold]
        else:
            raw = raw.copy()
            raw["score"] = 1.0

        if raw.empty:
            return []

        # 권한 필터 (chunks 테이블 전용 — emails는 항상 local이고 owner 컬럼 없음)
        if use_owner_filter and scopes is not None and "scope" in raw.columns:
            raw = raw[raw["scope"].isin(scopes)]
            if "owner" in raw.columns:
                local_mask = raw["scope"] == "local"
                other_mask = ~local_mask
                local_filtered = raw[local_mask & (raw["owner"] == self._current_user)]
                raw = pd.concat([raw[other_mask], local_filtered])

        if raw.empty:
            return []

        raw = raw.sort_values("score", ascending=False).head(self._top_k)
        rows = raw.to_dict(orient="records")

        # text 컬럼 복호화
        for row in rows:
            try:
                row["text"] = self._crypto.decrypt(row["text"])
            except Exception as exc:
                logger.warning("청크 복호화 실패 (chunk_id=%s): %s", row.get("chunk_id"), exc)
                row["text"] = ""

        return rows

    def _merge(
        self, chunks: list[dict[str, Any]], emails: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """chunks와 emails를 score 내림차순으로 병합하고 top_k로 자른다."""
        merged = chunks + emails
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for row in sorted(merged, key=lambda r: float(r.get("score", 0.0)), reverse=True):
            cid = row.get("chunk_id", "")
            if cid and cid in seen:
                continue
            seen.add(cid)
            deduped.append(row)
        return deduped[: self._top_k]

    def _rerank(self, rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        """cross-encoder rerank placeholder. 사내 rerank API 연동 시 구현한다."""
        return rows

    def _sandwich(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Lost in the Middle 대응 샌드위치 배열: [0,2,4,...] + reversed([1,3,...])."""
        evens = rows[::2]
        odds = list(reversed(rows[1::2]))
        return evens + odds
