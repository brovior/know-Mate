"""문서·메일 경계 정리와 임베딩 취소의 종료·데이터 보존 회귀."""
import json
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from knowmate.app.lifecycle import stop_worker
from knowmate.rag.embedding import EmbeddingClient, VECTOR_DIM
from knowmate.rag.embedding_cancel import EmbeddingCancellation, EmbeddingCancelledError, embedding_cancellation_scope


def _tls_contexts(tmp_path):
    """localhost 회귀에만 사용하는 인증서와 TLS 컨텍스트를 만든다."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "localhost.crt", tmp_path / "localhost.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_file, key_file)
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.load_verify_locations(cert_file)
    return server_context, client_context


@pytest.mark.parametrize("response_started", [False, True])
@pytest.mark.parametrize("warm_up", [False, True])
@pytest.mark.parametrize("use_tls", [False, True])
def test_cancel_interrupts_blocked_http_without_retry(response_started, warm_up, use_tls, tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(1)
            if warm_up and len(requests) == 1:
                payload = json.dumps({"data": [{"index": 0, "embedding": [0.0] * VECTOR_DIM}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                return
            if response_started:
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"{")
                self.wfile.flush()
            entered.set()
            release.wait(5)

        def log_message(self, *_args):
            pass

    cancellation = EmbeddingCancellation()
    errors = []
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        if use_tls:
            server_context, client_context = _tls_contexts(tmp_path)
            server.socket = server_context.wrap_socket(server.socket, server_side=True)
            monkeypatch.setattr(ssl, "_create_default_https_context", lambda: client_context)
        server_thread = threading.Thread(target=server.handle_request, daemon=True)
        client = EmbeddingClient(f"{'https' if use_tls else 'http'}://localhost:{server.server_port}", "localhost")

        def embed():
            try:
                with embedding_cancellation_scope(cancellation):
                    if warm_up:
                        client.embed(["warmup"])
                    client.embed(["synthetic input"])
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=embed, daemon=True)
        server_thread.start()
        worker.start()
        try:
            assert entered.wait(3)
            assert cancellation.in_progress
            cancellation.cancel()
            worker.join(2)
            assert not worker.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], EmbeddingCancelledError)
            assert requests == [1] * (2 if warm_up else 1)
            assert not cancellation.in_progress
        finally:
            cancellation.cancel()
            release.set()
            worker.join(3)
            server_thread.join(3)


def test_cancel_scope_and_keepalive_are_isolated_between_threads():
    client = EmbeddingClient("http://unused", "unused", fake=True)
    client._conn = sentinel = object()
    cancellation = EmbeddingCancellation()
    cancellation.cancel()
    results = []

    def independent_search():
        assert client._conn is None
        results.append(client.embed(["query"]))
        client._conn = object()

    with embedding_cancellation_scope(cancellation):
        with pytest.raises(EmbeddingCancelledError):
            client.embed(["cancelled document"])
        search = threading.Thread(target=independent_search)
        search.start()
        search.join(2)
        assert not search.is_alive()
    assert len(results) == 1
    assert client._conn is sentinel
    assert len(client.embed(["later query"])) == 1


@pytest.mark.parametrize("during_initial_wait", [False, True])
def test_shutdown_allows_bounded_embedding_or_office_cleanup(during_initial_wait):
    class Worker:
        maintenance_in_progress = False
        reads = 0

        @property
        def blocking_shutdown_work(self):
            self.reads += 1
            return not during_initial_wait or self.reads >= 2

        def isRunning(self):
            return True

        def cancel(self):
            pass

        def wait(self, ms):
            calls.append(ms)
            return len(calls) == (2 if during_initial_wait else 1)

        def terminate(self):
            pytest.fail("bounded cleanup should finish before terminate")

    calls = []
    assert stop_worker(Worker(), hard_exit=lambda _code: pytest.fail("unexpected hard exit")) is False
    assert calls == ([8000, 52000] if during_initial_wait else [60000])


def test_shutdown_timeout_log_reports_actual_stage(caplog):
    caplog.set_level("WARNING")
    worker = SimpleNamespace(
        isRunning=lambda: True, cancel=lambda: None, wait=lambda _ms: False,
        terminate=lambda: None, work_stage="embedding",
    )
    hard_exit = []
    assert stop_worker(worker, hard_exit=hard_exit.append) is True
    assert hard_exit == [0]
    assert "stage=embedding" in caplog.text
    assert "COM 블로킹 추정" not in caplog.text


@pytest.mark.parametrize("cleanup_result", ["success", "pending", "error"])
def test_document_cleanup_precedes_mail_even_below_restart_threshold(tmp_path, monkeypatch, cleanup_result):
    from knowmate.collector.scheduler import CollectorWorker
    from knowmate.rag.indexer import Indexer
    from knowmate.rag.email_indexer import EmailIndexer
    from knowmate.secure.fake_reader import FakeReader
    from knowmate.secure.office_guard import OfficeCleanupResult

    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "document.doc").write_bytes(b"synthetic document")
    events = []
    embedding = EmbeddingClient("", "", fake=True)
    indexer = Indexer(tmp_path / "db", embedding)
    mail_indexer = EmailIndexer(tmp_path / "db", embedding)
    owner_thread = threading.get_ident()

    def cleanup(**kwargs):
        assert threading.get_ident() == owner_thread
        assert kwargs == {"grace_sec": 5.0, "quit_timeout_sec": 10.0}
        events.append("office_cleanup")
        if cleanup_result == "error":
            raise RuntimeError("synthetic cleanup error")
        return OfficeCleanupResult(ownership_pending=frozenset({"WINWORD.EXE"}) if cleanup_result == "pending" else frozenset())

    def mail(*_args, **_kwargs):
        assert events == ["office_cleanup"]
        events.append("mail")
        return 0, 0

    monkeypatch.setattr("knowmate.collector.mail_scanner.run_mail_scan", mail)
    worker = CollectorWorker(
        {"collector": {"watch_folders": [str(watch)], "com_restart_every_n_files": 30},
         "mail": {"enabled": True}, "cleanup": {"dry_run": True}},
        indexer, FakeReader(), state_file=tmp_path / "state.json", email_indexer=mail_indexer,
        com_cleanup_fn=cleanup,
    )
    worker._run_cycle()
    assert events == ["office_cleanup", "mail"]
    assert not worker.blocking_shutdown_work


def test_mail_cancel_preserves_old_rows_and_does_not_record_failure(tmp_path, monkeypatch):
    from knowmate.collector.mail_scanner import run_mail_scan
    from knowmate.collector.failure_state import load_failures
    from knowmate.rag.email_indexer import EmailIndexer

    watch = tmp_path / "watch"
    watch.mkdir()
    source = watch / "message.mysingle"
    source.write_bytes(b"synthetic mail")
    parsed = {
        "mail_uid": "knox:synthetic", "message_id": "synthetic", "body_text": "synthetic body",
        "subject": "synthetic", "sender": "sender", "recipients": "recipient", "mail_date": "",
        "thread_ref": "", "source_file": str(source), "source_type": "knox", "source_meta": "{}",
    }
    monkeypatch.setattr("knowmate.secure.mysingle_reader.parse_mail_file", lambda _path: dict(parsed))
    client = EmbeddingClient("", "", fake=True)
    indexer = EmailIndexer(tmp_path / "db", client)
    old_ids = indexer.index_mail(dict(parsed), mtime=0)
    cancellation = EmbeddingCancellation()

    def cancelled_embed(_texts):
        cancellation.cancel()
        cancellation.check()

    monkeypatch.setattr(client, "embed", cancelled_embed)
    failure_file = tmp_path / "failures.json"
    with embedding_cancellation_scope(cancellation):
        indexed, _ = run_mail_scan(
            [str(watch)], indexer, {"mail": {"max_mails_per_scan": 1}},
            state_file=tmp_path / "mail_state.json", failure_file=failure_file,
        )
    assert indexed == 0
    assert not load_failures(failure_file)
    rows = indexer.table.search().select(["chunk_id"]).to_arrow().to_pylist()
    assert {row["chunk_id"] for row in rows} == set(old_ids)
    assert indexer.last_mail_scan_completion.value == "UNKNOWN"


def test_tray_shutdown_during_mail_embedding_finishes_worker_cleanup(tmp_path, monkeypatch):
    from knowmate.collector.scheduler import CollectorWorker
    from knowmate.rag.email_indexer import EmailIndexer
    from knowmate.rag.indexer import Indexer
    from knowmate.secure.fake_reader import FakeReader
    from knowmate.secure.office_guard import OfficeCleanupResult

    entered, release = threading.Event(), threading.Event()
    events = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            release.wait(10)

        def log_message(self, *_args):
            pass

    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "document.doc").write_bytes(b"synthetic document")
    (watch / "message.mysingle").write_bytes(b"synthetic mail")
    monkeypatch.setattr("knowmate.secure.mysingle_reader.parse_mail_file", lambda path: {
        "mail_uid": "knox:shutdown", "message_id": "shutdown", "body_text": "synthetic body",
        "subject": "synthetic", "sender": "sender", "recipients": "recipient", "mail_date": "",
        "thread_ref": "", "source_file": str(path), "source_type": "knox", "source_meta": "{}",
    })

    def cleanup(**_kwargs):
        events.append(("cleanup", threading.get_ident()))
        return OfficeCleanupResult()

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server_thread = threading.Thread(target=server.handle_request, daemon=True)
        doc_indexer = Indexer(tmp_path / "db", EmbeddingClient("", "", fake=True))
        mail_indexer = EmailIndexer(tmp_path / "db", EmbeddingClient(f"http://127.0.0.1:{server.server_port}", "localhost"))
        worker = CollectorWorker(
            {"collector": {"watch_folders": [str(watch)]}, "mail": {"enabled": True}, "cleanup": {"dry_run": True}},
            doc_indexer, FakeReader(), state_file=tmp_path / "state.json", email_indexer=mail_indexer,
            com_cleanup_fn=cleanup,
        )
        server_thread.start()
        worker.start()
        try:
            assert entered.wait(5)
            assert len(events) == 1  # 메일 HTTP 요청 전에 문서용 Office 정리를 끝냈다.
            assert worker.work_stage == "embedding"
            start = time.monotonic()
            assert stop_worker(worker, hard_exit=lambda _code: pytest.fail("unexpected hard exit")) is False
            assert time.monotonic() - start < 5
            assert not worker.isRunning()
            assert len(events) == 2  # 정상 종료의 최종 정리도 생성 워커에서 실행됐다.
            assert events[0][1] == events[1][1] != threading.get_ident()
            assert worker.work_stage == "idle"
            assert mail_indexer.table.count_rows() == 0
        finally:
            release.set()
            worker.cancel()
            assert worker.wait(5000)
            server_thread.join(3)
