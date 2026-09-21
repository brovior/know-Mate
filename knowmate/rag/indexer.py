"""LanceDB 스키마 및 Indexer 클래스 (CLAUDE.md 6-2, 6-3)."""
import getpass
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa

from knowmate.rag.chunker import chunk_text
from knowmate.rag.embedding import EmbeddingClient, VECTOR_DIM
from knowmate.rag.lance_maintenance import LanceTableMaintenance

logger = logging.getLogger(__name__)

# 문서 인덱싱 포맷 버전 — 변경 시 기존 문서 자동 재인덱싱 (state.index_version 비교)
DOC_INDEX_VERSION = "3"

SCHEMA = pa.schema(
    [
        pa.field("chunk_id", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("file_type", pa.string()),
        pa.field("scope", pa.string()),
        pa.field("owner", pa.string()),
        pa.field("acl_group", pa.string()),
        pa.field("mtime", pa.float64()),
        pa.field("indexed_at", pa.string()),
        pa.field("chunk_index", pa.int32()),
        pa.field("chunk_total", pa.int32()),
        # A generation is the atomic document replacement unit.  ``chunk_id``
        # intentionally stays random so an old delete can never remove a new
        # row which happens to reuse a deterministic identifier.
        pa.field("doc_uid", pa.string()),
        pa.field("revision", pa.string()),
        pa.field("generation_id", pa.string()),
        pa.field("text", pa.string()),    # AES-256-GCM 암호화 저장 (CLAUDE.md 5장 4번)
        pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
        pa.field("is_deleted", pa.bool_()),
        pa.field("deleted_at", pa.string()),
        pa.field("miss_count", pa.int32()),
    ]
)

TABLE_NAME = "chunks"


class Indexer:
    def __init__(
        self,
        db_path: str | Path,
        embed_client: EmbeddingClient,
        chunk_size: int = 400,
        overlap: int = 80,
        batch_size: int = 32,
        crypto=None,
        maintenance_config=None,
    ) -> None:
        """LanceDB에 연결하고 chunks 테이블을 준비한다.

        crypto: CryptoManager 또는 FakeCryptoManager 인스턴스.
                None이면 FakeCryptoManager를 사용한다.
        """
        import lancedb  # type: ignore

        self._embed = embed_client
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._batch_size = batch_size
        self._max_chunks_per_file = 500
        self._xlsx_max_rows_per_sheet = 2000

        if crypto is None:
            from knowmate.secure.crypto import FakeCryptoManager
            self._crypto = FakeCryptoManager()
        else:
            self._crypto = crypto

        self._db = lancedb.connect(str(db_path))
        self.table_was_recreated = False
        try:
            self._table = self._db.open_table(TABLE_NAME)
        except Exception:
            self._table = self._db.create_table(TABLE_NAME, schema=SCHEMA)
            self.table_was_recreated = True
        self._ensure_generation_schema()
        try:
            self.table_is_empty = self._table.count_rows() == 0
        except Exception:
            self.table_is_empty = False
        self._maintenance = LanceTableMaintenance(
            self._table, TABLE_NAME, maintenance_config, db_path=db_path,
            recreated=self.table_was_recreated, confirmed_empty=self.table_is_empty,
        )

    @property
    def table(self) -> Any:
        """LanceDB 테이블 객체를 반환한다."""
        return self._table

    def index_file(
        self,
        path: str,
        text: str,
        mtime: float,
        scope: str,
        *,
        doc_uid: str | None = None,
        revision: str | None = None,
        generation_id: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """파일 텍스트를 청킹·임베딩·암호화해 LanceDB에 저장하고 chunk_id 리스트를 반환한다."""
        file_type = Path(path).suffix.lower().lstrip(".")
        doc_uid = doc_uid or self.document_uid(path)
        revision = revision or self.document_revision(path, mtime=mtime, size=None)
        generation_id = generation_id or str(uuid.uuid4())
        # 파일명·경로를 본문 앞에 붙여 제목/폴더명 언급 질의도 벡터 검색에 매칭되게 한다
        p = Path(path)
        meta_header = f"파일명: {p.name}\n경로: {p.parent}\n\n"
        chunks = chunk_text(
            meta_header + text, file_type, self._chunk_size, self._overlap,
            max_chunks_per_file=self._max_chunks_per_file,
            xlsx_max_rows_per_sheet=self._xlsx_max_rows_per_sheet,
        )
        if not chunks:
            return []

        owner = getpass.getuser()
        indexed_at = datetime.now(timezone.utc).isoformat()
        total = len(chunks)
        chunk_ids: list[str] = []
        all_rows: list[dict[str, Any]] = []
        embed_sec = 0.0

        logger.debug("청크 수: %d, 배치 크기: %d", total, self._batch_size)
        for batch_start in range(0, total, self._batch_size):
            batch = chunks[batch_start : batch_start + self._batch_size]
            logger.debug("배치 임베딩 시작: %d~%d / %d", batch_start, batch_start + len(batch) - 1, total)
            t0 = time.perf_counter()
            vectors = self._embed.embed(batch)
            if len(vectors) != len(batch):
                raise ValueError("embedding result count does not match document chunks")
            embed_sec += time.perf_counter() - t0
            logger.debug("배치 임베딩 완료: %d~%d", batch_start, batch_start + len(batch) - 1)

            for i, (chunk_text_val, vector) in enumerate(zip(batch, vectors)):
                global_idx = batch_start + i
                cid = str(uuid.uuid4())
                chunk_ids.append(cid)
                all_rows.append(
                    {
                        "chunk_id": cid,
                        "file_path": path,
                        "file_type": file_type,
                        "scope": scope,
                        "owner": owner,
                        "acl_group": "",
                        "mtime": mtime,
                        "indexed_at": indexed_at,
                        "chunk_index": global_idx,
                        "chunk_total": total,
                        "doc_uid": doc_uid,
                        "revision": revision,
                        "generation_id": generation_id,
                        "text": self._crypto.encrypt(chunk_text_val),  # AES-256-GCM 암호화
                        "vector": [float(v) for v in vector],
                        "is_deleted": False,
                        "deleted_at": "",
                        "miss_count": 0,
                    }
                )

            if on_progress:
                on_progress(min(batch_start + len(batch), total), total)

        # 파일당 add() 1회 — 배치마다 add하면 LanceDB fragment가 과도하게 쌓여
        # 검색·후속 쓰기 성능이 저하된다.
        t0 = time.perf_counter()
        self._table.add(all_rows)
        self._maintenance.record_mutation()
        self.table_is_empty = False
        save_sec = time.perf_counter() - t0

        logger.info(
            "인덱싱 완료: path=%s chunks=%d scope=%s embed=%.2fs save=%.2fs",
            path, total, scope, embed_sec, save_sec,
        )
        return chunk_ids

    @staticmethod
    def document_uid(path: str) -> str:
        """Return the stable, case-insensitive identity for a document path."""
        from knowmate.collector.scanner import normalize_path_key
        return hashlib.sha256(normalize_path_key(path).encode("utf-8")).hexdigest()

    @classmethod
    def document_revision(
        cls, path: str, *, mtime: float, size: int | None, mtime_ns: int | None = None,
    ) -> str:
        """Return the exact source revision used to validate a DB generation."""
        if mtime_ns is None:
            mtime_ns = int(mtime * 1_000_000_000)
        material = f"{cls.document_uid(path)}|{size if size is not None else ''}|{mtime_ns}|{DOC_INDEX_VERSION}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def recover_generation(self, doc_uid: str, revision: str) -> dict[str, Any] | None:
        """Return one verified active generation for a source revision.

        A database read failure is deliberately raised: callers must not add
        another document while a prior commit is uncertain.
        """
        rows = self._document_metadata(doc_uid)
        candidates = self._complete_generations(rows, revision=revision, active_only=True)
        if not candidates:
            return None
        winner = self._pick_generation(candidates)
        winner["old_chunk_ids"] = self._other_generation_ids(rows, winner["generation_id"])
        return winner

    def generation_cleanup_ids(self, doc_uid: str, keep_generation_id: str | None = None) -> list[str]:
        """Return DB-derived stale IDs for one document, with no state dependency."""
        return self._other_generation_ids(self._document_metadata(doc_uid), keep_generation_id)

    def path_cleanup_ids(self, path: str) -> list[str]:
        """Return legacy or malformed rows for an exact stored source path."""
        return list(dict.fromkeys(
            row["chunk_id"] for row in self._document_metadata_for_path(path)
            if isinstance(row.get("chunk_id"), str) and row["chunk_id"]
        ))

    def recover_document_state(self) -> list[dict[str, Any]]:
        """Return untrusted path cleanup entries after an unfinished run.

        Startup does not select a generation by wall-clock timestamp.  A live
        source is later matched to its exact revision by ``recover_generation``;
        a missing source is handed to normal orphan cleanup with every row ID.
        """
        rows = self._document_metadata(None)
        by_path: dict[str, list[str]] = {}
        for row in rows:
            cid, path = row.get("chunk_id"), row.get("file_path")
            if isinstance(cid, str) and cid and isinstance(path, str) and path:
                by_path.setdefault(path, []).append(cid)
        return [
            {"file_path": path, "chunk_ids": list(dict.fromkeys(ids)), "untrusted": True}
            for path, ids in by_path.items()
        ]

    def _ensure_generation_schema(self) -> None:
        """Add nullable generation columns without replacing existing Lance data."""
        missing = [field for field in SCHEMA if field.name not in self._table.schema.names]
        if not missing:
            return
        try:
            self._table.add_columns(missing)
        except Exception as exc:
            # Old rows remain readable, but writes without these fields would
            # make crash recovery ambiguous.  Fail closed rather than recreate
            # or delete the user's table.
            raise RuntimeError("chunks table generation schema migration failed") from exc

    def _document_metadata(self, doc_uid: str | None) -> list[dict[str, Any]]:
        """Read all document metadata with an explicit result bound."""
        columns = [
            "chunk_id", "file_path", "mtime", "indexed_at", "chunk_index", "chunk_total",
            "doc_uid", "revision", "generation_id", "is_deleted",
        ]
        try:
            # Explicit None disables Lance search's default result limit, so a
            # generation or stale-document cleanup is never silently truncated.
            query = self._table.search()
            if doc_uid is not None:
                safe = doc_uid.replace("'", "''")
                query = query.where(f"doc_uid = '{safe}'")
            arrow = query.select(columns).limit(None).to_arrow()
            return arrow.to_pylist()
        except Exception as exc:
            logger.error("document generation metadata read failed: %s", exc)
            raise RuntimeError("document generation metadata read failed") from exc

    def _document_metadata_for_path(self, path: str) -> list[dict[str, Any]]:
        """Read projected metadata for one legacy path without a default limit."""
        columns = ["chunk_id", "file_path", "doc_uid", "generation_id"]
        try:
            safe = path.replace("'", "''")
            return self._table.search().where(
                f"file_path = '{safe}'"
            ).select(columns).limit(None).to_arrow().to_pylist()
        except Exception as exc:
            logger.error("document legacy-path metadata read failed: %s", exc)
            raise RuntimeError("document legacy-path metadata read failed") from exc

    @staticmethod
    def _complete_generations(
        rows: list[dict[str, Any]], *, revision: str | None, active_only: bool,
    ) -> list[dict[str, Any]]:
        """Validate exact chunk coverage before treating rows as committed."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            generation = row.get("generation_id")
            if isinstance(generation, str) and generation:
                groups.setdefault(generation, []).append(row)
        complete: list[dict[str, Any]] = []
        for generation, group in groups.items():
            first = group[0]
            expected_revision = first.get("revision")
            if not isinstance(expected_revision, str) or not expected_revision:
                continue
            if revision is not None and expected_revision != revision:
                continue
            if any(row.get("revision") != expected_revision for row in group):
                continue
            uid = first.get("doc_uid")
            path = first.get("file_path")
            if not isinstance(uid, str) or not uid or not isinstance(path, str) or not path:
                continue
            if any(
                row.get("doc_uid") != uid
                or row.get("file_path") != path
                or row.get("mtime") != first.get("mtime")
                or row.get("indexed_at") != first.get("indexed_at")
                for row in group
            ):
                continue
            if Indexer.document_uid(path) != uid:
                continue
            if active_only and any(row.get("is_deleted") is not False for row in group):
                continue
            total = first.get("chunk_total")
            if type(total) is not int or total < 1 or any(row.get("chunk_total") != total for row in group):
                continue
            ids = [row.get("chunk_id") for row in group]
            indices = [row.get("chunk_index") for row in group]
            if (len(group) != total or len(set(ids)) != total or any(not isinstance(cid, str) or not cid for cid in ids)
                    or set(indices) != set(range(total))):
                continue
            complete.append({
                "generation_id": generation, "revision": expected_revision,
                "chunk_ids": list(ids), "chunk_total": total,
                "file_path": first.get("file_path"), "mtime": first.get("mtime"),
                "indexed_at": first.get("indexed_at") or "",
            })
        return complete

    @staticmethod
    def _pick_generation(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """Choose a deterministic winner when a crash left duplicate generations."""
        return max(candidates, key=lambda item: (str(item.get("indexed_at", "")), item["generation_id"]))

    @staticmethod
    def _other_generation_ids(rows: list[dict[str, Any]], winner: str) -> list[str]:
        """Return every non-winner row ID, including incomplete and soft-deleted rows."""
        return list(dict.fromkeys(
            row["chunk_id"] for row in rows
            if row.get("generation_id") != winner and isinstance(row.get("chunk_id"), str) and row["chunk_id"]
        ))

    def delete_chunks(self, chunk_ids: list[str]) -> None:
        """chunk_id 목록을 2단계 soft delete한다.

        1차 miss(miss_count=0): is_deleted=true, miss_count=1 마킹.
        2차 miss(miss_count>=1): 물리 삭제.
        """
        if not chunk_ids:
            return

        now = datetime.now(timezone.utc).isoformat()
        id_list = ", ".join(f"'{cid}'" for cid in chunk_ids)

        # 현재 상태 조회
        df = (
            self._table.search()
            .where(f"chunk_id IN ({id_list})")
            .select(["chunk_id", "miss_count"])
            .limit(len(chunk_ids) * 2)
            .to_arrow()
            .to_pandas()
        )

        if df.empty:
            return

        # miss_count == 0 → 1차 miss: soft delete 마킹
        first_miss_ids = df.loc[df["miss_count"] == 0, "chunk_id"].tolist()
        if first_miss_ids:
            fm_list = ", ".join(f"'{cid}'" for cid in first_miss_ids)
            self._table.update(
                where=f"chunk_id IN ({fm_list})",
                values={"is_deleted": True, "deleted_at": now, "miss_count": 1},
            )
            self._maintenance.record_mutation()
            logger.info("soft delete 마킹(1차): %d건", len(first_miss_ids))

        # miss_count >= 1 → 2차 miss: 물리 삭제
        hard_delete_ids = df.loc[df["miss_count"] >= 1, "chunk_id"].tolist()
        if hard_delete_ids:
            hd_list = ", ".join(f"'{cid}'" for cid in hard_delete_ids)
            self._table.delete(f"chunk_id IN ({hd_list})")
            self._maintenance.record_mutation()
            logger.info("물리 삭제(2차): %d건", len(hard_delete_ids))

    def delete_chunks_permanently(self, chunk_ids: list[str]) -> None:
        """교체가 완료된 기존 chunk_id 목록을 즉시 물리 삭제한다."""
        if not chunk_ids:
            return

        id_list = ", ".join(f"'{cid}'" for cid in chunk_ids)
        self._table.delete(f"chunk_id IN ({id_list})")
        self._maintenance.record_mutation()
        logger.info("교체된 기존 청크 물리 삭제: %d건", len(chunk_ids))

    def delete_file_chunks(self, path: str) -> None:
        """경로의 문서 청크를 삭제하고 유지보수 mutation으로 기록한다."""
        safe = path.replace("'", "''")
        self._table.delete(f"file_path = '{safe}'")
        self._maintenance.record_mutation()

    @property
    def maintenance_in_progress(self) -> bool:
        return self._maintenance.in_progress

    def maintenance_periodic_due(self) -> bool:
        return self._maintenance.periodic_due()

    def mark_maintenance_backlog(self) -> bool:
        """Mark a streaming document backlog before writes begin."""
        return self._maintenance.mark_backlog_active()

    def run_hard_limit_maintenance(self, **kwargs) -> bool:
        return self._maintenance.checkpoint_hard_limit(**kwargs)

    def finish_maintenance_backlog(self, **kwargs) -> bool:
        """Close a completed document stream after its state checkpoint."""
        return self._maintenance.finish_backlog(**kwargs)

    def run_steady_maintenance(self, **kwargs) -> bool:
        return self._maintenance.checkpoint_steady(**kwargs)

    def run_startup_maintenance(self, **kwargs) -> bool:
        return self._maintenance.run_startup_check(**kwargs)

    def run_periodic_maintenance(self, **kwargs) -> bool:
        return self._maintenance.run_periodic(**kwargs)

    def run_cycle_end_maintenance(self, **kwargs) -> bool:
        return self._maintenance.run_cycle_end(**kwargs)

    def optimize(self) -> None:
        """LanceDB optimize()로 삭제 데이터를 정리한다 (compact_files() 사용 금지)."""
        self._table.optimize()
        self._maintenance.note_external_optimize_success()
        logger.info("LanceDB optimize 완료")
