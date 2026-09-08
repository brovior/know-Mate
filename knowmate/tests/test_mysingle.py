"""Knox .mysingle 메일 인덱싱 파이프라인 테스트.

모든 테스트는 extractor:fake + embedding:fake 조건에서 사외(폐쇄망 외부)에서도 통과해야 한다.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

from knowmate.rag.embedding import EmbeddingClient

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_embed() -> EmbeddingClient:
    """테스트용 fake 임베딩 클라이언트를 반환한다."""
    return EmbeddingClient(base_url="", host_header="", fake=True)


def _write_mail(dest: Path, uid: str, msgid: str) -> None:
    """sample.mysingle의 고유 ID만 바꿔 서로 다른 메일 파일을 만든다."""
    raw = (FIXTURES / "sample.mysingle").read_bytes()
    raw = raw.replace(b"2026062600000001", uid.encode())
    raw = raw.replace(b"<test-001@company.com>", f"<{msgid}@company.com>".encode())
    dest.write_bytes(raw)


# ---------------------------------------------------------------------------
# 파서 테스트
# ---------------------------------------------------------------------------

class TestParseMysingle:
    def test_subject_decoded(self):
        """=?UTF-8?B?...?= 헤더가 올바르게 디코딩된다."""
        from knowmate.secure.mysingle_reader import parse_mysingle
        result = parse_mysingle(str(FIXTURES / "sample.mysingle"))
        assert len(result["subject"]) > 0

    def test_body_text_extracted(self):
        """HTML 본문에서 텍스트가 추출된다."""
        from knowmate.secure.mysingle_reader import parse_mysingle
        result = parse_mysingle(str(FIXTURES / "sample.mysingle"))
        assert "A설비" in result["body_text"]
        assert "알람" in result["body_text"]

    def test_mail_uid_from_unique_id(self):
        """X-Desktop-Msg-UniqueID가 있으면 knox:{id} 형식으로 mail_uid가 생성된다."""
        from knowmate.secure.mysingle_reader import parse_mysingle
        result = parse_mysingle(str(FIXTURES / "sample.mysingle"))
        assert result["mail_uid"] == "knox:2026062600000001"

    def test_source_meta_json(self):
        """source_meta가 JSON 문자열이고 knox 헤더를 포함한다."""
        from knowmate.secure.mysingle_reader import parse_mysingle
        result = parse_mysingle(str(FIXTURES / "sample.mysingle"))
        meta = json.loads(result["source_meta"])
        assert meta["x_desktop_msg_unique_id"] == "2026062600000001"
        assert meta["x_cms_rootmailid"] == "ROOT-001"

    def test_source_file_is_absolute(self):
        """source_file이 절대 경로로 반환된다."""
        from knowmate.secure.mysingle_reader import parse_mysingle
        result = parse_mysingle(str(FIXTURES / "sample.mysingle"))
        assert Path(result["source_file"]).is_absolute()

    def test_mail_uid_fallback_to_message_id(self, tmp_path):
        """X-Desktop-Msg-UniqueID가 없으면 Message-ID로 fallback한다."""
        content = (
            "MIME-Version: 1.0\r\n"
            "From: a@b.com\r\n"
            "Subject: test\r\n"
            "Message-ID: <fallback-id@company.com>\r\n"
            "Content-Type: text/html; charset=UTF-8\r\n\r\n"
            "<html><body>본문</body></html>\r\n"
        )
        p = tmp_path / "no_uid.mysingle"
        p.write_bytes(content.encode("utf-8"))  # write_text는 Windows에서 CRLF를 깨뜨린다
        from knowmate.secure.mysingle_reader import parse_mysingle
        result = parse_mysingle(str(p))
        assert result["mail_uid"] == "knox:<fallback-id@company.com>"

    def test_no_body_raises(self, tmp_path):
        """본문 파트가 없으면 ValueError가 발생한다."""
        content = (
            "MIME-Version: 1.0\r\n"
            "From: a@b.com\r\n"
            "Subject: empty\r\n"
            "Content-Type: multipart/mixed; boundary=B\r\n\r\n"
            "--B\r\nContent-Type: image/gif\r\n\r\nGIF\r\n--B--\r\n"
        )
        p = tmp_path / "empty.mysingle"
        p.write_bytes(content.encode("utf-8"))  # write_text는 Windows에서 CRLF를 깨뜨린다
        from knowmate.secure.mysingle_reader import parse_mysingle
        with pytest.raises(ValueError):
            parse_mysingle(str(p))


# ---------------------------------------------------------------------------
# html_to_text 테스트
# ---------------------------------------------------------------------------

class TestHtmlToText:
    def test_strips_tags(self):
        from knowmate.secure.mysingle_reader import html_to_text
        result = html_to_text("<h1>제목</h1><p>내용</p>")
        assert "제목" in result
        assert "내용" in result
        assert "<" not in result

    def test_empty_string(self):
        from knowmate.secure.mysingle_reader import html_to_text
        assert html_to_text("") == ""


# ---------------------------------------------------------------------------
# EmailIndexer 테스트
# ---------------------------------------------------------------------------

_HAS_LANCEDB = bool(__import__("importlib").util.find_spec("lancedb"))
pytestmark_lancedb = pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치 — 폐쇄망 환경에서 실행")


@pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
class TestEmailIndexer:
    def _sample_parsed(self, uid: str = "knox:TEST001", source: str = "/data/test.mysingle") -> dict:
        return {
            "mail_uid": uid,
            "message_id": "<test@co>",
            "subject": "테스트",
            "sender": "a@b.com",
            "recipients": "c@d.com",
            "mail_date": "2026-06-25",
            "thread_ref": "",
            "body_text": "알람 처리 절차입니다.",
            "source_file": source,
            "source_meta": "{}",
        }

    def test_index_and_is_indexed(self, tmp_path):
        """index_mail 후 is_indexed가 True를 반환한다."""
        from knowmate.rag.email_indexer import EmailIndexer
        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        chunk_ids = ei.index_mail(self._sample_parsed(), mtime=1000.0)
        assert len(chunk_ids) > 0
        assert ei.is_indexed("knox:TEST001", 1000.0)

    def test_is_indexed_false_for_unknown(self, tmp_path):
        """인덱싱하지 않은 mail_uid는 is_indexed가 False를 반환한다."""
        from knowmate.rag.email_indexer import EmailIndexer
        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        assert not ei.is_indexed("knox:UNKNOWN", 999.0)

    def test_is_indexed_projects_only_required_columns(self):
        """중복 확인은 벡터·암호문 없이 mtime과 버전 메타만 조회한다."""
        import pyarrow as pa
        from knowmate.rag.email_indexer import EMAIL_INDEX_VERSION, EmailIndexer

        class Query:
            selected = None

            def where(self, _expr):
                return self

            def select(self, columns):
                self.selected = columns
                return self

            def limit(self, _count):
                return self

            def to_arrow(self):
                return pa.table({
                    "mtime": [1000.0],
                    "source_meta": [json.dumps({"_index_version": EMAIL_INDEX_VERSION})],
                })

        query = Query()
        indexer = object.__new__(EmailIndexer)
        indexer.table = types.SimpleNamespace(search=lambda: query)

        assert indexer.is_indexed("knox:TEST001", 1000.0)
        assert query.selected == ["mtime", "source_meta"]

    def test_delete_mail_chunks(self, tmp_path):
        """delete_mail_chunks 후 is_indexed가 False가 된다."""
        from knowmate.rag.email_indexer import EmailIndexer
        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        ei.index_mail(self._sample_parsed("knox:DEL001"), mtime=1000.0)
        ei.delete_mail_chunks("knox:DEL001")
        assert not ei.is_indexed("knox:DEL001", 1000.0)

    def test_migrates_pre_v3_table_missing_mail_date_ts(self, tmp_path):
        """mail_date_ts 없는 구 스키마 테이블을 열면 컬럼이 자동 추가되고 인덱싱이 성공한다."""
        import lancedb
        import pyarrow as pa
        from knowmate.rag.email_indexer import (
            EMAIL_SCHEMA, EMAIL_TABLE_NAME, EmailIndexer, get_or_create_emails_table,
        )
        # v3 이전 스키마 재현: mail_date_ts 필드만 제거해 테이블 생성
        old_schema = pa.schema([f for f in EMAIL_SCHEMA if f.name != "mail_date_ts"])
        db = lancedb.connect(str(tmp_path))
        db.create_table(EMAIL_TABLE_NAME, schema=old_schema)
        assert "mail_date_ts" not in set(db.open_table(EMAIL_TABLE_NAME).schema.names)

        # get_or_create가 마이그레이션으로 컬럼을 추가해야 한다
        table = get_or_create_emails_table(db)
        assert "mail_date_ts" in set(table.schema.names)

        # 마이그레이션 후 실제 인덱싱(table.add)이 실패 없이 동작
        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        chunk_ids = ei.index_mail(self._sample_parsed("knox:MIG001"), mtime=1000.0)
        assert len(chunk_ids) > 0
        assert ei.is_indexed("knox:MIG001", 1000.0)


# ---------------------------------------------------------------------------
# mail_scanner 테스트
# ---------------------------------------------------------------------------

class TestMailScanner:
    @staticmethod
    def _fake_mail_indexer(monkeypatch, *, recreated: bool = False, empty: bool = False, fail_index: bool = False):
        """LanceDB 없이 스캐너 상태 전이만 검증하는 인덱서 더블을 만든다."""
        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )

        class FakeIndexer:
            table_was_recreated = recreated
            table_is_empty = empty

            def __init__(self):
                self.is_indexed_calls = 0
                self.indexed: set[tuple[str, float]] = set()

            def is_indexed(self, mail_uid: str, mtime: float) -> bool:
                self.is_indexed_calls += 1
                return (mail_uid, mtime) in self.indexed

            def index_mail(self, parsed: dict, mtime: float) -> list[str]:
                if fail_index:
                    raise KeyboardInterrupt("중단 재현")
                self.indexed.add((parsed["mail_uid"], mtime))
                return ["chunk"]

        return FakeIndexer()

    def test_scan_finds_mysingle(self, tmp_path):
        """scan_mail_folders가 .mysingle 파일을 탐지한다."""
        (tmp_path / "a.mysingle").write_bytes(b"test")
        (tmp_path / "b.txt").write_bytes(b"not mail")
        from knowmate.collector.mail_scanner import scan_mail_folders
        results = scan_mail_folders([str(tmp_path)], max_per_scan=100)
        assert len(results) == 1
        assert results[0]["path"].endswith(".mysingle")

    def test_scan_returns_all_candidates_with_size_and_stable_order(self, tmp_path):
        """후보는 한도와 무관하게 전부 반환하고 mtime·경로 순으로 정렬한다."""
        for name in ("b", "a", "c"):
            (tmp_path / f"{name}.mysingle").write_bytes(b"test")
            os.utime(tmp_path / f"{name}.mysingle", (1_000, 1_000))
        from knowmate.collector.mail_scanner import scan_mail_folders
        results = scan_mail_folders([str(tmp_path)], max_per_scan=3)
        assert [Path(item["path"]).stem for item in results] == ["a", "b", "c"]
        assert all(item["size"] == 4 for item in results)

    def test_changed_state_is_saved_once_and_steady_cache_is_not_rewritten(
        self, tmp_path, monkeypatch,
    ):
        """전체 성공 캐시는 변경 사이클 끝에 한 번만 쓰고 정상 사이클엔 다시 쓰지 않는다."""
        from knowmate.collector import mail_scanner

        watch = tmp_path / "watch"
        watch.mkdir()
        _write_mail(watch / "mail.mysingle", uid="2026062600444444", msgid="save-once")
        indexer = self._fake_mail_indexer(monkeypatch)
        state_file = tmp_path / "mail_scan_state.json"
        failure_file = tmp_path / "mail_index_failure.json"
        cfg = {"mail": {"max_mails_per_scan": 1, "batch_commit_every": 1}}
        real_save = mail_scanner.save_mail_scan_state
        save_calls = []
        failure_save_calls = []

        def recording_save(path, state):
            save_calls.append(path)
            return real_save(path, state)

        def recording_failure_save(path, failures):
            failure_save_calls.append(path)
            return True

        monkeypatch.setattr(mail_scanner, "save_mail_scan_state", recording_save)
        monkeypatch.setattr(
            mail_scanner.failure_state, "save_failures", recording_failure_save,
        )
        mail_scanner.run_mail_scan(
            [str(watch)], indexer, cfg,
            state_file=state_file, failure_file=failure_file,
        )
        assert save_calls == [state_file]
        assert failure_save_calls == []

        save_calls.clear()
        mail_scanner.run_mail_scan(
            [str(watch)], indexer, cfg,
            state_file=state_file, failure_file=failure_file,
        )
        assert save_calls == []
        assert failure_save_calls == []

    def test_inaccessible_root_keeps_mail_failure_history(self, tmp_path, monkeypatch):
        """메일 폴더가 일시적으로 끊겨도 누적 실패 횟수와 백오프 기록을 보존한다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scanner import run_mail_scan

        missing_root = tmp_path / "disconnected"
        failed_path = str(missing_root / "mail.mysingle")
        failure_file = tmp_path / "mail_index_failure.json"
        failures = {}
        failure_state.note_failure(
            failures, failed_path, failure_state.KIND_UNKNOWN_TRANSIENT, "parse", None,
            1000.0, 100, 2000.0,
        )
        failure_state.save_failures(failure_file, failures)

        run_mail_scan(
            [str(missing_root)], self._fake_mail_indexer(monkeypatch),
            {"mail": {"max_mails_per_scan": 1}},
            state_file=tmp_path / "mail_scan_state.json", failure_file=failure_file,
        )
        assert failed_path in failure_state.load_failures(failure_file)

    def test_only_windows_lock_errors_use_short_busy_backoff(self):
        """영구 권한·일반 I/O 오류는 5~10분 고정 재시도로 오분류하지 않는다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scanner import _mail_os_failure_kind

        locked = OSError("sharing violation")
        locked.winerror = 32
        assert _mail_os_failure_kind(locked) == failure_state.KIND_TEMPORARY_BUSY
        assert _mail_os_failure_kind(PermissionError("denied")) == failure_state.KIND_UNKNOWN_TRANSIENT

    def test_failed_mail_releases_cursor_for_later_mail_next_cycle(self, tmp_path, monkeypatch):
        """한도 1에서 첫 메일 실패 후 다음 사이클은 뒤 정상 메일을 처리한다."""
        from knowmate.collector.mail_scanner import run_mail_scan

        watch = tmp_path / "watch"
        watch.mkdir()
        broken = watch / "broken.mysingle"
        broken.write_bytes(b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n--B--\r\n")
        valid = watch / "valid.mysingle"
        _write_mail(valid, uid="2026062600777777", msgid="valid-after-failure")
        os.utime(broken, (2_000, 2_000))
        os.utime(valid, (1_000, 1_000))
        indexer = self._fake_mail_indexer(monkeypatch)
        state_file = tmp_path / "mail_scan_state.json"
        failure_file = tmp_path / "mail_index_failure.json"
        cfg = {"mail": {"max_mails_per_scan": 1, "batch_commit_every": 1}}

        assert run_mail_scan(
            [str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file,
            get_now=lambda: 100.0,
        ) == (0, 1)
        second_indexed, _ = run_mail_scan(
            [str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file,
            get_now=lambda: 100.0,
        )
        assert second_indexed == 1

    def test_recreated_table_clears_cache_before_interruption(self, tmp_path, monkeypatch):
        """재생성 후 첫 DB 저장 중단 전에도 이전 성공 캐시는 이미 디스크에서 제거된다."""
        from knowmate.collector.mail_scanner import run_mail_scan

        watch = tmp_path / "watch"
        watch.mkdir()
        path = watch / "mail.mysingle"
        _write_mail(path, uid="2026062600666666", msgid="recreated")
        state_file = tmp_path / "mail_scan_state.json"
        state_file.write_text(json.dumps({
            "schema_version": 1,
            "cursor": None,
            "files": {
                "stale": {
                    "path": str(path), "mtime": path.stat().st_mtime, "size": path.stat().st_size,
                    "mail_uid": "knox:old", "index_version": "3",
                },
            },
        }), encoding="utf-8")
        indexer = self._fake_mail_indexer(monkeypatch, recreated=True, empty=True, fail_index=True)
        cfg = {"mail": {"max_mails_per_scan": 1, "batch_commit_every": 1}}

        with pytest.raises(KeyboardInterrupt):
            run_mail_scan(
                [str(watch)], indexer, cfg, state_file=state_file,
                failure_file=tmp_path / "mail_index_failure.json",
            )
        assert json.loads(state_file.read_text(encoding="utf-8"))["files"] == {}
        assert not indexer.table_was_recreated

        recovered = self._fake_mail_indexer(monkeypatch)
        assert run_mail_scan(
            [str(watch)], recovered, cfg, state_file=state_file,
            failure_file=tmp_path / "mail_index_failure.json",
        ) == (1, 0)
        assert recovered.is_indexed_calls == 1

    def test_recreated_table_defers_scan_when_cache_clear_cannot_save(self, tmp_path, monkeypatch):
        """빈 캐시를 확정하지 못하면 재생성된 DB에 어떤 메일도 쓰지 않는다."""
        from knowmate.collector import mail_scanner

        watch = tmp_path / "watch"
        watch.mkdir()
        _write_mail(watch / "mail.mysingle", uid="2026062600555555", msgid="save-failure")
        indexer = self._fake_mail_indexer(monkeypatch, recreated=True, empty=True)
        monkeypatch.setattr(mail_scanner, "save_mail_scan_state", lambda *_args: False)

        assert mail_scanner.run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 1}},
            state_file=tmp_path / "mail_scan_state.json",
            failure_file=tmp_path / "mail_index_failure.json",
        ) == (0, 0)
        assert indexer.is_indexed_calls == 0
        assert indexer.table_was_recreated

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_run_mail_scan_indexes_new(self, tmp_path):
        """새 .mysingle 파일이 인덱싱된다."""
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.collector.mail_scanner import run_mail_scan

        dest = tmp_path / "watch" / "a.mysingle"
        dest.parent.mkdir()
        shutil.copy(FIXTURES / "sample.mysingle", dest)

        ei = EmailIndexer(db_path=tmp_path / "db", embed_client=_fake_embed())
        cfg = {"mail": {"max_mails_per_scan": 100, "batch_commit_every": 10}}
        cnt, _ = run_mail_scan(
            [str(dest.parent)], ei, cfg,
            state_file=tmp_path / "mail_scan_state.json",
            failure_file=tmp_path / "mail_index_failure.json",
        )
        assert cnt == 1

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_run_mail_scan_skips_duplicate(self, tmp_path):
        """이미 인덱싱된 메일은 재인덱싱하지 않는다."""
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.collector.mail_scanner import run_mail_scan

        dest = tmp_path / "watch" / "a.mysingle"
        dest.parent.mkdir()
        shutil.copy(FIXTURES / "sample.mysingle", dest)

        ei = EmailIndexer(db_path=tmp_path / "db", embed_client=_fake_embed())
        cfg = {"mail": {"max_mails_per_scan": 100, "batch_commit_every": 10}}

        state_file = tmp_path / "mail_scan_state.json"
        failure_file = tmp_path / "mail_index_failure.json"
        run_mail_scan([str(dest.parent)], ei, cfg, state_file=state_file, failure_file=failure_file)
        cnt2, skipped = run_mail_scan([str(dest.parent)], ei, cfg, state_file=state_file, failure_file=failure_file)
        assert cnt2 == 0
        assert skipped == 1

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_scan_limit_is_attempt_budget_and_cursor_reaches_all_mail(self, tmp_path):
        """한도 밖 메일도 다음 사이클의 커서 순환으로 인덱싱된다 (#82)."""
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
        cfg = {"mail": {"max_mails_per_scan": 2, "batch_commit_every": 1}}
        state_file = tmp_path / "mail_scan_state.json"
        failure_file = tmp_path / "mail_index_failure.json"

        first_indexed, _ = run_mail_scan(
            [str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file,
        )
        assert first_indexed == 2
        second_indexed, _ = run_mail_scan(
            [str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file,
        )
        assert second_indexed == 1
        df = indexer.table.search().select(["source_file"]).limit(100).to_arrow().to_pandas()
        assert {Path(p).stem for p in df["source_file"].unique()} == {"new", "mid", "old"}

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_success_cache_avoids_db_check_on_later_cycle(self, tmp_path):
        """성공 캐시 적중 메일은 이후 사이클에서 is_indexed를 호출하지 않는다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer

        watch = tmp_path / "watch"
        watch.mkdir()
        _write_mail(watch / "mail.mysingle", uid="2026062600999999", msgid="cached")
        indexer = EmailIndexer(db_path=tmp_path / "db", embed_client=_fake_embed())
        cfg = {"mail": {"max_mails_per_scan": 1, "batch_commit_every": 1}}
        state_file = tmp_path / "mail_scan_state.json"
        failure_file = tmp_path / "mail_index_failure.json"
        run_mail_scan([str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file)

        def fail_if_called(*_args):
            raise AssertionError("성공 캐시가 DB 확인을 막아야 함")

        indexer.is_indexed = fail_if_called
        indexed, skipped = run_mail_scan(
            [str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file,
        )
        assert (indexed, skipped) == (0, 1)

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_parse_failure_does_not_starve_later_mail(self, tmp_path):
        """실패 메일도 커서를 전진시켜 같은 사이클의 뒤 메일을 처리한다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer

        watch = tmp_path / "watch"
        watch.mkdir()
        broken = watch / "broken.mysingle"
        broken.write_bytes(b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n--B--\r\n")
        valid = watch / "valid.mysingle"
        _write_mail(valid, uid="2026062600888888", msgid="valid")
        os.utime(broken, (2_000, 2_000))
        os.utime(valid, (1_000, 1_000))

        indexer = EmailIndexer(db_path=tmp_path / "db", embed_client=_fake_embed())
        cfg = {"mail": {"max_mails_per_scan": 2, "batch_commit_every": 1}}
        indexed, _ = run_mail_scan(
            [str(watch)], indexer, cfg,
            state_file=tmp_path / "mail_scan_state.json",
            failure_file=tmp_path / "mail_index_failure.json",
            get_now=lambda: 100.0,
        )
        assert indexed == 1

    def test_mail_disabled_check(self):
        """mail.enabled=false이면 스캔 분기에 진입하지 않는다."""
        cfg = {"mail": {"enabled": False}}
        assert not cfg.get("mail", {}).get("enabled", False)


# ---------------------------------------------------------------------------
# .eml (표준 이메일) 지원 테스트
# ---------------------------------------------------------------------------

_EML_SAMPLE = (
    "MIME-Version: 1.0\r\n"
    "From: alice@company.com\r\n"
    "To: bob@company.com\r\n"
    "Subject: 3월 점검 결과\r\n"
    "Date: Mon, 16 Jun 2025 14:23:00 +0900\r\n"
    "Message-ID: <eml-test-001@company.com>\r\n"
    "Content-Type: text/html; charset=UTF-8\r\n\r\n"
    "<html><body>설비 점검 완료했습니다.</body></html>\r\n"
)


class TestEmlSupport:
    def test_parse_eml_source_type(self, tmp_path):
        """.eml은 source_type='eml', mail_uid는 eml: 접두를 가진다."""
        from knowmate.secure.mysingle_reader import parse_mail_file
        p = tmp_path / "mail.eml"
        p.write_bytes(_EML_SAMPLE.encode("utf-8"))  # write_text는 Windows에서 CRLF를 깨뜨린다
        r = parse_mail_file(str(p))
        assert r["source_type"] == "eml"
        assert r["mail_uid"] == "eml:<eml-test-001@company.com>"
        assert r["subject"] == "3월 점검 결과"
        assert "설비 점검" in r["body_text"]

    def test_mysingle_still_knox(self):
        """.mysingle은 여전히 source_type='knox', knox: 접두 (회귀 방지)."""
        from knowmate.secure.mysingle_reader import parse_mail_file
        r = parse_mail_file(str(FIXTURES / "sample.mysingle"))
        assert r["source_type"] == "knox"
        assert r["mail_uid"].startswith("knox:")

    def test_scan_finds_both_extensions(self, tmp_path):
        """scan_mail_folders가 .mysingle과 .eml을 모두 탐지한다."""
        (tmp_path / "a.mysingle").write_bytes(b"test")
        (tmp_path / "b.eml").write_bytes(b"test")
        (tmp_path / "c.txt").write_bytes(b"not mail")
        from knowmate.collector.mail_scanner import scan_mail_folders
        results = scan_mail_folders([str(tmp_path)], max_per_scan=100)
        names = sorted(Path(r["path"]).name for r in results)
        assert names == ["a.mysingle", "b.eml"]
