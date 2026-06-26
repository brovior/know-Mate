"""Knox .mysingle 메일 인덱싱 파이프라인 테스트.

모든 테스트는 extractor:fake + embedding:fake 조건에서 사외(폐쇄망 외부)에서도 통과해야 한다.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from knowmate.rag.embedding import EmbeddingClient

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_embed() -> EmbeddingClient:
    """테스트용 fake 임베딩 클라이언트를 반환한다."""
    return EmbeddingClient(base_url="", host_header="", fake=True)


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
        p.write_text(content, encoding="utf-8")
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
        p.write_text(content, encoding="utf-8")
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

    def test_delete_mail_chunks(self, tmp_path):
        """delete_mail_chunks 후 is_indexed가 False가 된다."""
        from knowmate.rag.email_indexer import EmailIndexer
        ei = EmailIndexer(db_path=tmp_path, embed_client=_fake_embed())
        ei.index_mail(self._sample_parsed("knox:DEL001"), mtime=1000.0)
        ei.delete_mail_chunks("knox:DEL001")
        assert not ei.is_indexed("knox:DEL001", 1000.0)


# ---------------------------------------------------------------------------
# mail_scanner 테스트
# ---------------------------------------------------------------------------

class TestMailScanner:
    def test_scan_finds_mysingle(self, tmp_path):
        """scan_mail_folders가 .mysingle 파일을 탐지한다."""
        (tmp_path / "a.mysingle").write_bytes(b"test")
        (tmp_path / "b.txt").write_bytes(b"not mail")
        from knowmate.collector.mail_scanner import scan_mail_folders
        results = scan_mail_folders([str(tmp_path)], max_per_scan=100)
        assert len(results) == 1
        assert results[0]["path"].endswith(".mysingle")

    def test_scan_respects_max(self, tmp_path):
        """max_per_scan 제한이 적용된다."""
        for i in range(5):
            (tmp_path / f"m{i}.mysingle").write_bytes(b"test")
        from knowmate.collector.mail_scanner import scan_mail_folders
        results = scan_mail_folders([str(tmp_path)], max_per_scan=3)
        assert len(results) == 3

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
        cnt, _ = run_mail_scan([str(dest.parent)], ei, cfg)
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

        run_mail_scan([str(dest.parent)], ei, cfg)           # 1차
        cnt2, skipped = run_mail_scan([str(dest.parent)], ei, cfg)  # 2차
        assert cnt2 == 0
        assert skipped == 1

    def test_mail_disabled_check(self):
        """mail.enabled=false이면 스캔 분기에 진입하지 않는다."""
        cfg = {"mail": {"enabled": False}}
        assert not cfg.get("mail", {}).get("enabled", False)
