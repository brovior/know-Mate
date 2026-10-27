"""Real Qt ownership and responsiveness across background exclusion stages."""
from __future__ import annotations

import json
import threading

import pytest
from PyQt6.QtCore import QEventLoop, QTimer

from knowmate.app.bridge import Bridge
from knowmate.collector import policy_service
from knowmate.tests.test_policy_worker import make_worker


def _until(predicate, timeout: int = 5000) -> None:
    """Run queued GUI events with a bounded deadline instead of GUI sleeps."""
    loop, poll, deadline = QEventLoop(), QTimer(), QTimer()
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
    assert predicate(), "background policy did not reach the expected boundary"


@pytest.mark.parametrize("count", [10, 150, 1000])
def test_gui_stays_responsive_and_owns_exclusion_until_actual_thread_exit(tmp_path, monkeypatch, count):
    """Slow deletes and final aggregation cannot freeze or prematurely unlock UI."""
    worker, paths, cfg = make_worker(tmp_path, monkeypatch, count)
    bridge = Bridge(collector_worker=worker)
    bridge._last_indexed = "previous index completion"
    delete_entered, release_delete = threading.Event(), threading.Event()
    counts_entered, release_counts = threading.Event(), threading.Event()
    terminal_entered, release_native = threading.Event(), threading.Event()
    original_delete, original_counts = worker._indexer.delete_file_chunks, policy_service._counts
    original_policy_run = worker._policy_job.run
    calls = []

    def delete(path):
        calls.append(threading.current_thread() is threading.main_thread())
        if len(calls) == 1:
            delete_entered.set()
            assert release_delete.wait(10)
        original_delete(path)

    def counts(owner):
        counts_entered.set()
        assert release_counts.wait(10)
        assert threading.current_thread() is not threading.main_thread()
        return original_counts(owner)

    def policy_run(owner):
        result = original_policy_run(owner)
        terminal_entered.set()
        assert release_native.wait(10)
        return result

    monkeypatch.setattr(worker._indexer, "delete_file_chunks", delete)
    monkeypatch.setattr(policy_service, "_counts", counts)
    monkeypatch.setattr(worker._policy_job, "run", policy_run)
    monkeypatch.setattr(bridge, "_compute_doc_mail_counts",
                        lambda *_a, **_k: pytest.fail("GUI attempted policy count projection"))
    beats = []
    heartbeat = QTimer()
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: beats.append(1))
    heartbeat.start()
    try:
        accepted = json.loads(bridge.startFailurePolicyJob("exclude", json.dumps(paths)))
        assert accepted["ok"] and accepted["accepted"]
        _until(lambda: delete_entered.is_set() and len(beats) >= 3)
        assert bridge.startReindex() == "policy_busy"
        assert bridge.retryFile(paths[0]) == "policy_busy"
        assert json.loads(bridge.excludeFiles(json.dumps(paths)))["error"] == "policy_busy"
        assert json.loads(bridge.saveSettings("{}"))["code"] == "policy_busy"
        assert json.loads(bridge.getIndexStatus())["status"] == "policy_running"
        assert len(json.loads(bridge.getFailures())) == count
        release_delete.set()
        _until(counts_entered.is_set)
        assert len(worker._indexer.deleted) == count
        assert json.loads(bridge.getFailurePolicyJobStatus())["active"]
        assert bridge.startReindex() == "policy_busy"
        release_counts.set()
        _until(terminal_entered.is_set)
        assert worker.policy_job_snapshot()["terminal"]
        bridge._poll_failure_updates()
        assert worker.isRunning()
        assert not json.loads(bridge.getFailurePolicyJobStatus())["terminal"]
        assert bridge.startReindex() == "policy_busy"
        release_native.set()
        _until(lambda: not worker.isRunning() and not bridge._policy.busy)
        final = json.loads(bridge.getFailurePolicyJobStatus())
        assert final["terminal"] and not final["active"]
        assert final["result"]["changed"] == count
        assert final["counts"]["doc_count"] == 0
        assert len(cfg["collector"]["exclude_files"]) == count
        assert bridge._last_indexed == "previous index completion"
        assert calls == [False] * count
        assert len(beats) >= 3
    finally:
        release_delete.set()
        release_counts.set()
        release_native.set()
        assert worker.wait(5000)
        heartbeat.stop()
        bridge._failure_timer.stop()
