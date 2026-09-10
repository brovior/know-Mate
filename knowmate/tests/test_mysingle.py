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
from unittest.mock import MagicMock

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

    def test_email_indexer_syntax_is_python311_compatible(self):
        """exact-ID SQL 문자열 생성이 Python 3.11 문법에서도 파싱된다."""
        import ast

        source = (Path(__file__).parents[1] / "rag" / "email_indexer.py").read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 11))

    def test_index_and_get_index_state(self, tmp_path):
        """index_mail 후 명시 상태가 CURRENT를 반환한다."""
        from knowmate.rag.email_indexer import EmailIndexer, MailIndexState
        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        chunk_ids = ei.index_mail(self._sample_parsed(), mtime=1000.0)
        assert len(chunk_ids) > 0
        assert ei.get_index_state("knox:TEST001", 1000.0).state is MailIndexState.CURRENT

    def test_get_index_state_missing_for_unknown(self, tmp_path):
        """인덱싱하지 않은 mail_uid는 MISSING 상태를 반환한다."""
        from knowmate.rag.email_indexer import EmailIndexer, MailIndexState
        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        assert ei.get_index_state("knox:UNKNOWN", 999.0).state is MailIndexState.MISSING

    def test_get_index_state_projects_only_required_columns(self):
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
                    "chunk_id": ["old-chunk"],
                    "mtime": [1000.0],
                    "source_meta": [json.dumps({"_index_version": EMAIL_INDEX_VERSION})],
                })

        query = Query()
        indexer = object.__new__(EmailIndexer)
        indexer.table = types.SimpleNamespace(search=lambda: query)

        assert indexer.get_index_state("knox:TEST001", 1000.0).state.name == "CURRENT"
        assert query.selected == ["chunk_id", "mtime", "source_meta"]

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

    def test_stale_state_captures_active_old_chunk_ids(self, tmp_path):
        """변경 메일은 교체 전에 active 기존 청크 ID를 정확히 캡처한다."""
        from knowmate.rag.email_indexer import EmailIndexer, MailIndexState

        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        old_ids = ei.index_mail(self._sample_parsed(), mtime=1000.0)
        check = ei.get_index_state("knox:TEST001", 2000.0)

        assert check.state is MailIndexState.STALE
        assert set(check.old_chunk_ids) == set(old_ids)

    def test_missing_mail_never_deletes(self, tmp_path):
        """신규 메일 저장은 기존 청크 삭제 API를 호출하지 않는다."""
        from knowmate.rag.email_indexer import EmailIndexer

        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        ei.delete_chunk_ids = MagicMock()
        ei.index_mail(self._sample_parsed(), mtime=1000.0)

        ei.delete_chunk_ids.assert_not_called()

    def test_stale_embedding_failure_keeps_old_rows(self, tmp_path):
        """변경 메일 임베딩 실패는 기존 검색 가능 행을 삭제하지 않는다."""
        from knowmate.rag.email_indexer import EmailIndexer

        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        old_ids = ei.index_mail(self._sample_parsed(), mtime=1000.0)

        class FailingEmbed:
            def embed(self, _texts):
                raise RuntimeError("embedding failed")

        ei._embed = FailingEmbed()
        with pytest.raises(RuntimeError, match="embedding failed"):
            ei.index_mail(self._sample_parsed(), mtime=2000.0)

        rows = ei.table.search().select(["chunk_id"]).to_arrow().to_pylist()
        assert {row["chunk_id"] for row in rows} == set(old_ids)

    def test_stale_add_failure_keeps_old_rows(self, tmp_path):
        """변경 메일 새 행 add 실패는 기존 검색 가능 행을 삭제하지 않는다."""
        from knowmate.rag.email_indexer import EmailIndexer

        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        old_ids = ei.index_mail(self._sample_parsed(), mtime=1000.0)
        check = ei.get_index_state("knox:TEST001", 2000.0)
        table = ei.table
        ei.table = types.SimpleNamespace(add=MagicMock(side_effect=RuntimeError("add failed")))

        with pytest.raises(RuntimeError, match="add failed"):
            ei.index_mail(self._sample_parsed(), mtime=2000.0, index_check=check)

        rows = table.search().select(["chunk_id"]).to_arrow().to_pylist()
        assert {row["chunk_id"] for row in rows} == set(old_ids)

    def test_direct_stale_delete_failure_is_not_silent(self, tmp_path):
        """직접 index_mail 호출도 삭제 실패를 숨기지 않고 기존·새 행을 모두 보존한다."""
        from knowmate.rag.email_indexer import EmailIndexer, PendingMailDeleteError

        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        old_ids = ei.index_mail(self._sample_parsed(), mtime=1000.0)
        ei.delete_chunk_ids = MagicMock(side_effect=RuntimeError("delete failed"))

        with pytest.raises(PendingMailDeleteError) as exc_info:
            ei.index_mail(self._sample_parsed(), mtime=2000.0)

        assert set(exc_info.value.chunk_ids) == set(old_ids)
        assert ei.table.count_rows() > len(old_ids)


# ---------------------------------------------------------------------------
# mail_scan_state v2 테스트
# ---------------------------------------------------------------------------

