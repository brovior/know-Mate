"""Exercise live failure refresh and cleanup admission with a real Qt worker."""
from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEventLoop, QTimer

from knowmate.app.bridge import Bridge
from knowmate.collector import failure_state
from knowmate.collector.failure_updates import FailureUpdates
from knowmate.collector.scheduler import CollectorWorker
from knowmate.collector.scanner import normalize_path_key, validate_retry_target


def _until(predicate, timeout: int = 4000) -> None:
    """Drive queued Qt events until a bounded observable condition is met."""
    loop = QEventLoop()
    poll, deadline = QTimer(), QTimer()
    poll.setInterval(5)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll.start()
    deadline.start(timeout)
    if not predicate():
        loop.exec()
    poll.stop()
    deadline.stop()
    assert predicate(), "Qt integration condition timed out"


def test_live_refresh_precedes_cycle_end_and_cleanup_keeps_admission_closed(tmp_path, monkeypatch):
    """A resolved card disappears while B is blocked; cleanup still owns execution."""
    source_a, source_b = str(tmp_path / "a.txt"), str(tmp_path / "b.txt")
    failure_file = tmp_path / "failure.json"
    records = {}
    for path in (source_a, source_b):
        failure_state.note_failure(records, path, failure_state.KIND_READ_ERROR,
                                   "read", None, 1.0, 1, 1000.0)
    assert failure_state.save_failures(failure_file, records)
    monkeypatch.setattr("knowmate.config.get_config", lambda: {"collector": {}})
    body_entered, release_body = threading.Event(), threading.Event()
    cleanup_entered, release_cleanup = threading.Event(), threading.Event()

    class Scope:
        active = True

        def __enter__(self):
            return self

        def close(self):
            self.active = False

    monkeypatch.setattr("knowmate.secure.com_scope.ComScope", Scope)
    worker = CollectorWorker(
        config={"collector": {}}, indexer=SimpleNamespace(), extractor=None,
        state_file=tmp_path / "state.json", failure_file=failure_file,
        purge_meta_file=tmp_path / "purge.json",
    )
    worker.last_cycle_changed = False

    def cycle():
        failure_state.note_success(worker._failure_updates.records, source_a)
        body_entered.set()
        assert release_body.wait(8), "test body was not released"

    def cleanup(_phase):
        cleanup_entered.set()
        assert release_cleanup.wait(8), "test cleanup was not released"

    monkeypatch.setattr(worker, "_run_cycle", cycle)
    monkeypatch.setattr(worker, "_cleanup_com_apps", cleanup)
    bridge = Bridge(collector_worker=worker)
    notifications, finishes = [], []
    bridge.failuresChanged.connect(lambda: notifications.append(json.loads(bridge.getFailures())))
    bridge.indexFinished.connect(finishes.append)
    try:
        worker.start()
        _until(lambda: body_entered.is_set() and bool(notifications))
        assert worker.isRunning()
        assert [card["path"] for card in notifications[-1]] == [source_b]
        assert source_a in failure_state.load_failures(failure_file)
        assert not finishes
        release_body.set()
        _until(cleanup_entered.is_set)
        assert bridge.retryFile(source_b) == "busy"
        assert not finishes
        release_cleanup.set()
        _until(lambda: bool(finishes) and not worker.isRunning())
        assert source_a not in failure_state.load_failures(failure_file)
        assert len(finishes) == 1
    finally:
        release_body.set()
        release_cleanup.set()
        assert worker.wait(4000)
        bridge._failure_timer.stop()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows path alias admission")
@pytest.mark.parametrize("alias", ["case", "slash", "extended"])
def test_windows_target_aliases_keep_exclusion_and_root_policy(tmp_path, alias):
    """Equivalent target spellings must not bypass a normal-path exclusion."""
    root = tmp_path / "docs"
    root.mkdir()
    source = root / "selected.txt"
    source.write_text("synthetic", encoding="utf-8")
    path = str(source)
    if alias == "case":
        path = path.upper()
    elif alias == "slash":
        path = path.replace("\\", "/")
    else:
        path = "\\\\?\\" + path
    assert validate_retry_target(path, [str(root)])[0] == "ok"
    status, meta = validate_retry_target(
        path, [str(root)], exclude_files=frozenset({normalize_path_key(str(source))}),
    )
    assert status == "excluded"
    assert meta is None


def test_slow_initial_load_cannot_publish_an_empty_failure_snapshot(tmp_path, monkeypatch):
    """The GUI keeps its previous list until the worker baseline is complete."""
    records = {}
    failure_state.note_failure(records, "synthetic.txt", failure_state.KIND_READ_ERROR,
                               "read", None, 1.0, 1, 1000.0)
    loading, release = threading.Event(), threading.Event()

    def load(_path):
        loading.set()
        assert release.wait(4)
        return records

    monkeypatch.setattr(failure_state, "load_failures", load)
    updates = FailureUpdates(tmp_path / "failure.json")
    thread = threading.Thread(target=updates.begin, args=("all",))
    try:
        thread.start()
        assert loading.wait(2)
        assert updates.drain() is None
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert updates.drain()["baseline"] == records
    finally:
        release.set()
        thread.join(4)


def test_storage_retry_keeps_latest_cards_without_replaying_rejected_request(tmp_path, monkeypatch):
    """A real storage-only run repairs disk and does not execute the original retry."""
    source = str(tmp_path / "a.txt")
    failure_file = tmp_path / "failure.json"
    records = {}
    failure_state.note_failure(records, source, failure_state.KIND_READ_ERROR,
                               "read", None, 1.0, 1, 1000.0)
    save = failure_state.save_failures
    assert save(failure_file, records)
    monkeypatch.setattr("knowmate.config.get_config", lambda: {"collector": {}})
    worker = CollectorWorker(
        config={"collector": {}}, indexer=SimpleNamespace(), extractor=None,
        state_file=tmp_path / "state.json", failure_file=failure_file,
        purge_meta_file=tmp_path / "purge.json",
    )
    worker.last_cycle_changed = False
    calls = []

    def cycle():
        calls.append("index")
        failure_state.note_success(worker._failure_updates.records, source)

    monkeypatch.setattr(worker, "_run_cycle", cycle)
    monkeypatch.setattr(failure_state, "save_failures", lambda *_args: False)
    worker.run()
    assert worker.failure_persistence_pending
    assert source in failure_state.load_failures(failure_file)
    bridge = Bridge(collector_worker=worker)
    try:
        bridge._poll_failure_updates()
        assert json.loads(bridge.getFailures()) == []
        monkeypatch.setattr(failure_state, "save_failures", save)
        assert bridge.retryFile(source) == "persistence_pending"
        _until(lambda: not worker.isRunning() and not worker.failure_persistence_pending)
        bridge._poll_failure_updates()
        assert failure_state.load_failures(failure_file) == {}
        assert json.loads(bridge.getFailures()) == []
        assert calls == ["index"]
    finally:
        assert worker.wait(4000)
        bridge._failure_timer.stop()
