"""OFF/basic diagnostics preserve real fake document/mail indexing results."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from knowmate.rag.embedding import VECTOR_DIM


def test_off_basic_preserve_chunks_embedding_cursor_failures_and_idle(tmp_path, caplog):
    """Compare stored projected rows and durable state across three actual cycles."""
    from knowmate.collector.scheduler import CollectorWorker
    from knowmate.rag.email_indexer import EmailIndexer
    from knowmate.rag.indexer import Indexer
    from knowmate.secure.fake_reader import FakeReader

    watch = tmp_path / "watch"
    watch.mkdir()
    for name in ("a.txt", "bad.txt"):
        (watch / name).write_text("synthetic document", encoding="utf-8")
    for index in range(3):
        (watch / f"mail{index}.eml").write_bytes((
            f"From: fake@example.com\r\nSubject: fixture{index}\r\n"
            f"Message-ID: <memory{index}@example.com>\r\n"
            f"Content-Type: text/plain; charset=UTF-8\r\n\r\n"
            f"synthetic mail body marker{index}"
        ).encode())
    (watch / "bad.eml").write_bytes(b"\x00" * 16)
    for index, path in enumerate(sorted(watch.iterdir())):
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))

    class Reader(FakeReader):
        def extract(self, path: str) -> str:
            if Path(path).name == "bad.txt":
                raise ValueError("synthetic unreadable fixture")
            return super().extract(path)

    class Embed:
        def __init__(self):
            self.calls = []

        def embed(self, texts):
            self.calls.append(list(texts))
            return [[0.0] * VECTOR_DIM for _ in texts]

    def run(enabled):
        root = tmp_path / ("basic" if enabled else "off")
        root.mkdir()
        embed = Embed()
        indexer = Indexer(root / "db", embed)
        emails = EmailIndexer(root / "db", embed)
        cfg = {
            "collector": {"watch_folders": [str(watch)], "memory_diagnostics_enabled": enabled, "memory_diagnostics_mode": "basic"},
            "chunking": {"chunk_size": 400, "overlap": 80},
            "cleanup": {"dry_run": True, "max_delete_ratio": 0.3},
            "mail": {"enabled": True, "extensions": [".eml"], "max_mails_per_scan": 2},
        }
        worker = CollectorWorker(
            cfg, indexer, Reader(), state_file=root / "documents.json", email_indexer=emails,
            mail_state_file=root / "mail.json", failure_file=root / "failures.json", purge_meta_file=root / "purge.json",
            get_now=lambda: 1_800_000_000, com_cleanup_fn=lambda **_: None,
        )
        errors = []
        worker.error.connect(errors.append)
        states = []
        for _ in range(3):
            worker.run()
            assert worker._memory_diagnostics is None and worker._work_phase == "idle"
            mail_state = json.loads((root / "mail.json").read_text(encoding="utf-8"))
            failures = json.loads((root / "failures.json").read_text(encoding="utf-8"))["files"]
            for record in failures.values():
                record.pop("last_failed_ts", None)
            documents = json.loads((root / "documents.json").read_text(encoding="utf-8"))
            states.append({
                "mail": mail_state,
                "failures": failures,
                "documents": {path: {key: value for key, value in item.items() if key in {"mtime", "size", "revision", "doc_uid"}} for path, item in documents.items()},
            })
        document_rows = indexer.table.search().select(["file_path", "chunk_index", "chunk_total", "text", "vector"]).limit(100).to_arrow().to_pylist()
        mail_rows = emails.table.search().select(["mail_uid", "chunk_index", "chunk_total", "text", "vector"]).limit(100).to_arrow().to_pylist()
        assert document_rows and len(mail_rows) == 3
        assert not errors
        assert {Path(path).name for path in states[-1]["failures"]} == {"bad.txt", "bad.eml"}
        return embed.calls, sorted(document_rows, key=lambda row: row["chunk_index"]), sorted(mail_rows, key=lambda row: row["mail_uid"]), states

    with caplog.at_level(logging.INFO, logger="knowmate.collector.memory_diagnostics"):
        off = run(False)
        caplog.clear()
        basic = run(True)
    assert off == basic
    diagnostics = [record.message for record in caplog.records if record.name == "knowmate.collector.memory_diagnostics"]
    assert diagnostics
    assert all("synthetic" not in line and "marker" not in line and str(tmp_path) not in line for line in diagnostics)
