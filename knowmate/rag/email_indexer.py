"""emails 테이블 스키마 및 EmailIndexer (Knox .mysingle 전용)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa

from knowmate.rag.chunker import chunk_text
from knowmate.rag.embedding import EmbeddingClient, VECTOR_DIM

logger = logging.getLogger(__name__)

# 인덱싱 포맷 버전 — 변경 시 기존 메일 자동 재인덱싱
EMAIL_INDEX_VERSION = "2"

EMAIL_SCHEMA = pa.schema([
    # ── 청크 공통 ──
    pa.field("chunk_id",        pa.string()),
    pa.field("scope",           pa.string()),     # 메일은 항상 'local'
    pa.field("indexed_at",      pa.string()),
    pa.field("chunk_index",     pa.int32()),
    pa.field("chunk_total",     pa.int32()),
    pa.field("text",            pa.string()),     # AES-256-GCM 암호화
    pa.field("vector",          pa.list_(pa.float32(), VECTOR_DIM)),
    pa.field("is_deleted",      pa.bool_()),
    pa.field("deleted_at",      pa.string()),
    pa.field("miss_count",      pa.int32()),
    pa.field("mtime",           pa.float64()),    # .mysingle 파일 mtime
    # ── 메일 공통 (Knox/Outlook 동일) ──
    pa.field("mail_uid",        pa.string()),     # 'knox:...' | 'outlook:...'
    pa.field("source_type",     pa.string()),     # 'knox' | 'outlook'
    pa.field("message_id",      pa.string()),
    pa.field("subject",         pa.string()),
    pa.field("sender",          pa.string()),
    pa.field("recipients",      pa.string()),
    pa.field("mail_date",       pa.string()),
    pa.field("thread_ref",      pa.string()),
    pa.field("source_file",     pa.string()),     # .mysingle 경로
    # ── 청크 출처 구분 ──
    pa.field("chunk_origin",    pa.string()),     # 'body' | 'attachment'
    pa.field("attach_filename", pa.string()),
    pa.field("attach_sha256",   pa.string()),
    # ── 소스 고유 봉투 ──
    pa.field("source_meta",     pa.string()),     # JSON 문자열
])

EMAIL_TABLE_NAME = "emails"


def _inject_version(source_meta: str) -> str:
    """source_meta JSON 문자열에 _index_version 필드를 삽입한다."""
    import json
    try:
        meta = json.loads(source_meta or "{}")
    except Exception:
        meta = {}
    meta["_index_version"] = EMAIL_INDEX_VERSION
    return json.dumps(meta, ensure_ascii=False)


def get_or_create_emails_table(db):
    """emails 테이블이 없으면 생성, 있으면 open해 반환한다."""
    if EMAIL_TABLE_NAME in db.table_names():
        return db.open_table(EMAIL_TABLE_NAME)
    return db.create_table(EMAIL_TABLE_NAME, schema=EMAIL_SCHEMA)


class EmailIndexer:
    def __init__(
        self,
        db_path: str | Path,
        embed_client: EmbeddingClient,
        chunk_size: int = 400,
        overlap: int = 80,
        batch_size: int = 32,
        crypto=None,
    ) -> None:
        """emails 테이블에 연결하고 EmailIndexer를 초기화한다."""
        import lancedb

        self._embed = embed_client
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._batch_size = batch_size

        if crypto is None:
            from knowmate.secure.crypto import FakeCryptoManager
            self._crypto = FakeCryptoManager()
        else:
            self._crypto = crypto

        db = lancedb.connect(str(db_path))
        self.table = get_or_create_emails_table(db)

    def is_indexed(self, mail_uid: str, mtime: float) -> bool:
        """동일 mail_uid + mtime + 인덱스 버전이 모두 일치하면 True를 반환한다."""
        import json
        try:
            df = (
                self.table.search()
                .where(f"mail_uid = '{mail_uid}' AND is_deleted = false")
                .limit(1)
                .to_arrow()
                .to_pandas()
            )
            if df.empty:
                return False
            row = df.iloc[0]
            if abs(float(row["mtime"]) - mtime) >= 1.0:
                return False
            # source_meta에 저장된 인덱스 버전 확인
            try:
                meta = json.loads(row.get("source_meta", "{}") or "{}")
                if meta.get("_index_version") != EMAIL_INDEX_VERSION:
                    return False
            except Exception:
                return False
            return True
        except Exception as exc:
            logger.warning("[email_indexer] is_indexed 조회 실패 (uid=%s): %s", mail_uid[:20], exc)
            return False

    def index_mail(
        self,
        parsed: dict,
        mtime: float,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """
        파싱된 메일 dict를 청킹·임베딩·암호화해 emails 테이블에 저장한다.
        chunk_id 리스트를 반환한다.
        """
        # 메타데이터 헤더를 본문 앞에 붙여 발신인·날짜·수신인 기반 검색 지원
        meta_header = (
            f"제목: {parsed.get('subject', '')}\n"
            f"발신: {parsed.get('sender', '')}\n"
            f"수신: {parsed.get('recipients', '')}\n"
            f"날짜: {parsed.get('mail_date', '')}\n\n"
        )
        body_text: str = meta_header + parsed["body_text"]
        chunks = chunk_text(body_text, "txt", self._chunk_size, self._overlap)
        if not chunks:
            return []

        # 기존 청크 삭제 (변경된 메일 재인덱싱)
        self.delete_mail_chunks(parsed["mail_uid"])

        indexed_at = datetime.now(timezone.utc).isoformat()
        total = len(chunks)
        chunk_ids: list[str] = []

        for batch_start in range(0, total, self._batch_size):
            batch = chunks[batch_start: batch_start + self._batch_size]
            vectors = self._embed.embed(batch)

            rows: list[dict[str, Any]] = []
            for i, (chunk_text_val, vector) in enumerate(zip(batch, vectors)):
                global_idx = batch_start + i
                cid = str(uuid.uuid4())
                chunk_ids.append(cid)
                rows.append({
                    "chunk_id":        cid,
                    "scope":           "local",
                    "indexed_at":      indexed_at,
                    "chunk_index":     global_idx,
                    "chunk_total":     total,
                    "text":            self._crypto.encrypt(chunk_text_val),
                    "vector":          [float(v) for v in vector],
                    "is_deleted":      False,
                    "deleted_at":      "",
                    "miss_count":      0,
                    "mtime":           mtime,
                    "mail_uid":        parsed["mail_uid"],
                    "source_type":     "knox",
                    "message_id":      parsed["message_id"],
                    "subject":         parsed["subject"],
                    "sender":          parsed["sender"],
                    "recipients":      parsed["recipients"],
                    "mail_date":       parsed["mail_date"],
                    "thread_ref":      parsed["thread_ref"],
                    "source_file":     parsed["source_file"],
                    "chunk_origin":    "body",
                    "attach_filename": "",
                    "attach_sha256":   "",
                    "source_meta":     _inject_version(parsed["source_meta"]),
                })

            self.table.add(rows)

            if on_progress:
                on_progress(batch_start + len(batch), total)

        logger.info(
            "[email_indexer] 인덱싱 완료: uid=%s chunks=%d",
            parsed["mail_uid"][:30], len(chunk_ids),
        )
        return chunk_ids

    def delete_mail_chunks(self, mail_uid: str) -> None:
        """mail_uid에 해당하는 모든 청크를 emails 테이블에서 삭제한다."""
        try:
            self.table.delete(f"mail_uid = '{mail_uid}'")
        except Exception as exc:
            logger.warning("[email_indexer] 청크 삭제 실패 (uid=%s): %s", mail_uid[:30], exc)

    def optimize(self) -> None:

        """emails 테이블을 최적화한다."""
        self.table.optimize()
