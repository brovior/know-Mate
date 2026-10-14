"""Restore saved DB counts after deferred collector setup, without reindexing."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import lancedb
from PyQt6.QtCore import QObject, pyqtSignal

from knowmate.app.bridge import Bridge
from knowmate.tests.test_bridge import (
    _FakeWorker, _SAMPLE_CHUNKS, _SAMPLE_MAILS, _chunks_arrow, _emails_arrow,
)


class _SignalWorker(QObject):
    """Attach a count-only worker through the same signals as the real collector."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(str)
    indexing_needed = pyqtSignal(str)

    def __init__(self, indexer: object, email_indexer: object) -> None:
        super().__init__()
        self._indexer = indexer
        self._email_indexer = email_indexer
        self.last_cycle_changed = False

    def isRunning(self) -> bool:
        """No indexing runs while restored counts are read."""
        return False


def _fake_worker() -> _SignalWorker:
    """Provide duplicate chunks and deleted rows to check document counting."""
    source = _FakeWorker(_SAMPLE_CHUNKS, _SAMPLE_MAILS)
    return _SignalWorker(source._indexer, source._email_indexer)


def test_worker_attachment_refreshes_early_zero_count() -> None:
    bridge = Bridge()
    updates = []
    bridge.statusUpdated.connect(lambda value: updates.append(json.loads(value)))
    assert json.loads(bridge.getIndexStatus())["doc_count"] == 0

    worker = _fake_worker()
    bridge.set_worker(worker)

    assert len(updates) == 1
    assert updates[0]["local_count"] == 1
    assert updates[0]["shared_count"] == 1
    assert updates[0]["mail_count"] == 2
    assert updates[0]["doc_count"] == 2
    assert updates[0]["last_indexed"]
    assert worker._indexer.table.last_query_builder.requested_columns == [
        "file_path", "scope", "is_deleted", "indexed_at",
    ]


def test_unchanged_cycle_keeps_restored_counts_without_querying_again() -> None:
    bridge = Bridge()
    updates = []
    bridge.statusUpdated.connect(lambda value: updates.append(json.loads(value)))
    bridge.getIndexStatus()
    worker = _fake_worker()
    bridge.set_worker(worker)
    doc_query = worker._indexer.table.last_query_builder
    mail_query = worker._email_indexer.table.last_query_builder

    worker.finished.emit("인덱싱 완료 - 처리 0건")

    assert updates[-1]["local_count"] == 1
    assert updates[-1]["doc_count"] == 2
    assert updates[-1]["mail_count"] == 2
    assert worker._indexer.table.last_query_builder is doc_query
    assert worker._email_indexer.table.last_query_builder is mail_query


def test_restart_shows_already_committed_counts_from_reopened_lance_db(tmp_path: Path) -> None:
    # Counts come from committed DB rows, not the state checkpoint or a new scan.
    db = lancedb.connect(str(tmp_path / "index"))
    db.create_table("chunks", data=_chunks_arrow(_SAMPLE_CHUNKS))
    db.create_table("emails", data=_emails_arrow(_SAMPLE_MAILS))
    del db

    reopened = lancedb.connect(str(tmp_path / "index"))
    worker = _SignalWorker(
        SimpleNamespace(table=reopened.open_table("chunks")),
        SimpleNamespace(table=reopened.open_table("emails")),
    )
    bridge = Bridge()
    assert json.loads(bridge.getIndexStatus())["local_count"] == 0
    updates = []
    bridge.statusUpdated.connect(lambda value: updates.append(json.loads(value)))

    bridge.set_worker(worker)

    assert len(updates) == 1
    assert updates[0]["doc_count"] == 2
    assert updates[0]["local_count"] == 1
    assert updates[0]["mail_count"] == 2
