"""질문 스레드 전용 기존 인덱스 읽기 핸들. 스키마/키를 생성하거나 고치지 않는다."""
from pathlib import Path
from types import SimpleNamespace

import lancedb
import pyarrow as pa

from knowmate.rag.embedding import VECTOR_DIM


class QueryIndexUnavailable(RuntimeError):
    """기존 인덱스 또는 읽기 스키마의 준비가 필요하다."""

    def __init__(self, reason: str) -> None:
        """사용자 안내용 고정 사유를 보관한다."""
        super().__init__(reason)
        self.reason = reason


_COMMON = {
    "chunk_id": pa.string(), "text": pa.string(), "scope": pa.string(),
    "is_deleted": pa.bool_(),
    "vector": pa.list_(pa.float32(), VECTOR_DIM),
}
_REQUIRED = {
    "chunks": {**_COMMON, "file_path": pa.string(), "file_type": pa.string(),
               "owner": pa.string(), "mtime": pa.float64()},
    "emails": {**_COMMON, "source_file": pa.string(), "source_type": pa.string(),
               "subject": pa.string(), "sender": pa.string(), "recipients": pa.string(),
               "mail_date": pa.string(), "mail_date_ts": pa.float64()},
}


class QueryReader:
    """요청 안에서만 기존 LanceDB 테이블을 열고 참조를 해제한다."""

    def __init__(self, db_path: Path) -> None:
        """인덱스 경로가 없으면 연결조차 생성하지 않는다."""
        self._db = None
        self.table = None
        self.email_indexer = None
        if not db_path.is_dir():
            raise QueryIndexUnavailable("index_missing")
        try:
            self._db = lancedb.connect(str(db_path))
            names = set(self._db.list_tables().tables)
            for name in ("chunks", "emails"):
                if name not in names:
                    continue
                table = self._db.open_table(name)
                schema = table.schema
                if any(
                    field not in schema.names or schema.field(field).type != data_type
                    for field, data_type in _REQUIRED[name].items()
                ):
                    raise QueryIndexUnavailable("schema_not_ready")
                if name == "chunks":
                    self.table = table
                else:
                    self.email_indexer = SimpleNamespace(table=table)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """공개 close API가 없는 LanceDB 연결/테이블의 요청 참조를 해제한다."""
        self.table = None
        self.email_indexer = None
        self._db = None
