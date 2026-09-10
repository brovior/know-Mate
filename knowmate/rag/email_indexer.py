"""emails 테이블 스키마 및 EmailIndexer (Knox .mysingle 전용)."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa

from knowmate.rag.chunker import chunk_text
from knowmate.rag.embedding import (
    EmbeddingClient,
    EmbeddingContentError,
    VECTOR_DIM,
    _validate_vectors,
)

logger = logging.getLogger(__name__)

# 인덱싱 포맷 버전 — 변경 시 기존 메일 자동 재인덱싱
EMAIL_INDEX_VERSION = "3"  # v3: mail_date_ts(epoch) 필드 추가 — 날짜 범위 검색용

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
    pa.field("mail_date",       pa.string()),      # RFC 원문 문자열 (표시용)
    pa.field("mail_date_ts",    pa.float64()),      # epoch (날짜 범위 검색용). 파싱 실패 시 0.0
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


class MailIndexState(str, Enum):
    """메일 DB 조회 뒤 스캐너가 취할 명시적인 상태다."""

    CURRENT = "current"
    MISSING = "missing"
    STALE = "stale"
    ERROR = "error"


@dataclass(frozen=True)
class MailIndexCheck:
    """DB 조회 결과와 안전한 교체에 필요한 기존 청크 ID다."""

    state: MailIndexState
    old_chunk_ids: tuple[str, ...] = ()


class PendingMailDeleteError(RuntimeError):
    """새 행 저장 뒤 기존 행 삭제가 실패해 호출자가 ID를 보존해야 함을 알린다."""

    def __init__(self, chunk_ids: tuple[str, ...], cause: Exception) -> None:
        """재시도할 청크 ID와 원래 삭제 오류를 보관한다."""
        super().__init__("새 메일 청크 저장 뒤 기존 청크 삭제에 실패했습니다")
        self.chunk_ids = chunk_ids
        self.__cause__ = cause


@dataclass
class MailIndexJob:
    """메일 하나의 독립 청킹 결과와 교차메일 임베딩 작업 상태다."""

    parsed: dict[str, Any]
    mtime: float
    check: MailIndexCheck
    chunks: list[str]
    vectors: list[list[float] | None] = field(default_factory=list)
    content_error: EmbeddingContentError | None = None


@dataclass
class MailEmbeddingResult:
    """교차메일 임베딩의 격리 실패와 중단 원인을 전달한다."""

    blocking_error: Exception | None = None
    input_chunks: int = 0
    batch_count: int = 0
    embed_calls: int = 0
    split_retries: int = 0


def _parse_mail_date_ts(mail_date: str) -> float:
    """RFC822 Date 헤더 문자열을 epoch(float)로 변환한다. 파싱 실패 시 0.0."""
    if not mail_date:
        return 0.0
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(mail_date)
        if dt is None:
            return 0.0
        return dt.timestamp()
    except Exception:
        return 0.0


def _inject_version(source_meta: str) -> str:
    """source_meta JSON 문자열에 _index_version 필드를 삽입한다."""
    import json
    try:
        meta = json.loads(source_meta or "{}")
    except Exception:
        meta = {}
    meta["_index_version"] = EMAIL_INDEX_VERSION
    return json.dumps(meta, ensure_ascii=False)


# pyarrow 스칼라 타입 → 누락 컬럼 마이그레이션용 SQL 기본값(DataFusion)
_MIGRATION_DEFAULT_SQL = {
    pa.float64(): "CAST(0.0 AS DOUBLE)",
    pa.int32():   "CAST(0 AS INT)",
    pa.string():  "CAST('' AS STRING)",
    pa.bool_():   "CAST(FALSE AS BOOLEAN)",
}


def _migrate_emails_schema(db, table) -> tuple[object, bool]:
    """기존 emails 테이블에 EMAIL_SCHEMA 대비 누락된 스칼라 컬럼을 채운다.

    v3에서 mail_date_ts(float64)가 추가됐으나, 그 이전에 생성된 테이블은 이 컬럼이
    없어 index_mail의 table.add()·날짜필터 WHERE가 'field ... does not exist in table
    schema'로 실패한다. LanceDB add_columns로 누락 컬럼을 기본값과 함께 추가해
    재생성 없이 마이그레이션한다(기존 행 보존). add_columns 미지원·실패 시에는
    테이블을 재생성해 폴백한다(메일은 백업저장소 = .mysingle/.eml에서 재인덱싱됨).

    반환: 마이그레이션(또는 재생성)된 테이블 객체.
    """
    try:
        existing = set(table.schema.names)
    except Exception as exc:
        logger.warning("[email_indexer] 스키마 조회 실패, 마이그레이션 생략: %s", exc)
        return table, False

    to_add: dict[str, str] = {}
    unsupported: list[str] = []
    for field in EMAIL_SCHEMA:
        if field.name in existing:
            continue
        default_sql = _MIGRATION_DEFAULT_SQL.get(field.type)
        if default_sql is None:  # list/vector 등 — add_columns 기본값 생성 불가
            unsupported.append(field.name)
        else:
            to_add[field.name] = default_sql

    if not to_add and not unsupported:
        return table, False  # 최신 스키마 — 변경 없음

    if to_add:
        try:
            table.add_columns(to_add)
            logger.info("[email_indexer] emails 스키마 마이그레이션 — 컬럼 추가: %s", list(to_add))
        except Exception as exc:
            logger.error(
                "[email_indexer] add_columns 실패(%s) — 테이블 재생성으로 폴백. "
                "기존 메일은 다음 스캔에서 재인덱싱됩니다: %s", list(to_add), exc,
            )
            unsupported = []  # 재생성이 모든 누락을 해소
            try:
                db.drop_table(EMAIL_TABLE_NAME)
            except Exception:
                pass
            return db.create_table(EMAIL_TABLE_NAME, schema=EMAIL_SCHEMA), True

    if unsupported:
        logger.error(
            "[email_indexer] 자동 추가 불가한 누락 컬럼 %s — 테이블 재생성으로 폴백. "
            "기존 메일은 다음 스캔에서 재인덱싱됩니다.", unsupported,
        )
        try:
            db.drop_table(EMAIL_TABLE_NAME)
        except Exception:
            pass
        return db.create_table(EMAIL_TABLE_NAME, schema=EMAIL_SCHEMA), True

    return table, False


def get_or_create_emails_table(db, *, with_status: bool = False):
    """emails 테이블을 열거나 생성하고, 요청 시 재생성 여부도 반환한다."""
    if EMAIL_TABLE_NAME in db.table_names():
        table = db.open_table(EMAIL_TABLE_NAME)
        result, recreated = _migrate_emails_schema(db, table)
    else:
        result, recreated = db.create_table(EMAIL_TABLE_NAME, schema=EMAIL_SCHEMA), True
    if with_status:
        return result, recreated
    return result


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
        self.table, self.table_was_recreated = get_or_create_emails_table(db, with_status=True)
        try:
            self.table_is_empty = self.table.count_rows() == 0
        except Exception:
            self.table_is_empty = False

    def get_index_state(self, mail_uid: str, mtime: float) -> MailIndexCheck:
        """현재 파일과 DB 행의 관계 및 안전한 교체 대상 ID를 조회한다."""
        import json
        safe_uid = mail_uid.replace("'", "''")
        try:
            rows = (
                self.table.search()
                .where(f"mail_uid = '{safe_uid}' AND is_deleted = false")
                .select(["chunk_id", "mtime", "source_meta"])
                .to_arrow()
                .to_pylist()
            )
        except Exception as exc:
            logger.warning("[email_indexer] 상태 조회 실패 (uid=%s): %s", mail_uid[:20], exc)
            return MailIndexCheck(MailIndexState.ERROR)
        if not rows:
            return MailIndexCheck(MailIndexState.MISSING)

        chunk_ids = tuple(
            row["chunk_id"] for row in rows if isinstance(row.get("chunk_id"), str)
        )
        is_current = bool(chunk_ids)
        for row in rows:
            try:
                meta = json.loads(row.get("source_meta", "{}") or "{}")
                is_current = is_current and (
                    abs(float(row["mtime"]) - mtime) < 1.0
                    and meta.get("_index_version") == EMAIL_INDEX_VERSION
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                is_current = False
        return MailIndexCheck(
            MailIndexState.CURRENT if is_current else MailIndexState.STALE,
            chunk_ids,
        )

    def is_indexed(self, mail_uid: str, mtime: float) -> bool:
        """하위호환용 bool 조회; 새 수집 경로는 ``get_index_state``를 사용한다."""
        return self.get_index_state(mail_uid, mtime).state is MailIndexState.CURRENT

    def index_mail(
        self,
        parsed: dict,
        mtime: float,
        on_progress: Callable[[int, int], None] | None = None,
        *,
        index_check: MailIndexCheck | None = None,
        delete_old: bool = True,
    ) -> list[str]:
        """
        파싱된 메일 dict를 청킹·임베딩·암호화해 emails 테이블에 저장한다.
        chunk_id 리스트를 반환한다.
        """
        check = index_check or self.get_index_state(parsed["mail_uid"], mtime)
        if check.state is MailIndexState.ERROR:
            raise RuntimeError("메일 기존 청크 상태를 확인하지 못했습니다")
        if check.state is MailIndexState.CURRENT:
            return []
        job = self.prepare_mail(parsed, mtime, check)
        result = self.embed_mail_jobs([job])
        if job.content_error:
            raise job.content_error
        if result.blocking_error:
            raise result.blocking_error
        chunk_ids = self.commit_mail_job(job, on_progress)

        if delete_old and job.chunks and check.state is MailIndexState.STALE and check.old_chunk_ids:
            try:
                self.delete_chunk_ids(check.old_chunk_ids)
            except Exception as exc:
                raise PendingMailDeleteError(check.old_chunk_ids, exc) from exc

        return chunk_ids

    def prepare_mail(self, parsed: dict[str, Any], mtime: float, check: MailIndexCheck) -> MailIndexJob:
        """메일별 메타헤더·청킹을 독립적으로 끝내고 교차메일 임베딩 작업을 만든다."""
        meta_header = (
            f"제목: {parsed.get('subject', '')}\n"
            f"발신: {parsed.get('sender', '')}\n"
            f"수신: {parsed.get('recipients', '')}\n"
            f"날짜: {parsed.get('mail_date', '')}\n\n"
        )
        chunks = chunk_text(meta_header + parsed["body_text"], "txt", self._chunk_size, self._overlap)
        return MailIndexJob(
            parsed=parsed,
            mtime=mtime,
            check=check,
            chunks=chunks,
            vectors=[None] * len(chunks),
        )

    def embed_mail_jobs(self, jobs: list[MailIndexJob]) -> MailEmbeddingResult:
        """여러 메일 청크를 batch_size 단위로 임베딩하고 결과를 원래 job에 되돌린다."""
        result = MailEmbeddingResult()
        refs = [(job, index) for job in jobs for index in range(len(job.chunks))]
        result.input_chunks = len(refs)
        for start in range(0, len(refs), self._batch_size):
            result.batch_count += 1
            if not self._embed_refs_with_content_isolation(refs[start:start + self._batch_size], result):
                break
        return result

    def _embed_refs_with_content_isolation(
        self, refs: list[tuple[MailIndexJob, int]], result: MailEmbeddingResult,
    ) -> bool:
        """ContentError만 이분화하고 transient·protocol 오류면 해당 사이클을 중단한다."""
        if not refs:
            return True
        try:
            result.embed_calls += 1
            vectors = self._embed.embed([job.chunks[index] for job, index in refs])
            vectors = _validate_vectors(vectors, len(refs))
            if len(vectors) != len(refs):
                raise RuntimeError(f"임베딩 결과 수 불일치: 요청={len(refs)}, 응답={len(vectors)}")
        except EmbeddingContentError as exc:
            if len(refs) == 1:
                refs[0][0].content_error = exc
                return True
            result.split_retries += 1
            middle = len(refs) // 2
            return (
                self._embed_refs_with_content_isolation(refs[:middle], result)
                and self._embed_refs_with_content_isolation(refs[middle:], result)
            )
        except Exception as exc:
            result.blocking_error = exc
            return False
        for (job, index), vector in zip(refs, vectors, strict=True):
            job.vectors[index] = vector
        return True

    def commit_mail_job(
        self, job: MailIndexJob, on_progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """모든 청크 벡터가 준비된 메일 하나만 새 행으로 원자적 추가를 시도한다."""
        if job.content_error or any(vector is None for vector in job.vectors):
            raise RuntimeError("메일 전체 청크 임베딩이 완료되지 않았습니다")
        if not job.chunks:
            return []

        parsed = job.parsed
        indexed_at = datetime.now(timezone.utc).isoformat()
        mail_date_ts = _parse_mail_date_ts(parsed.get("mail_date", ""))
        chunk_ids = [str(uuid.uuid4()) for _ in job.chunks]
        rows: list[dict[str, Any]] = []
        for index, (chunk_text_value, vector, chunk_id) in enumerate(
            zip(job.chunks, job.vectors, chunk_ids, strict=True)
        ):
            rows.append({
                "chunk_id": chunk_id,
                "scope": "local",
                "indexed_at": indexed_at,
                "chunk_index": index,
                "chunk_total": len(job.chunks),
                "text": self._crypto.encrypt(chunk_text_value),
                "vector": [float(value) for value in vector],
                "is_deleted": False,
                "deleted_at": "",
                "miss_count": 0,
                "mtime": job.mtime,
                "mail_uid": parsed["mail_uid"],
                "source_type": parsed.get("source_type", "knox"),
                "message_id": parsed["message_id"],
                "subject": parsed["subject"],
                "sender": parsed["sender"],
                "recipients": parsed["recipients"],
                "mail_date": parsed["mail_date"],
                "mail_date_ts": mail_date_ts,
                "thread_ref": parsed["thread_ref"],
                "source_file": parsed["source_file"],
                "chunk_origin": "body",
                "attach_filename": "",
                "attach_sha256": "",
                "source_meta": _inject_version(parsed["source_meta"]),
            })
        self.table.add(rows)
        if on_progress:
            on_progress(len(rows), len(rows))
        return chunk_ids

    def delete_mail_chunks(self, mail_uid: str) -> None:
        """mail_uid의 현재 청크 ID를 먼저 조회한 뒤 그 ID만 삭제한다."""
        safe_uid = mail_uid.replace("'", "''")
        try:
            rows = (
                self.table.search()
                .where(f"mail_uid = '{safe_uid}'")
                .select(["chunk_id"])
                .to_arrow()
                .to_pylist()
            )
            self.delete_chunk_ids([row["chunk_id"] for row in rows if isinstance(row.get("chunk_id"), str)])
        except Exception as exc:
            logger.warning("[email_indexer] 청크 삭제 실패 (uid=%s): %s", mail_uid[:30], exc)

    def delete_chunk_ids(self, chunk_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        """캡처한 기존 청크 ID만 삭제하고 성공한 정확한 ID를 반환한다."""
        ids = list(dict.fromkeys(chunk_id for chunk_id in chunk_ids if isinstance(chunk_id, str)))
        if not ids:
            return ()
        quoted_ids = []
        for chunk_id in ids:
            safe_chunk_id = chunk_id.replace("'", "''")
            quoted_ids.append(f"'{safe_chunk_id}'")
        quoted = ", ".join(quoted_ids)
        self.table.delete(f"chunk_id IN ({quoted})")
        return tuple(ids)

    def optimize(self) -> None:

        """emails 테이블을 최적화한다."""
        self.table.optimize()
