"""LanceDB 스키마 및 Indexer 클래스 (CLAUDE.md 6-2, 6-3)."""
import getpass
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa

from knowmate.rag.chunker import chunk_text
from knowmate.rag.embedding import EmbeddingClient, VECTOR_DIM

logger = logging.getLogger(__name__)

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

        if crypto is None:
            from knowmate.secure.crypto import FakeCryptoManager
            self._crypto = FakeCryptoManager()
        else:
            self._crypto = crypto

        self._db = lancedb.connect(str(db_path))
        if TABLE_NAME in self._db.list_tables():
            self._table = self._db.open_table(TABLE_NAME)
        else:
            self._table = self._db.create_table(TABLE_NAME, schema=SCHEMA)

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
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """파일 텍스트를 청킹·임베딩·암호화해 LanceDB에 저장하고 chunk_id 리스트를 반환한다."""
        file_type = Path(path).suffix.lower().lstrip(".")
        chunks = chunk_text(text, file_type, self._chunk_size, self._overlap)
        if not chunks:
            return []

        owner = getpass.getuser()
        indexed_at = datetime.now(timezone.utc).isoformat()
        total = len(chunks)
        chunk_ids: list[str] = []

        for batch_start in range(0, total, self._batch_size):
            batch = chunks[batch_start : batch_start + self._batch_size]
            vectors = self._embed.embed(batch)

            rows: list[dict[str, Any]] = []
            for i, (chunk_text_val, vector) in enumerate(zip(batch, vectors)):
                global_idx = batch_start + i
                cid = str(uuid.uuid4())
                chunk_ids.append(cid)
                rows.append(
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
                        "text": self._crypto.encrypt(chunk_text_val),  # AES-256-GCM 암호화
                        "vector": [float(v) for v in vector],
                        "is_deleted": False,
                        "deleted_at": "",
                        "miss_count": 0,
                    }
                )

            self._table.add(rows)

            if on_progress:
                on_progress(min(batch_start + len(batch), total), total)

        logger.info(
            "인덱싱 완료: path=%s chunks=%d scope=%s", path, total, scope
        )
        return chunk_ids

    def delete_chunks(self, chunk_ids: list[str]) -> None:
        """chunk_id 목록을 soft delete하고, miss_count>=2인 항목은 물리 삭제한다."""
        if not chunk_ids:
            return

        now = datetime.now(timezone.utc).isoformat()
        id_list = ", ".join(f"'{cid}'" for cid in chunk_ids)

        # 현재 상태 조회
        df = (
            self._table.search()
            .where(f"chunk_id IN ({id_list})")
            .limit(len(chunk_ids) * 2)
            .to_arrow()
            .to_pandas()
        )

        if df.empty:
            return

        # miss_count >= 2 → 물리 삭제
        hard_delete_ids = df.loc[df["miss_count"] >= 2, "chunk_id"].tolist()
        if hard_delete_ids:
            hd_list = ", ".join(f"'{cid}'" for cid in hard_delete_ids)
            self._table.delete(f"chunk_id IN ({hd_list})")
            logger.info("물리 삭제: %d건", len(hard_delete_ids))

        # miss_count < 2 → soft delete (miss_count 증가)
        soft_ids = df.loc[df["miss_count"] < 2, "chunk_id"].tolist()
        if soft_ids:
            soft_list = ", ".join(f"'{cid}'" for cid in soft_ids)
            self._table.update(
                where=f"chunk_id IN ({soft_list}) AND is_deleted = false",
                values={
                    "is_deleted": True,
                    "deleted_at": now,
                    "miss_count": 1,
                },
            )
            logger.info("soft delete 마킹: %d건", len(soft_ids))

    def optimize(self) -> None:
        """LanceDB optimize()로 삭제 데이터를 정리한다 (compact_files() 사용 금지)."""
        self._table.optimize()
        logger.info("LanceDB optimize 완료")