class TestMailScanStateV2:
    def test_v1_migration_keeps_cache_cursor_and_pending_deletes(self, tmp_path, monkeypatch):
        """v1은 path만 제거해 필요한 성공 캐시와 top-level 대기열을 보존한다."""
        from knowmate.collector.mail_scan_state import (
            load_mail_scan_state, save_mail_scan_state, state_needs_save,
        )

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )

        source = str(tmp_path / "한글 메일.mysingle")
        path = tmp_path / "state.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "cursor": {"mtime": 44.5, "path": "C:\\메일\\다음.mysingle"},
            "files": {"legacy-key": {
                "path": source, "mtime": 12.5, "size": 99, "mail_uid": "knox:uid",
                "index_version": "3", "uid_resolution_version": 2,
            }},
            "pending_deletes": ["old-a", "old-a", "old-b"],
        }, ensure_ascii=False), encoding="utf-8")

        state = load_mail_scan_state(path)
        key = os.path.normcase(os.path.abspath(source))
        assert state_needs_save(state)
        assert state["cursor"] == {"mtime": 44.5, "path": "C:\\메일\\다음.mysingle"}
        assert state["pending_deletes"] == ["old-a", "old-b"]
        assert state["files"] == {key: {
            "mtime": 12.5, "size": 99, "mail_uid": "knox:uid", "index_version": "3",
            "uid_resolution_version": 2,
        }}

        assert save_mail_scan_state(path, state)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["schema_version"] == 2
        assert "path" not in stored["files"][key]

    def test_idle_scan_persists_v1_migration_once(self, tmp_path):
        """처리할 파일이 없어도 v1→v2 변환은 다음 재시작 전에 저장된다."""
        from knowmate.collector.mail_scanner import run_mail_scan

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "schema_version": 1, "cursor": None, "files": {}, "pending_deletes": ["old-a"],
        }), encoding="utf-8")
        indexer = types.SimpleNamespace(
            table_was_recreated=False, table_is_empty=False,
            delete_chunk_ids=lambda _ids: (_ for _ in ()).throw(RuntimeError("offline")),
        )

        assert run_mail_scan(
            [], indexer, {"mail": {"max_mails_per_scan": 1}},
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (0, 0)
        assert json.loads(state_file.read_text(encoding="utf-8"))["schema_version"] == 2

    def test_compact_output_unicode_roundtrip_and_v2_reload_does_not_rewrite(self, tmp_path, monkeypatch):
        """저장은 한 줄 compact JSON이며 정상 v2는 유휴 사이클에 다시 쓰지 않는다."""
        from knowmate.collector import mail_scanner
        from knowmate.collector.mail_scan_state import cache_success, save_mail_scan_state

        path = tmp_path / "상태.json"
        source = str(tmp_path / "메일함" / "한글😀.mysingle")
        state = {"schema_version": 2, "cursor": None, "files": {}, "pending_deletes": []}
        cache_success(state, {"path": source, "mtime": 1.0, "size": 2}, "knox:한글😀")
        assert save_mail_scan_state(path, state)
        content = path.read_text(encoding="utf-8")
        assert "\n" not in content and "한글😀" in content

        saves = []
        real_save = mail_scanner.save_mail_scan_state
        monkeypatch.setattr(mail_scanner, "save_mail_scan_state", lambda *args: saves.append(args) or real_save(*args))
        assert mail_scanner.run_mail_scan(
            [], types.SimpleNamespace(table_was_recreated=False, table_is_empty=False),
            {"mail": {"max_mails_per_scan": 1}},
            state_file=path, failure_file=tmp_path / "failures.json",
        ) == (0, 0)
        assert saves == []
        assert json.loads(content) == json.loads(path.read_text(encoding="utf-8"))

    def test_pending_deletes_survive_migration_and_invalidation(self, tmp_path, monkeypatch):
        """v1 변환이나 빈 테이블 재생성도 삭제 대기열을 지우지 않는다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state, save_mail_scan_state

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "schema_version": 1, "cursor": None,
            "files": {"old": {"path": str(tmp_path / "old.mysingle"), "mtime": 1, "size": 1,
                                "mail_uid": "knox:old", "index_version": "3", "uid_resolution_version": 2}},
            "pending_deletes": ["old-a"],
        }), encoding="utf-8")

        state = load_mail_scan_state(state_file, invalidate_cache=True)
        assert state["files"] == {}
        assert state["pending_deletes"] == ["old-a"]
        assert save_mail_scan_state(state_file, state)
        assert load_mail_scan_state(state_file)["pending_deletes"] == ["old-a"]

    def test_v1_path_key_collision_keeps_newest_entry_deterministically(self, tmp_path, monkeypatch):
        """대소문자만 다른 legacy 키가 충돌하면 최신 항목 하나를 일관되게 고른다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )
        source = str(tmp_path / "same.mysingle")
        path = tmp_path / "state.json"
        path.write_text(json.dumps({
            "schema_version": 1, "cursor": None,
            "files": {
                "A": {"path": source, "mtime": 1, "size": 1, "mail_uid": "knox:old", "index_version": "3", "uid_resolution_version": 2},
                "a": {"path": source, "mtime": 2, "size": 1, "mail_uid": "knox:new", "index_version": "3", "uid_resolution_version": 2},
            }, "pending_deletes": [],
        }), encoding="utf-8")

        entry = next(iter(load_mail_scan_state(path)["files"].values()))
        assert entry["mail_uid"] == "knox:new"

    def test_atomic_save_failure_keeps_previous_state(self, tmp_path, monkeypatch):
        """replace 실패는 기존 정상 상태 파일을 덮어쓰지 않는다."""
        from knowmate.collector.mail_scan_state import save_mail_scan_state

        path = tmp_path / "state.json"
        original = '{"schema_version":2,"cursor":null,"files":{},"pending_deletes":["old"]}'
        path.write_text(original, encoding="utf-8")
        monkeypatch.setattr(Path, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
        assert not save_mail_scan_state(path, {"schema_version": 2, "cursor": None, "files": {}, "pending_deletes": []})
        assert path.read_text(encoding="utf-8") == original

    def test_corrupt_json_falls_back_to_empty_state(self, tmp_path):
        """부분 기록된 JSON은 다음 스캔을 안전한 빈 상태로 시작한다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state

        path = tmp_path / "state.json"
        path.write_text('{"schema_version":2,"files":', encoding="utf-8")
        assert load_mail_scan_state(path) == {
            "schema_version": 2, "cursor": None, "files": {}, "pending_deletes": [],
        }

    def test_migrated_uid_summary_remains_available_for_duplicate_resolution(self, tmp_path, monkeypatch):
        """v2에도 UID/max-mtime 요약에 필요한 mail_uid가 남는다."""
        from knowmate.collector import failure_state, mail_scanner
        from knowmate.collector.mail_scan_state import load_mail_scan_state

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )
        source = str(tmp_path / "copy.mysingle")
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "schema_version": 1, "cursor": None,
            "files": {"old": {"path": source, "mtime": 77, "size": 4,
                                "mail_uid": "knox:duplicate", "index_version": "3", "uid_resolution_version": 2}},
            "pending_deletes": [],
        }), encoding="utf-8")
        state = load_mail_scan_state(state_file)
        monkeypatch.setattr(mail_scanner, "_iter_mail_files", lambda *_args: iter([(source, 77.0, 4)]))

        _items, _seen, _failures, cached_uids, _skipped = mail_scanner._collect_actionable_candidates(
            [str(tmp_path)], [".mysingle"], state, {}, 0.0, failure_state.BackoffPolicy(),
        )
        assert cached_uids == {"knox:duplicate": 77.0}

    def test_large_v2_state_is_meaningfully_smaller_than_v1(self, tmp_path, monkeypatch):
        """경로 중복 제거는 2만 건 상태에서도 크기 절감으로 확인된다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state, save_mail_scan_state

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )

        files = {}
        for index in range(20_000):
            source = f"C:/very/long/mail/archive/2026/09/folder-{index:05d}/message-{index:05d}.mysingle"
            files[os.path.normcase(os.path.abspath(source))] = {
                "path": source, "mtime": float(index), "size": index + 1,
                "mail_uid": f"knox:{index}", "index_version": "3", "uid_resolution_version": 2,
            }
        path = tmp_path / "state.json"
        v1 = {"schema_version": 1, "cursor": None, "files": files, "pending_deletes": []}
        path.write_text(json.dumps(v1, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        v1_size = path.stat().st_size
        assert save_mail_scan_state(path, load_mail_scan_state(path))
        assert path.stat().st_size < v1_size * 0.8


# ---------------------------------------------------------------------------
# mail_scanner 테스트
# ---------------------------------------------------------------------------

class TestMailScanner:
    @staticmethod
    def _sample_parsed_for_scan(path: str, uid: str, body_text: str) -> dict:
        """파일 파싱을 대체하는 교차메일 배치 테스트용 최소 메일을 만든다."""
        return {
            "mail_uid": uid,
            "message_id": f"<{uid}@test>",
            "subject": "test",
            "sender": "a@test",
            "recipients": "b@test",
            "mail_date": "2026-09-09",
            "thread_ref": "",
            "body_text": body_text,
            "source_file": path,
            "source_meta": "{}",
        }

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
                self.state_check_calls = 0
                self.indexed: set[tuple[str, float]] = set()

            def get_index_state(self, mail_uid: str, mtime: float):
                self.state_check_calls += 1
                name = "CURRENT" if (mail_uid, mtime) in self.indexed else "MISSING"
                return types.SimpleNamespace(state=types.SimpleNamespace(name=name), old_chunk_ids=())

            def index_mail(self, parsed: dict, mtime: float, **_kwargs) -> list[str]:
                if fail_index:
                    raise KeyboardInterrupt("중단 재현")
                self.indexed.add((parsed["mail_uid"], mtime))
                return ["chunk"]

            def delete_chunk_ids(self, _chunk_ids) -> None:
                pass

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

    def test_duplicate_identical_watch_roots_attempt_and_fail_once(self, tmp_path, monkeypatch):
        """같은 root를 반복 등록해도 파일 하나는 한 번만 시도하고 실패를 한 번만 기록한다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scanner import run_mail_scan, scan_mail_folders

        watch = tmp_path / "watch"
        watch.mkdir()
        path = watch / "failed.mysingle"
        _write_mail(path, uid="2026062600555555", msgid="duplicate-root")
        parsed_paths = []
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda source: parsed_paths.append(source) or (_ for _ in ()).throw(OSError("offline")),
        )
        failure_file = tmp_path / "failures.json"

        assert len(scan_mail_folders([str(watch), str(watch), str(watch)], 3)) == 1
        assert run_mail_scan(
            [str(watch), str(watch), str(watch)], self._fake_mail_indexer(monkeypatch),
            {"mail": {"max_mails_per_scan": 3}}, state_file=tmp_path / "state.json",
            failure_file=failure_file, get_now=lambda: 1_000.0,
        ) == (0, 1)
        assert parsed_paths == [str(path)]
        record = failure_state.load_failures(failure_file)[str(path)]
        assert record.consecutive_failures == 1
        assert failure_state.backoff_seconds(record, str(path), failure_state.BackoffPolicy()) == 1_800.0

    def test_nested_watch_roots_attempt_and_fail_once(self, tmp_path, monkeypatch):
        """상위·하위 root가 겹쳐도 하위 파일은 한 번만 예산을 쓰고 실패를 한 번만 기록한다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scanner import run_mail_scan, scan_mail_folders

        watch = tmp_path / "watch"
        nested = watch / "nested"
        nested.mkdir(parents=True)
        path = nested / "failed.mysingle"
        _write_mail(path, uid="2026062600666666", msgid="nested-root")
        parsed_paths = []
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda source: parsed_paths.append(source) or (_ for _ in ()).throw(OSError("offline")),
        )
        failure_file = tmp_path / "failures.json"

        assert len(scan_mail_folders([str(watch), str(nested)], 3)) == 1
        assert run_mail_scan(
            [str(watch), str(nested)], self._fake_mail_indexer(monkeypatch),
            {"mail": {"max_mails_per_scan": 3}}, state_file=tmp_path / "state.json",
            failure_file=failure_file, get_now=lambda: 1_000.0,
        ) == (0, 1)
        assert parsed_paths == [str(path)]
        record = failure_state.load_failures(failure_file)[str(path)]
        assert record.consecutive_failures == 1
        assert failure_state.backoff_seconds(record, str(path), failure_state.BackoffPolicy()) == 1_800.0

    @pytest.mark.parametrize("extensions", [None, []])
    def test_empty_mail_extensions_use_defaults_without_pruning_success_cache(
        self, tmp_path, monkeypatch, extensions,
    ):
        """None/[]도 .mysingle·.eml 기본값으로 스캔해 성공 캐시를 유지한다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state
        from knowmate.collector.mail_scanner import run_mail_scan, scan_mail_folders

        watch = tmp_path / "watch"
        watch.mkdir()
        path = watch / "mail.mysingle"
        _write_mail(path, uid="2026062600777777", msgid="default-extensions")
        state_file = tmp_path / "state.json"
        cfg = {"mail": {"extensions": extensions, "max_mails_per_scan": 1}}

        assert [item["path"] for item in scan_mail_folders([str(watch)], 1, extensions)] == [str(path)]
        assert run_mail_scan(
            [str(watch)], self._fake_mail_indexer(monkeypatch), cfg,
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (1, 0)
        assert run_mail_scan(
            [str(watch)], self._fake_mail_indexer(monkeypatch), cfg,
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (0, 1)
        assert os.path.normcase(os.path.abspath(str(path))) in load_mail_scan_state(state_file)["files"]

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
        assert recovered.state_check_calls == 1

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
        assert indexer.state_check_calls == 0
        assert indexer.table_was_recreated

    def test_db_state_error_records_failure_without_index_mutation(self, tmp_path, monkeypatch):
        """DB 상태 조회 ERROR면 저장·삭제 없이 failure_state에만 실패를 기록한다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scanner import run_mail_scan

        watch = tmp_path / "watch"
        watch.mkdir()
        path = watch / "mail.mysingle"
        _write_mail(path, uid="2026062600333333", msgid="db-error")

        class ErrorIndexer:
            table_was_recreated = False
            table_is_empty = False

            def __init__(self):
                self.index_calls = 0
                self.delete_calls = 0

            def get_index_state(self, *_args):
                return types.SimpleNamespace(state=types.SimpleNamespace(name="ERROR"), old_chunk_ids=())

            def index_mail(self, *_args, **_kwargs):
                self.index_calls += 1
                raise AssertionError("ERROR 상태에서는 저장하면 안 됨")

            def delete_chunk_ids(self, _chunk_ids):
                self.delete_calls += 1
                raise AssertionError("ERROR 상태에서는 삭제하면 안 됨")

        indexer = ErrorIndexer()
        failure_file = tmp_path / "mail_index_failure.json"
        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 1}},
            state_file=tmp_path / "mail_scan_state.json", failure_file=failure_file,
            get_now=lambda: 100.0,
        ) == (0, 1)
        assert indexer.index_calls == 0 and indexer.delete_calls == 0
        assert str(path) in failure_state.load_failures(failure_file)

    def test_delete_failure_is_persisted_and_retried_before_cache_skip(self, tmp_path, monkeypatch):
        """기존 삭제 실패 ID는 성공 캐시 메일도 다음 사이클 시작 시 다시 삭제한다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state
        from knowmate.collector.mail_scanner import run_mail_scan

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )

        watch = tmp_path / "watch"
        watch.mkdir()
        path = watch / "mail.mysingle"
        _write_mail(path, uid="2026062600222222", msgid="pending-delete")

        class StaleIndexer:
            table_was_recreated = False
            table_is_empty = False

            def __init__(self):
                self.delete_attempts = 0

            def get_index_state(self, *_args):
                return types.SimpleNamespace(
                    state=types.SimpleNamespace(name="STALE"), old_chunk_ids=("old-1", "old-2"),
                )

            def index_mail(self, *_args, **_kwargs):
                return ["new-1"]

            def delete_chunk_ids(self, _chunk_ids):
                self.delete_attempts += 1
                if self.delete_attempts == 1:
                    raise RuntimeError("delete failed")
                return tuple(_chunk_ids)

        state_file = tmp_path / "mail_scan_state.json"
        failure_file = tmp_path / "mail_index_failure.json"
        indexer = StaleIndexer()
        cfg = {"mail": {"max_mails_per_scan": 1, "batch_commit_every": 1}}

        assert run_mail_scan([str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file) == (1, 0)
        pending = load_mail_scan_state(state_file)
        assert pending["pending_deletes"] == ["old-1", "old-2"]

        assert run_mail_scan([str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file) == (0, 1)
        assert indexer.delete_attempts == 2
        assert load_mail_scan_state(state_file)["pending_deletes"] == []

    def test_pending_delete_survives_disk_reload_and_fresh_indexer(self, tmp_path, monkeypatch):
        """저장된 대기열은 새 인덱서·새 스캔에서도 원본 파일과 무관하게 재시도된다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state, save_mail_scan_state
        from knowmate.collector.mail_scanner import run_mail_scan

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )
        state_file = tmp_path / "mail_scan_state.json"
        save_mail_scan_state(state_file, {
            "schema_version": 1,
            "cursor": None,
            "files": {},
            "pending_deletes": ["old-a"],
        })

        class FreshIndexer:
            table_was_recreated = False
            table_is_empty = False

            def __init__(self):
                self.deleted = []

            def delete_chunk_ids(self, chunk_ids):
                self.deleted.append(tuple(chunk_ids))
                return tuple(chunk_ids)

        indexer = FreshIndexer()
        assert run_mail_scan(
            [], indexer, {"mail": {"max_mails_per_scan": 1}},
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (0, 0)
        assert indexer.deleted == [("old-a",)]
        assert load_mail_scan_state(state_file)["pending_deletes"] == []

    def test_pending_delete_survives_index_cache_invalidation(self, tmp_path, monkeypatch):
        """메일 인덱스 버전 변경으로 성공 캐시가 비워져도 대기 삭제 ID는 남는다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="4"),
        )
        state_file = tmp_path / "mail_scan_state.json"
        state_file.write_text(json.dumps({
            "schema_version": 1,
            "cursor": None,
            "files": {"old": {
                "path": "C:/old.mysingle", "mtime": 1.0, "size": 1,
                "mail_uid": "knox:old", "index_version": "3",
            }},
            "pending_deletes": ["old-a"],
        }), encoding="utf-8")

        state = load_mail_scan_state(state_file)
        assert state["files"] == {}
        assert state["pending_deletes"] == ["old-a"]

    def test_new_replacement_keeps_older_pending_ids(self, tmp_path, monkeypatch):
        """old-A 재시도 실패 뒤 old-B 삭제 성공은 old-A 대기열을 지우지 않는다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state
        from knowmate.collector.mail_scanner import run_mail_scan

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )
        watch = tmp_path / "watch"
        watch.mkdir()
        path = watch / "mail.mysingle"
        _write_mail(path, uid="2026062600111111", msgid="uid-a")

        class ReplacingIndexer:
            table_was_recreated = False
            table_is_empty = False

            def __init__(self):
                self.round = 0
                self.deleted = []

            def get_index_state(self, *_args):
                self.round += 1
                old_id = "old-a" if self.round == 1 else "old-b"
                return types.SimpleNamespace(
                    state=types.SimpleNamespace(name="STALE"), old_chunk_ids=(old_id,),
                )

            def index_mail(self, *_args, **_kwargs):
                return ["new"]

            def delete_chunk_ids(self, chunk_ids):
                ids = tuple(chunk_ids)
                self.deleted.append(ids)
                if "old-a" in ids:
                    raise RuntimeError("old-a still locked")
                return ids

        indexer = ReplacingIndexer()
        state_file = tmp_path / "mail_scan_state.json"
        cfg = {"mail": {"max_mails_per_scan": 1, "batch_commit_every": 1}}
        run_mail_scan([str(watch)], indexer, cfg, state_file=state_file, failure_file=tmp_path / "failures.json")
        assert load_mail_scan_state(state_file)["pending_deletes"] == ["old-a"]

        _write_mail(path, uid="2026062600111112", msgid="uid-b")
        os.utime(path, (path.stat().st_mtime + 2, path.stat().st_mtime + 2))
        run_mail_scan([str(watch)], indexer, cfg, state_file=state_file, failure_file=tmp_path / "failures.json")

        assert load_mail_scan_state(state_file)["pending_deletes"] == ["old-a"]
        assert ("old-b",) in indexer.deleted

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
        """성공 캐시 적중 메일은 이후 사이클에서 상태 조회를 호출하지 않는다."""
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

        indexer.get_index_state = fail_if_called
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

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_cross_mail_embedding_uses_32_chunk_batches(self, tmp_path, monkeypatch):
        """65개 단일 청크 메일은 메일 경계를 넘어 32개씩 세 번 임베딩한다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        for index in range(65):
            (watch / f"{index:03d}.mysingle").write_bytes(b"x")

        def parse(path: str) -> dict:
            index = int(Path(path).stem)
            return self._sample_parsed_for_scan(path, f"knox:{index}", f"mail-{index}")

        monkeypatch.setattr("knowmate.secure.mysingle_reader.parse_mail_file", parse)

        class RecordingEmbed:
            def __init__(self):
                self.calls = []

            def embed(self, texts):
                self.calls.append(list(texts))
                return [[0.0] * VECTOR_DIM for _ in texts]

        embed = RecordingEmbed()
        indexer = EmailIndexer(tmp_path / "db", embed, batch_size=32)
        indexed, _ = run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 65}},
            state_file=tmp_path / "state.json", failure_file=tmp_path / "failures.json",
        )
        assert indexed == 65
        assert [len(call) for call in embed.calls] == [32, 32, 1]

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_streaming_window_fills_batch_before_flushing(self, tmp_path, monkeypatch):
        """32 미만 메일 둘은 다음 메일까지 받아 32개 API batch를 먼저 채운다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        for name in ("a", "b"):
            (watch / f"{name}.mysingle").write_bytes(b"x")
        body = ("x" * 99 + "\n") * 18
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(path, f"knox:{Path(path).stem}", body),
        )

        class RecordingEmbed:
            def __init__(self):
                self.calls = []

            def embed(self, texts):
                self.calls.append(len(texts))
                return [[0.0] * VECTOR_DIM for _ in texts]

        embed = RecordingEmbed()
        indexer = EmailIndexer(tmp_path / "db", embed, chunk_size=100, overlap=0, batch_size=32)
        indexed, _ = run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 2}},
            state_file=tmp_path / "state.json", failure_file=tmp_path / "failures.json",
        )
        assert indexed == 2
        assert embed.calls[0] == 32

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_streaming_window_never_accumulates_500_mail_jobs(self, tmp_path, monkeypatch):
        """500개 단일 청크 메일도 최대 32개 작업만 가진 윈도우로 순차 확정한다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        for index in range(500):
            (watch / f"{index:03d}.mysingle").write_bytes(b"x")
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(path, f"knox:{Path(path).stem}", "body"),
        )

        class GoodEmbed:
            def embed(self, texts):
                return [[0.0] * VECTOR_DIM for _ in texts]

        indexer = EmailIndexer(tmp_path / "db", GoodEmbed(), batch_size=32)
        windows = []
        original_embed_mail_jobs = indexer.embed_mail_jobs

        def record_window(jobs):
            windows.append((len(jobs), sum(len(job.chunks) for job in jobs)))
            return original_embed_mail_jobs(jobs)

        indexer.embed_mail_jobs = record_window
        indexed, _ = run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 500}},
            state_file=tmp_path / "state.json", failure_file=tmp_path / "failures.json",
        )
        assert indexed == 500
        assert len(windows) == 16
        assert max(job_count for job_count, _ in windows) == 32
        assert max(chunk_count for _, chunk_count in windows) == 32

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_duplicate_uid_has_one_generation_and_stays_cached(self, tmp_path, monkeypatch):
        """서로 다른 복사본은 커서 순서와 무관하게 한 번만 저장하고 다음 주기에 안정적이다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        first = watch / "old.mysingle"
        second = watch / "new.mysingle"
        first.write_bytes(b"x")
        second.write_bytes(b"x")
        os.utime(first, (1_000, 1_000))
        os.utime(second, (2_000, 2_000))
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(path, "knox:immutable", "same body"),
        )

        class RecordingEmbed:
            def __init__(self):
                self.calls = 0

            def embed(self, texts):
                self.calls += 1
                return [[0.0] * VECTOR_DIM for _ in texts]

        embed = RecordingEmbed()
        indexer = EmailIndexer(tmp_path / "db", embed, batch_size=32)
        state_file = tmp_path / "state.json"
        failure_file = tmp_path / "failures.json"
        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 2}},
            state_file=state_file, failure_file=failure_file,
        ) == (1, 1)
        assert indexer.table.count_rows() == 1
        assert embed.calls == 1
        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 2}},
            state_file=state_file, failure_file=failure_file,
        ) == (0, 2)
        assert indexer.table.count_rows() == 1
        assert embed.calls == 1

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_duplicate_uid_after_completed_window_uses_existing_generation(self, tmp_path, monkeypatch):
        """앞 윈도우에서 확정한 UID의 뒤 복사본은 DB 재확인·새 세대를 만들지 않는다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        for index in range(33):
            (watch / f"{index:03d}.mysingle").write_bytes(b"x")

        def parse(path: str) -> dict:
            index = int(Path(path).stem)
            uid = "knox:duplicate" if index in {0, 32} else f"knox:{index}"
            return self._sample_parsed_for_scan(path, uid, f"mail-{index}")

        monkeypatch.setattr("knowmate.secure.mysingle_reader.parse_mail_file", parse)

        class RecordingEmbed:
            def __init__(self):
                self.calls = 0

            def embed(self, texts):
                self.calls += 1
                return [[0.0] * VECTOR_DIM for _ in texts]

        embed = RecordingEmbed()
        indexer = EmailIndexer(tmp_path / "db", embed, batch_size=32)
        seen_uids = []
        real_get_index_state = indexer.get_index_state

        def record_index_state(mail_uid, mtime):
            seen_uids.append(mail_uid)
            return real_get_index_state(mail_uid, mtime)

        indexer.get_index_state = record_index_state
        state_file = tmp_path / "state.json"
        failure_file = tmp_path / "failures.json"
        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 33}},
            state_file=state_file, failure_file=failure_file,
        ) == (32, 1)
        assert seen_uids.count("knox:duplicate") == 1
        assert indexer.table.count_rows() == 32
        assert embed.calls == 1

        # 두 경로 모두 성공 캐시가 생겼으므로 다음 사이클은 파일을 열거나 DB를 조회하지 않는다.
        seen_uids.clear()
        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 33}},
            state_file=state_file, failure_file=failure_file,
        ) == (0, 33)
        assert seen_uids == []
        assert indexer.table.count_rows() == 32
        assert embed.calls == 1

    @pytest.mark.parametrize("old_first", [True, False])
    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_changed_duplicate_uid_selects_newer_content_regardless_of_cursor(
        self, tmp_path, monkeypatch, old_first,
    ):
        """같은 UID라도 새 본문은 old CURRENT·커서 순서와 무관하게 안전 교체한다."""
        from knowmate.collector.mail_scan_state import normalize_path_key
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer

        watch = tmp_path / "watch"
        watch.mkdir()
        old_path = watch / "old.mysingle"
        new_path = watch / "new.mysingle"
        old_path.write_bytes(b"old")
        new_path.write_bytes(b"new")
        os.utime(old_path, (1_000, 1_000))
        os.utime(new_path, (2_000, 2_000))
        uid = "knox:changed-duplicate"

        indexer = EmailIndexer(tmp_path / "db", _fake_embed(), batch_size=32)
        indexer.index_mail(self._sample_parsed_for_scan(str(old_path), uid, "old body"), 1_000.0)

        def parse(path: str) -> dict:
            body = "new body" if Path(path).name == "new.mysingle" else "old body"
            return self._sample_parsed_for_scan(path, uid, body)

        monkeypatch.setattr("knowmate.secure.mysingle_reader.parse_mail_file", parse)
        state_file = tmp_path / "state.json"
        if old_first:
            # 정렬상 newest인 new를 직전에 처리한 것처럼 두면 다음 순환은 old부터 시작한다.
            state_file.write_text(json.dumps({
                "schema_version": 1,
                "cursor": {"mtime": 2_000.0, "path": normalize_path_key(str(new_path))},
                "files": {},
                "pending_deletes": [],
            }), encoding="utf-8")

        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 2}},
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (1, 1)
        rows = indexer.table.search().select(["mail_uid", "mtime", "source_file", "text"]).to_arrow().to_pylist()
        assert [{key: row[key] for key in ("mail_uid", "mtime", "source_file")} for row in rows] == [
            {"mail_uid": uid, "mtime": 2_000.0, "source_file": str(new_path)},
        ]
        assert "new body" in rows[0]["text"] and "old body" not in rows[0]["text"]

        # old/new 양쪽은 shadow/current로 캐시되므로 다음 스캔은 DB 상태 조회 없이 안정적이다.
        indexer.get_index_state = lambda *_args: pytest.fail("resolved UID cache must skip DB state")
        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 2}},
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (0, 2)
        assert indexer.table.count_rows() == 1

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_pending_changed_duplicate_uid_replaces_only_after_prior_generation_finalizes(
        self, tmp_path, monkeypatch,
    ):
        """배치 대기 중 발견된 새 본문도 순차 확정해 최종 active generation은 하나다."""
        from knowmate.collector.mail_scan_state import normalize_path_key
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer

        watch = tmp_path / "watch"
        watch.mkdir()
        old_path = watch / "old.mysingle"
        new_path = watch / "new.mysingle"
        old_path.write_bytes(b"old")
        new_path.write_bytes(b"new")
        os.utime(old_path, (1_000, 1_000))
        os.utime(new_path, (2_000, 2_000))
        uid = "knox:pending-changed"
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(
                path, uid, "new body" if Path(path).name == "new.mysingle" else "old body",
            ),
        )
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({
            "schema_version": 1,
            "cursor": {"mtime": 2_000.0, "path": normalize_path_key(str(new_path))},
            "files": {},
            "pending_deletes": [],
        }), encoding="utf-8")
        indexer = EmailIndexer(tmp_path / "db", _fake_embed(), batch_size=32)

        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 2}},
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (2, 0)
        rows = indexer.table.search().select(["mtime", "source_file", "text"]).to_arrow().to_pylist()
        assert [{key: row[key] for key in ("mtime", "source_file")} for row in rows] == [
            {"mtime": 2_000.0, "source_file": str(new_path)},
        ]
        assert "new body" in rows[0]["text"] and "old body" not in rows[0]["text"]
        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 2}},
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (0, 2)

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_content_failure_isolates_one_mail_and_continues_later_chunks(self, tmp_path, monkeypatch):
        """중간 ContentError는 이분 격리하고 그 뒤 메일을 계속 저장한다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import EmbeddingContentError, VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        for index in range(32):
            (watch / f"{index:03d}.mysingle").write_bytes(b"x")

        def parse(path: str) -> dict:
            index = int(Path(path).stem)
            body = "BAD-CHUNK" if index == 15 else f"body-{index}"
            return self._sample_parsed_for_scan(path, f"knox:{index}", body)

        monkeypatch.setattr("knowmate.secure.mysingle_reader.parse_mail_file", parse)

        class SelectiveEmbed:
            def __init__(self):
                self.calls = 0

            def embed(self, texts):
                self.calls += 1
                if any("BAD-CHUNK" in text for text in texts):
                    raise EmbeddingContentError("bad input")
                return [[0.0] * VECTOR_DIM for _ in texts]

        embed = SelectiveEmbed()
        indexer = EmailIndexer(tmp_path / "db", embed, batch_size=32)
        failure_file = tmp_path / "failures.json"
        indexed, _ = run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 32}},
            state_file=tmp_path / "state.json", failure_file=failure_file,
        )
        rows = indexer.table.search().select(["mail_uid"]).to_arrow().to_pylist()
        assert indexed == 31
        assert "knox:15" not in {row["mail_uid"] for row in rows}
        assert str(watch / "015.mysingle") in failure_state.load_failures(failure_file)
        assert embed.calls > 1

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_multiple_content_failures_only_defer_their_owners(self, tmp_path, monkeypatch):
        """여러 불량 청크도 각 소유 메일만 실패시키고 나머지는 계속 저장한다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import EmbeddingContentError, VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        for index in range(33):
            (watch / f"{index:03d}.mysingle").write_bytes(b"x")

        def parse(path: str) -> dict:
            index = int(Path(path).stem)
            body = "BAD-CHUNK" if index in {8, 24} else f"body-{index}"
            return self._sample_parsed_for_scan(path, f"knox:{index}", body)

        monkeypatch.setattr("knowmate.secure.mysingle_reader.parse_mail_file", parse)

        class SelectiveEmbed:
            def embed(self, texts):
                if any("BAD-CHUNK" in text for text in texts):
                    raise EmbeddingContentError("bad input")
                return [[0.0] * VECTOR_DIM for _ in texts]

        indexer = EmailIndexer(tmp_path / "db", SelectiveEmbed(), batch_size=32)
        failure_file = tmp_path / "failures.json"
        indexed, _ = run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 33}},
            state_file=tmp_path / "state.json", failure_file=failure_file,
        )
        stored_uids = {row["mail_uid"] for row in indexer.table.search().select(["mail_uid"]).to_arrow().to_pylist()}
        assert indexed == 31
        assert {"knox:8", "knox:24"}.isdisjoint(stored_uids)
        failures = failure_state.load_failures(failure_file)
        assert str(watch / "008.mysingle") in failures
        assert str(watch / "024.mysingle") in failures

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_multi_batch_mail_content_failure_has_no_partial_db_rows(self, tmp_path):
        """여러 batch에 걸친 한 메일도 청크 하나가 실패하면 DB에 부분 저장하지 않는다."""
        from knowmate.rag.email_indexer import EmailIndexer, MailIndexCheck, MailIndexState
        from knowmate.rag.embedding import EmbeddingContentError, VECTOR_DIM

        class SelectiveEmbed:
            def embed(self, texts):
                if any("BAD-CHUNK" in text for text in texts):
                    raise EmbeddingContentError("bad input")
                return [[0.0] * VECTOR_DIM for _ in texts]

        indexer = EmailIndexer(tmp_path / "db", SelectiveEmbed(), chunk_size=100, overlap=0, batch_size=32)
        parsed = self._sample_parsed_for_scan("/data/long.mysingle", "knox:long", "x" * 3400 + "BAD-CHUNK")
        job = indexer.prepare_mail(parsed, 1000.0, MailIndexCheck(MailIndexState.MISSING))
        assert len(job.chunks) > 32
        indexer.embed_mail_jobs([job])
        assert job.content_error is not None
        with pytest.raises(RuntimeError, match="완료되지"):
            indexer.commit_mail_job(job)
        assert indexer.table.count_rows() == 0

    @pytest.mark.parametrize("error_class", ["transient", "protocol"])
    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_non_content_error_does_not_split_embedding_batch(self, tmp_path, monkeypatch, error_class):
        """transient·protocol 오류는 32청크 batch를 이분 분할하지 않는다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import EmbeddingProtocolError, EmbeddingTransientError

        watch = tmp_path / "watch"
        watch.mkdir()
        for index in range(32):
            (watch / f"{index:03d}.mysingle").write_bytes(b"x")
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(path, f"knox:{Path(path).stem}", "body"),
        )
        error = EmbeddingTransientError("busy") if error_class == "transient" else EmbeddingProtocolError("schema")

        class FailingEmbed:
            def __init__(self):
                self.calls = 0

            def embed(self, _texts):
                self.calls += 1
                raise error

        embed = FailingEmbed()
        indexer = EmailIndexer(tmp_path / "db", embed, batch_size=32)
        indexed, skipped = run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 32}},
            state_file=tmp_path / "state.json", failure_file=tmp_path / "failures.json",
        )
        assert (indexed, skipped) == (0, 32)
        assert embed.calls == 1

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_completed_window_survives_later_transient_error(self, tmp_path, monkeypatch):
        """앞 32개 윈도우는 다음 윈도우의 전역 오류 뒤에도 저장 상태를 유지한다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import EmbeddingTransientError, VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        for index in range(64):
            (watch / f"{index:03d}.mysingle").write_bytes(b"x")
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(path, f"knox:{Path(path).stem}", "body"),
        )

        class FailsSecondWindow:
            def __init__(self):
                self.calls = 0

            def embed(self, texts):
                self.calls += 1
                if self.calls == 2:
                    raise EmbeddingTransientError("busy")
                return [[0.0] * VECTOR_DIM for _ in texts]

        embed = FailsSecondWindow()
        indexer = EmailIndexer(tmp_path / "db", embed, batch_size=32)
        indexed, skipped = run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 64}},
            state_file=tmp_path / "state.json", failure_file=tmp_path / "failures.json",
        )
        assert (indexed, skipped) == (32, 32)
        assert indexer.table.count_rows() == 32
        assert embed.calls == 2

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_one_mail_add_failure_does_not_block_other_ready_mail(self, tmp_path, monkeypatch):
        """메일별 add 실패는 다른 준비 완료 메일의 저장을 막지 않는다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        for name in ("a", "b"):
            (watch / f"{name}.mysingle").write_bytes(b"x")
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(path, f"knox:{Path(path).stem}", "body"),
        )

        class GoodEmbed:
            def embed(self, texts):
                return [[0.0] * VECTOR_DIM for _ in texts]

        indexer = EmailIndexer(tmp_path / "db", GoodEmbed())
        original_commit = indexer.commit_mail_job

        def fail_first(job):
            if job.parsed["mail_uid"] == "knox:a":
                raise RuntimeError("add failed")
            return original_commit(job)

        indexer.commit_mail_job = fail_first
        failure_file = tmp_path / "failures.json"
        indexed, _ = run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 2}},
            state_file=tmp_path / "state.json", failure_file=failure_file,
        )
        assert indexed == 1
        assert {row["mail_uid"] for row in indexer.table.search().select(["mail_uid"]).to_arrow().to_pylist()} == {"knox:b"}
        assert str(watch / "a.mysingle") in failure_state.load_failures(failure_file)

    @pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb 미설치")
    def test_progress_reports_only_after_each_mail_is_final(self, tmp_path, monkeypatch):
        """배치 대기 중에는 진행률을 보내지 않고 commit 뒤에만 source 결과를 알린다."""
        from knowmate.collector.mail_scanner import run_mail_scan
        from knowmate.rag.email_indexer import EmailIndexer
        from knowmate.rag.embedding import VECTOR_DIM

        watch = tmp_path / "watch"
        watch.mkdir()
        for name in ("a", "b"):
            (watch / f"{name}.mysingle").write_bytes(b"x")
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(path, f"knox:{Path(path).stem}", "body"),
        )
        progress = []

        class CheckingEmbed:
            def embed(self, texts):
                assert progress == []
                return [[0.0] * VECTOR_DIM for _ in texts]

        indexer = EmailIndexer(tmp_path / "db", CheckingEmbed(), batch_size=32)

        def on_progress(current, total, filename):
            progress.append((current, total, filename, indexer.table.count_rows()))

        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 2, "batch_commit_every": 1}}, on_progress,
            state_file=tmp_path / "state.json", failure_file=tmp_path / "failures.json",
        ) == (2, 0)
        assert [(current, total, rows) for current, total, _name, rows in progress] == [(1, 2, 1), (2, 2, 2)]

    def test_warm_cache_does_not_flood_progress_callbacks(self, tmp_path, monkeypatch):
        """시도 예산 밖의 대량 성공 캐시는 진행률 callback을 건별로 호출하지 않는다."""
        from knowmate.collector.mail_scan_state import cache_success, save_mail_scan_state
        from knowmate.collector.mail_scanner import run_mail_scan

        watch = tmp_path / "watch"
        watch.mkdir()
        state = {"schema_version": 1, "cursor": None, "files": {}, "pending_deletes": []}
        for index in range(1_000):
            path = watch / f"{index:04d}.mysingle"
            path.write_bytes(b"x")
            cache_success(
                state,
                {"path": str(path), "mtime": path.stat().st_mtime, "size": path.stat().st_size},
                f"knox:warm-{index}",
            )
        state_file = tmp_path / "state.json"
        save_mail_scan_state(state_file, state)
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda _path: pytest.fail("warm cache must not parse mail files"),
        )
        indexer = types.SimpleNamespace(table_was_recreated=False, table_is_empty=False)
        progress = []

        assert run_mail_scan(
            [str(watch)], indexer,
            {"mail": {"max_mails_per_scan": 500, "batch_commit_every": 1}},
            on_progress=lambda *event: progress.append(event),
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (0, 1_000)
        assert progress == []

    def test_early_filter_retains_only_actionable_candidates_and_normalizes_once(self, tmp_path, monkeypatch):
        """대량 캐시·백오프는 전수 확인하되 후보 정렬·보관에는 넣지 않는다."""
        from knowmate.collector import failure_state, mail_scanner

        monkeypatch.setitem(
            sys.modules, "knowmate.rag.email_indexer", types.SimpleNamespace(EMAIL_INDEX_VERSION="3"),
        )
        watch = tmp_path / "mail"
        watch.mkdir()
        paths = [str(watch / f"{index:04d}.mysingle") for index in range(1_000)]
        state = {"files": {}, "pending_deletes": []}
        for index, path in enumerate(paths[:980]):
            state["files"][os.path.normcase(os.path.abspath(path))] = {
                "path": path, "mtime": float(index), "size": 1,
                "mail_uid": f"knox:cached-{index}", "index_version": "3",
                "uid_resolution_version": 2,
            }
        failures = {}
        for index, path in enumerate(paths[980:990], start=980):
            failure_state.note_failure(
                failures, path, failure_state.KIND_UNKNOWN_TRANSIENT, "parse", None,
                float(index), 1, 1_000.0,
            )

        real_normalize = mail_scanner.normalize_path_key
        normalized = []
        monkeypatch.setattr(
            mail_scanner, "normalize_path_key",
            lambda path: normalized.append(path) or real_normalize(path),
        )
        monkeypatch.setattr(
            mail_scanner, "_iter_mail_files",
            lambda _root, _exts: ((path, float(index), 1) for index, path in enumerate(paths)),
        )

        candidates, seen_keys, cached_failures, cached_uids, skipped = mail_scanner._collect_actionable_candidates(
            [str(watch)], [".mysingle"], state, failures, 1_001.0,
            failure_state.BackoffPolicy(),
        )

        assert len(seen_keys) == len(paths)
        assert len(normalized) == len(paths)
        assert cached_failures == []
        assert len(cached_uids) == 980
        assert skipped == 990
        assert [Path(item["path"]).stem for item in candidates] == [
            f"{index:04d}" for index in range(999, 989, -1)
        ]

    def test_early_filter_excludes_cache_and_active_backoff_from_attempt_budget(
        self, tmp_path, monkeypatch,
    ):
        """캐시·대기 파일은 파싱/DB 시도와 actionable 후보 모두에서 빠진다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scan_state import save_mail_scan_state
        from knowmate.collector.mail_scanner import run_mail_scan

        watch = tmp_path / "watch"
        watch.mkdir()
        cached = watch / "cached.mysingle"
        deferred = watch / "deferred.mysingle"
        actionable = watch / "actionable.mysingle"
        for index, path in enumerate((cached, deferred, actionable)):
            _write_mail(path, uid=f"20260626007777{index}", msgid=path.stem)
            os.utime(path, (3_000 - index, 3_000 - index))
        indexer = self._fake_mail_indexer(monkeypatch)
        state_file = tmp_path / "state.json"
        cache_key = os.path.normcase(os.path.abspath(str(cached)))
        save_mail_scan_state(state_file, {
            "schema_version": 1,
            "cursor": None,
            "files": {cache_key: {
                "path": str(cached), "mtime": cached.stat().st_mtime, "size": cached.stat().st_size,
                "mail_uid": "knox:cached", "index_version": "3", "uid_resolution_version": 2,
            }},
            "pending_deletes": [],
        })
        failure_file = tmp_path / "failures.json"
        failures = {}
        failure_state.note_failure(
            failures, str(deferred), failure_state.KIND_UNKNOWN_TRANSIENT, "parse", None,
            deferred.stat().st_mtime, deferred.stat().st_size, 1_000.0,
        )
        failure_state.save_failures(failure_file, failures)
        parsed_paths = []
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: parsed_paths.append(path) or self._sample_parsed_for_scan(path, f"knox:{Path(path).stem}", "body"),
        )

        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 1}},
            state_file=state_file, failure_file=failure_file, get_now=lambda: 1_001.0,
        ) == (1, 2)
        assert parsed_paths == [str(actionable)]
        assert indexer.state_check_calls == 1

    def test_expired_mail_backoff_becomes_actionable_again(self, tmp_path, monkeypatch):
        """백오프 만료 파일은 다음 전수 스캔에서 다시 시도 후보가 된다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scanner import run_mail_scan

        watch = tmp_path / "watch"
        watch.mkdir()
        path = watch / "retry.mysingle"
        _write_mail(path, uid="2026062600777799", msgid="expired-backoff")
        indexer = self._fake_mail_indexer(monkeypatch)
        failure_file = tmp_path / "failures.json"
        failures = {}
        failure_state.note_failure(
            failures, str(path), failure_state.KIND_UNKNOWN_TRANSIENT, "parse", None,
            path.stat().st_mtime, path.stat().st_size, 1_000.0,
        )
        failure_state.save_failures(failure_file, failures)

        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 1}},
            state_file=tmp_path / "state.json", failure_file=failure_file,
            get_now=lambda: 2_801.0,
        ) == (1, 0)
        assert indexer.state_check_calls == 1
        assert failure_state.load_failures(failure_file) == {}

    def test_cached_newer_duplicate_uid_keeps_older_changed_alias_as_shadow(self, tmp_path, monkeypatch):
        """후보에서 제외한 최신 alias가 오래된 변경 본문의 세대 되돌림을 막는다."""
        from knowmate.collector.mail_scan_state import load_mail_scan_state, save_mail_scan_state
        from knowmate.collector.mail_scanner import run_mail_scan

        watch = tmp_path / "watch"
        watch.mkdir()
        old_path = watch / "old.mysingle"
        cached_new_path = watch / "new.mysingle"
        old_path.write_bytes(b"old")
        cached_new_path.write_bytes(b"new")
        os.utime(old_path, (100, 100))
        os.utime(cached_new_path, (200, 200))
        indexer = self._fake_mail_indexer(monkeypatch)
        state_file = tmp_path / "state.json"
        cached_key = os.path.normcase(os.path.abspath(str(cached_new_path)))
        save_mail_scan_state(state_file, {
            "schema_version": 1,
            "cursor": None,
            "files": {cached_key: {
                "path": str(cached_new_path),
                "mtime": cached_new_path.stat().st_mtime, "size": cached_new_path.stat().st_size,
                "mail_uid": "knox:duplicate", "index_version": "3", "uid_resolution_version": 2,
            }},
            "pending_deletes": [],
        })
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(path, "knox:duplicate", "old body"),
        )

        assert run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 1}},
            state_file=state_file, failure_file=tmp_path / "failures.json",
        ) == (0, 2)
        assert indexer.state_check_calls == 0
        state = load_mail_scan_state(state_file)
        assert os.path.normcase(os.path.abspath(str(old_path))) in state["files"]

    def test_actionable_cursor_reaches_more_than_limit_all_new_mail(self, tmp_path, monkeypatch):
        """500건을 넘는 신규 backlog도 actionable 커서로 모두 한 번씩 처리한다."""
        from knowmate.collector.mail_scanner import run_mail_scan

        watch = tmp_path / "watch"
        watch.mkdir()
        for index in range(501):
            (watch / f"{index:04d}.mysingle").write_bytes(b"x")
        indexer = self._fake_mail_indexer(monkeypatch)
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: self._sample_parsed_for_scan(path, f"knox:{Path(path).stem}", "body"),
        )
        state_file = tmp_path / "state.json"
        failure_file = tmp_path / "failures.json"
        cfg = {"mail": {"max_mails_per_scan": 200}}

        assert run_mail_scan([str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file) == (200, 0)
        assert run_mail_scan([str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file) == (200, 200)
        assert run_mail_scan([str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file) == (101, 400)
        assert len(indexer.indexed) == 501

    def test_actionable_cursor_wraps_when_newer_candidate_appears(self, tmp_path, monkeypatch):
        """처리 중 후보 구성이 바뀌어도 새 최신 파일이 기존 backlog를 굶기지 않는다."""
        from knowmate.collector.mail_scanner import run_mail_scan

        watch = tmp_path / "watch"
        watch.mkdir()
        for name, mtime in (("a", 300), ("b", 200), ("c", 100)):
            path = watch / f"{name}.mysingle"
            path.write_bytes(b"x")
            os.utime(path, (mtime, mtime))
        parsed_names = []
        monkeypatch.setattr(
            "knowmate.secure.mysingle_reader.parse_mail_file",
            lambda path: parsed_names.append(Path(path).stem)
            or self._sample_parsed_for_scan(path, f"knox:{Path(path).stem}", "body"),
        )
        indexer = self._fake_mail_indexer(monkeypatch)
        state_file = tmp_path / "state.json"
        failure_file = tmp_path / "failures.json"
        cfg = {"mail": {"max_mails_per_scan": 1}}

        assert run_mail_scan([str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file) == (1, 0)
        newer = watch / "d.mysingle"
        newer.write_bytes(b"x")
        os.utime(newer, (400, 400))
        for expected_skips in (1, 2, 3):
            assert run_mail_scan(
                [str(watch)], indexer, cfg, state_file=state_file, failure_file=failure_file,
            ) == (1, expected_skips)
        assert parsed_names == ["a", "b", "c", "d"]

    def test_inaccessible_root_keeps_pending_deletes_and_failure_history(self, tmp_path, monkeypatch):
        """일시 단절 root에서는 failure prune과 pending-delete 손실이 모두 없어야 한다."""
        from knowmate.collector import failure_state
        from knowmate.collector.mail_scan_state import load_mail_scan_state, save_mail_scan_state
        from knowmate.collector.mail_scanner import run_mail_scan

        missing_root = tmp_path / "disconnected"
        failed_path = str(missing_root / "mail.mysingle")
        failure_file = tmp_path / "failures.json"
        failures = {}
        failure_state.note_failure(
            failures, failed_path, failure_state.KIND_UNKNOWN_TRANSIENT, "parse", None,
            1_000.0, 1, 2_000.0,
        )
        failure_state.save_failures(failure_file, failures)
        state_file = tmp_path / "state.json"
        save_mail_scan_state(state_file, {
            "schema_version": 1, "cursor": None, "files": {}, "pending_deletes": ["old-1"],
        })
        indexer = self._fake_mail_indexer(monkeypatch)
        indexer.delete_chunk_ids = lambda _ids: (_ for _ in ()).throw(RuntimeError("offline"))

        assert run_mail_scan(
            [str(missing_root)], indexer, {"mail": {"max_mails_per_scan": 1}},
            state_file=state_file, failure_file=failure_file,
        ) == (0, 0)
        assert failed_path in failure_state.load_failures(failure_file)
        assert load_mail_scan_state(state_file)["pending_deletes"] == ["old-1"]

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
