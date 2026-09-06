"""교차검증(`docs/PERF_ANALYSIS_REVIEW.md`)에서 확인된 미수정 결함의 재현 테스트.

여기 있는 테스트는 **고쳐야 할 동작을 assert**하므로 현재는 전부 xfail이다.
결함을 고치면 XPASS로 뒤집혀 실패하므로(strict), 그때 xfail 표시를 지우고
해당 주제의 테스트 파일로 옮긴다.

모든 테스트는 extractor:fake + embedding:fake 조건에서 사외에서도 실행된다.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from knowmate.rag.embedding import EmbeddingClient

FIXTURES = Path(__file__).parent / "fixtures"

_HAS_LANCEDB = bool(__import__("importlib").util.find_spec("lancedb"))
pytestmark = pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치 — 폐쇄망 환경에서 실행")


def _fake_embed() -> EmbeddingClient:
    """테스트용 fake 임베딩 클라이언트를 반환한다."""
    return EmbeddingClient(base_url="", host_header="", fake=True)


def _write_mail(dest: Path, uid: str, msgid: str) -> None:
    """sample.mysingle의 고유 ID만 바꿔 서로 다른 메일 파일을 만든다.

    write_bytes를 쓴다 — write_text는 Windows에서 CRLF를 CR CR LF로 바꿔
    메일 헤더 파싱을 깨뜨린다.
    """
    raw = (FIXTURES / "sample.mysingle").read_bytes()
    raw = raw.replace(b"2026062600000001", uid.encode())
    raw = raw.replace(b"<test-001@company.com>", f"<{msgid}@company.com>".encode())
    dest.write_bytes(raw)


def _count_rows(table) -> tuple[int, int]:
    """테이블의 (전체 행 수, is_deleted=false 행 수)를 반환한다."""
    df = (
        table.search()
        .select(["chunk_id", "is_deleted"])
        .limit(1_000_000)
        .to_arrow()
        .to_pandas()
    )
    if df.empty:
        return 0, 0
    return len(df), int((~df["is_deleted"]).sum())


@pytest.mark.xfail(
    strict=True,
    reason="scan_mail_folders가 DB 상태 확인 전에 mtime 상위 N건으로 자른다 "
           "— 한도를 넘는 오래된 메일이 후보에 들어오지 못한다",
)
def test_mail_beyond_scan_limit_is_eventually_indexed(tmp_path):
    """스캔 한도를 넘는 메일도 사이클을 반복하면 결국 인덱싱돼야 한다."""
    from knowmate.collector.mail_scanner import run_mail_scan
    from knowmate.rag.email_indexer import EmailIndexer

    watch = tmp_path / "watch"
    watch.mkdir()
    for i, (name, ts) in enumerate(
        [("new", 3_000_000_000), ("mid", 2_000_000_000), ("old", 1_000_000_000)]
    ):
        path = watch / f"{name}.mysingle"
        _write_mail(path, uid=f"2026062600{i:06d}", msgid=f"mail-{name}")
        os.utime(path, (ts, ts))

    indexer = EmailIndexer(db_path=tmp_path / "db", embed_client=_fake_embed())
    cfg = {"mail": {"max_mails_per_scan": 2, "batch_commit_every": 10}}

    for _ in range(5):
        run_mail_scan([str(watch)], indexer, cfg)

    df = (
        indexer.table.search()
        .select(["source_file"])
        .limit(1_000_000)
        .to_arrow()
        .to_pandas()
    )
    indexed = {Path(p).stem for p in df["source_file"].unique()} if not df.empty else set()
    assert "old" in indexed, f"5사이클을 돌려도 오래된 메일이 인덱싱되지 않음. 인덱싱된 것: {sorted(indexed)}"


@pytest.mark.xfail(
    strict=True,
    reason="수정 성공 시 state가 새 chunk_ids로 덮여 옛 ID를 찾을 경로가 사라진다 "
           "— soft delete된 행이 영구 누적되고 optimize()도 지우지 못한다",
)
def test_repeated_edits_do_not_accumulate_dead_rows(tmp_path):
    """같은 문서를 반복 수정해도 죽은 행이 쌓이지 않아야 한다."""
    from knowmate.rag.indexer import Indexer

    indexer = Indexer(db_path=tmp_path / "db", embed_client=_fake_embed())
    path = str(tmp_path / "doc.txt")

    chunk_ids = indexer.index_file(path, "첫 번째 내용입니다. " * 30, 1000.0, "local")

    # scheduler의 modified 경로와 같은 순서(옛 청크 삭제 → 새 내용 인덱싱)로 반복
    for i in range(2, 7):
        indexer.delete_chunks(chunk_ids)
        chunk_ids = indexer.index_file(path, f"{i}번째 내용입니다. " * 30, 1000.0 + i, "local")

    indexer.optimize()
    total, active = _count_rows(indexer.table)
    assert total == active, (
        f"수정 5회 후 죽은 행이 {total - active}개 남음 (전체 {total}, 살아있는 것 {active}). "
        "optimize()도 지우지 못한다."
    )


class _FailingEmbed(EmbeddingClient):
    """fail=True인 동안 임베딩 API 실패를 흉내내는 클라이언트."""

    def __init__(self) -> None:
        """fake 모드로 초기화하고 실패 스위치를 꺼 둔다."""
        super().__init__(base_url="", host_header="", fake=True)
        self.fail = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        """fail=True이면 임베딩 API 실패와 같은 예외를 던진다."""
        if self.fail:
            raise RuntimeError("임베딩 API 호출 실패: 테스트용 흉내")
        return super().embed(texts)


def _index_then_fail(tmp_path) -> tuple:
    """정상 인덱싱 1회 후 임베딩을 고장내고 재인덱싱을 1회 실패시킨다."""
    from knowmate.rag.indexer import Indexer

    embed = _FailingEmbed()
    indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
    path = str(tmp_path / "doc.txt")

    chunk_ids = indexer.index_file(path, "정상 내용입니다. " * 30, 1000.0, "local")

    embed.fail = True
    indexer.delete_chunks(chunk_ids)          # scheduler: modified → 옛 청크 삭제
    with pytest.raises(RuntimeError):
        indexer.index_file(path, "새 내용입니다. " * 30, 2000.0, "local")

    return indexer, path, chunk_ids


@pytest.mark.xfail(
    strict=True,
    reason="재인덱싱은 옛 청크를 먼저 soft delete하므로, 임베딩이 실패하면 "
           "그 시점부터 문서가 검색 결과에서 사라진다",
)
def test_failed_reindex_keeps_document_searchable(tmp_path):
    """재인덱싱이 실패해도 직전까지 검색되던 내용은 남아 있어야 한다."""
    indexer, _path, _ids = _index_then_fail(tmp_path)

    total, active = _count_rows(indexer.table)
    assert active > 0, (
        f"인덱싱이 실패했을 뿐인데 문서가 검색에서 사라짐 (전체 {total}행, 살아있는 것 {active}행). "
        "실패는 UNKNOWN_TRANSIENT로 분류돼 재시도가 30분→6시간→7일로 밀린다."
    )


@pytest.mark.xfail(
    strict=True,
    reason="실패 시 state가 갱신되지 않아 다음 사이클이 같은 chunk_ids로 "
           "delete_chunks를 다시 호출하고, 2차 miss로 물리 삭제된다",
)
def test_failed_reindex_does_not_physically_delete(tmp_path):
    """재인덱싱이 연속 실패해도 기존 청크를 물리 삭제하면 안 된다."""
    indexer, path, chunk_ids = _index_then_fail(tmp_path)

    # 다음 사이클: state가 그대로라 같은 ids로 다시 삭제 요청 → 2차 miss
    indexer.delete_chunks(chunk_ids)
    with pytest.raises(RuntimeError):
        indexer.index_file(path, "새 내용입니다. " * 30, 2000.0, "local")

    total, _active = _count_rows(indexer.table)
    assert total > 0, "연속 실패만으로 기존 청크가 물리 삭제됨 — 복구 경로가 남지 않는다."
