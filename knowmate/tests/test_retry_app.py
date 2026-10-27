"""Exercise failure metadata admission and GUI lifecycle without Office."""
from __future__ import annotations

import json
from types import SimpleNamespace

from knowmate.app.bridge import Bridge
from knowmate.app.failure_view import FailureView
from knowmate.collector import failure_state


class MailboxWorker:
    """Expose copied envelopes and controllable native thread completion."""

    last_cycle_changed = False

    def __init__(self, failure_file) -> None:
        """Start with a live cycle and no unsaved failure state."""
        self._failure_file = failure_file
        self.running = True
        self.failure_persistence_pending = False
        self.envelope = None
        self.saves = 0
        self.starts = 0

    def isRunning(self) -> bool:
        """Return the independently controlled native thread state."""
        return self.running

    def drain_failure_updates(self) -> dict | None:
        """Retain the latest terminal as the production mailbox does."""
        result = self.envelope
        if result is not None:
            self.envelope = dict(result, baseline=None, changes={})
        return result

    def request_failure_persist(self) -> bool:
        """Reserve one storage-only operation."""
        self.saves += 1
        return True

    def start(self) -> None:
        """Mark the reserved operation running."""
        self.starts += 1
        self.running = True


def envelope(*, cycle=1, revision=0, baseline=None, changes=None, terminal=None) -> dict:
    """Build a metadata-only mailbox observation."""
    return dict(cycle_id=cycle, revision=revision, baseline=baseline,
                changes=changes or {}, terminal=terminal, persisted_revision=revision)


def test_running_results_override_old_disk_and_finish_waits_for_native_exit(tmp_path, monkeypatch) -> None:
    """Show A's success while B and the final COM cleanup are still active."""
    monkeypatch.setattr("knowmate.config.get_config", lambda: {"collector": {}})
    path = str(tmp_path / "a.pptx")
    records = {}
    failure_state.note_failure(records, path, failure_state.KIND_UNKNOWN_TRANSIENT,
                               "read", None, mtime=1, size=1, now=1)
    failure_file = tmp_path / "failures.json"
    failure_state.save_failures(failure_file, records)
    worker = MailboxWorker(failure_file)
    bridge = Bridge(collector_worker=worker)
    bridge._last_indexed = "previous complete scan"
    finished = []
    bridge.indexFinished.connect(finished.append)
    worker.envelope = envelope(baseline=records)
    bridge._poll_failure_updates()
    assert len(json.loads(bridge.getFailures())) == 1
    worker.envelope = envelope(revision=1, changes={path: None})
    bridge._poll_failure_updates()
    assert json.loads(bridge.getFailures()) == []
    assert path in failure_state.load_failures(failure_file)
    terminal = dict(status="ok", scope="single", message="one finished", persisted=True)
    worker.envelope = envelope(revision=1, terminal=terminal)
    bridge._poll_failure_updates()
    assert finished == []
    assert bridge.retryFile(path) == "busy"
    worker.running = False
    bridge._poll_failure_updates()
    bridge._poll_failure_updates()
    assert finished == ["one finished"]
    assert bridge._last_indexed == "previous complete scan"


def test_storage_failure_retains_overlay_and_blocks_every_writer(tmp_path) -> None:
    """Schedule storage-only retry instead of silently launching another scan."""
    worker = MailboxWorker(tmp_path / "failures.json")
    worker.running = False
    worker.failure_persistence_pending = True
    bridge = Bridge(collector_worker=worker)
    assert bridge.retryFile("a.pptx") == "persistence_pending"
    assert worker.starts == worker.saves == 1
    assert bridge.startReindex() == "busy"
    assert json.loads(bridge.excludeFiles('["a.pptx"]'))["error"] == "busy"
    assert json.loads(bridge.unexcludeFiles('["a.pptx"]'))["error"] == "busy"
    assert worker.saves == 1


def test_queued_start_blocks_exclusion_mutation(tmp_path) -> None:
    """Do not change exclusions after a single-file request was queued."""
    window = SimpleNamespace(_ppt_preparation=SimpleNamespace(start_pending=True))
    bridge = Bridge(main_window=window)
    assert json.loads(bridge.excludeFiles('["a.pptx"]'))["error"] == "busy"
    assert json.loads(bridge.unexcludeFiles('["a.pptx"]'))["error"] == "busy"


def test_old_cycle_does_not_remove_new_failure_and_error_terminal_finishes(tmp_path) -> None:
    """Ignore stale cycle messages and finalize an outer exception once."""
    worker = MailboxWorker(tmp_path / "failures.json")
    view = FailureView()
    worker.envelope = envelope(cycle=2, baseline={"new": "scalar"})
    assert view.poll(worker)[0]
    worker.envelope = envelope(cycle=1, revision=99, changes={"new": None})
    assert view.poll(worker) == (False, None)
    assert view.records == {"new": "scalar"}
    worker.running = False
    worker.envelope = envelope(cycle=2, terminal=dict(status="error", scope="all", persisted=False))
    assert view.poll(worker)[1]["status"] == "error"
    assert view.poll(worker) == (False, None)


def test_repeated_busy_clicks_cannot_bypass_refresh_frequency(tmp_path, monkeypatch) -> None:
    """Drain current metadata while retaining at most one notification per 500ms."""
    clock = [0.0]
    monkeypatch.setattr("knowmate.app.bridge.time.monotonic", lambda: clock[0])
    worker = MailboxWorker(tmp_path / "failures.json")
    bridge = Bridge(collector_worker=worker)
    notifications = []
    bridge.failuresChanged.connect(lambda: notifications.append(clock[0]))
    worker.envelope = envelope(baseline={})
    assert bridge._worker_write_guard() == "busy"
    for revision in range(1, 101):
        worker.envelope = envelope(revision=revision, changes={"a": None})
        assert bridge._worker_write_guard() == "busy"
    assert notifications == [0.0]
    clock[0] = 0.5
    bridge._poll_failure_updates()
    assert notifications == [0.0, 0.5]


def test_cancel_queued_preparation_finishes_ui_once_without_index_timestamp() -> None:
    """Cancel a start before a collector exists without leaving the running UI stuck."""
    preparation = SimpleNamespace(start_pending=True)
    preparation.cancel_start = lambda: setattr(preparation, "start_pending", False)
    bridge = Bridge(main_window=SimpleNamespace(_ppt_preparation=preparation))
    bridge._last_indexed = "previous scan"
    finished = []
    bridge.indexFinished.connect(finished.append)
    bridge.cancelReindex()
    bridge.cancelReindex()
    assert not preparation.start_pending
    assert finished == ["인덱싱 시작 요청을 취소했습니다."]
    assert bridge._last_indexed == "previous scan"


def test_cancel_active_worker_waits_for_actual_cleanup_terminal(tmp_path) -> None:
    """Keep an active indexing operation busy until its normal terminal arrives."""
    worker = MailboxWorker(tmp_path / "failures.json")
    cancelled = []
    worker.cancel = lambda: cancelled.append(True)
    preparation = SimpleNamespace(start_pending=False, cancel_start=lambda: None)
    bridge = Bridge(main_window=SimpleNamespace(_ppt_preparation=preparation),
                    collector_worker=worker)
    finished = []
    bridge.indexFinished.connect(finished.append)
    bridge.cancelReindex()
    assert cancelled == [True]
    assert finished == []
    assert bridge._worker_write_guard() == "busy"
