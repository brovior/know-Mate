"""PDF diagnostics preserve extraction and avoid logging document content."""
import logging
import sys
from types import SimpleNamespace

import pytest

from knowmate.secure.plain_reader import PlainReader


@pytest.fixture
def sample_pdf(tmp_path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "sample.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), "PRIVATE_DOCUMENT_CONTENT")
        doc.save(path)
    return path


def test_pdf_probe_logs_runtime_and_header_without_extracted_text(sample_pdf, monkeypatch, caplog):
    import fitz

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "AegisDesk.exe")
    with caplog.at_level(logging.INFO, logger="knowmate.secure.plain_reader"):
        text = PlainReader().extract(str(sample_pdf))

    assert "PRIVATE_DOCUMENT_CONTENT" in text
    assert "PRIVATE_DOCUMENT_CONTENT" not in caplog.text
    assert "frozen=True executable=AegisDesk.exe" in caplog.text
    assert f"pymupdf_version={fitz.VersionBind}" in caplog.text
    assert f"header_read=ok header_bytes=8 header_hex={sample_pdf.read_bytes()[:8].hex().upper()}" in caplog.text


def test_header_access_failure_does_not_prevent_parser_attempt(sample_pdf, monkeypatch, caplog):
    import knowmate.secure.plain_reader as reader_module

    def denied(*args, **kwargs):
        raise PermissionError(13, "PRIVATE_ACCESS_MESSAGE")

    monkeypatch.setattr(reader_module, "open", denied, raising=False)
    with caplog.at_level(logging.INFO, logger=reader_module.__name__):
        text = PlainReader().extract(str(sample_pdf))

    assert "PRIVATE_DOCUMENT_CONTENT" in text
    assert "header_read=failed header_bytes=0 header_hex=-" in caplog.text
    assert "error_type=PermissionError errno=13" in caplog.text
    assert "PRIVATE_ACCESS_MESSAGE" not in caplog.text
    assert "PRIVATE_DOCUMENT_CONTENT" not in caplog.text


def test_pdf_open_failure_preserves_original_exception_and_redacts_messages(tmp_path, monkeypatch, caplog):
    path = tmp_path / "unreadable.pdf"
    path.write_bytes(b"TESTHEADPRIVATE_DOCUMENT_CONTENT")
    cause = ValueError("PRIVATE_CAUSE_MESSAGE")
    failure = RuntimeError("PRIVATE_PARSER_MESSAGE")

    def failing_open(_path):
        raise failure from cause

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=failing_open, VersionBind="test-version"))
    with caplog.at_level(logging.INFO, logger="knowmate.secure.plain_reader"):
        with pytest.raises(RuntimeError) as raised:
            PlainReader().extract(str(path))

    assert raised.value is failure
    assert raised.value.__cause__ is cause
    assert "header_read=ok header_bytes=8 header_hex=5445535448454144" in caplog.text
    assert "error_type=RuntimeError cause_type=ValueError" in caplog.text
    assert "PRIVATE_" not in caplog.text
