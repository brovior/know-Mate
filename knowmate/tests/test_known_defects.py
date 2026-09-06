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
