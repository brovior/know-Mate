"""Policy admission, metadata-only GUI reads and actual-thread completion."""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from knowmate.app.bridge import Bridge
from knowmate.app.failure_policy_controller import FailurePolicyController


class PolicyWorker:
    """Model native ownership independently from service terminal publication."""

    def __init__(self, failure_file=None, start_error="") -> None:
        """Create one unused metadata slot without filesystem access."""
        self.running = False
        self.pending = False
        self.snapshot = None
        self.start_error = start_error
        self.starts = self.acks = 0
        self._failure_file = failure_file
        self.failure_persistence_pending = False

    def request_policy_job(self, job_id: str, action: str, paths: tuple) -> str:
        """Reserve an immutable operation before native start."""
        self.pending = True
        self.snapshot = dict(job_id=job_id, action=action, revision=1, stage="reserved",
                             done=0, total=len(paths), terminal=False, result={}, counts=None,
                             settings_saved=False, policy_unknown=False)
        return "ok"

    def request_policy_reconcile(self, job_id: str) -> str:
        """Model a read-only confirmation command."""
        return self.request_policy_job(job_id, "reconcile", ())

    def start(self) -> None:
        """Allow either a confirmed failure or a late native-start exception."""
        self.starts += 1
        if self.start_error == "before":
            raise RuntimeError("synthetic pre-start error")
        self.running = True
        if self.start_error == "after":
            raise RuntimeError("synthetic post-start error")

    def isRunning(self) -> bool:
        """Return native execution separately from operation result availability."""
        return self.running

    def wait(self, timeout: int) -> bool:
        """Forbid a blocking wait from GUI admission and polling."""
        assert timeout == 0
        return not self.running

    def clear_policy_command(self, _job_id: str) -> bool:
        """Discard only the modeled never-started operation."""
        assert not self.running
        self.pending = False
        return True

    def acknowledge_policy_job(self, _job_id: str) -> bool:
        """Record release after actual native exit."""
        assert not self.running
        self.acks += 1
        self.pending = False
        return True

    def policy_job_snapshot(self) -> dict | None:
        """Copy the single latest observation."""
        return deepcopy(self.snapshot)

    def complete(self, **values) -> None:
        """Publish a terminal without ending the native execution."""
        self.snapshot.update(terminal=True, stage="complete", settings_saved=True,
                             revision=self.snapshot["revision"] + 1, **values)


def test_terminal_does_not_release_until_native_join_and_does_not_change_index_time(tmp_path):
    """Finalized scalar counts never query the GUI's database helper."""
    worker = PolicyWorker(tmp_path / "failure.json")
    bridge = Bridge(collector_worker=worker)
    bridge._last_indexed = "previous index"
    bridge._compute_doc_mail_counts = lambda *_a, **_k: pytest.fail("GUI DB query")
    finished = []
    bridge.indexFinished.connect(finished.append)
    accepted = json.loads(bridge.startFailurePolicyJob("exclude", '["a.txt"]'))
    assert accepted["accepted"] and bridge.policy_shutdown_active
    worker.complete(counts=dict(doc_count=3, local_count=2, shared_count=1, mail_count=4))
    bridge._poll_failure_updates()
    assert json.loads(bridge.getFailurePolicyJobStatus())["active"]
    assert bridge.startReindex() == "policy_busy"
    assert worker.acks == 0
    worker.running = False
    bridge._poll_failure_updates()
    bridge._poll_failure_updates()
    status = json.loads(bridge.getFailurePolicyJobStatus())
    assert status["terminal"] and not status["active"]
    assert worker.acks == 1 and finished == []
    assert bridge._last_indexed == "previous index"
    assert json.loads(bridge.getIndexStatus())["doc_count"] == 3
    bridge._failure_timer.stop()


def test_active_policy_blocks_every_conflicting_entrypoint_without_gui_io(tmp_path, monkeypatch):
    """Reload/read status can use warm metadata while no writer can overlap."""
    worker = PolicyWorker(tmp_path / "failure.json")
    bridge = Bridge(collector_worker=worker)
    json.loads(bridge.startFailurePolicyJob("exclude", '["a.txt"]'))
    cfg = {"collector": {"exclude_files": ["a.txt"], "watch_folders": ["docs"]}}
    monkeypatch.setattr("knowmate.config.get_cached_config", lambda: cfg)
    monkeypatch.setattr("knowmate.config.get_config", lambda: pytest.fail("GUI config load"))
    monkeypatch.setattr("knowmate.collector.failure_state.load_failures", lambda *_a: pytest.fail("GUI failure read"))
    bridge._compute_doc_mail_counts = lambda *_a, **_k: pytest.fail("GUI DB query")
    assert json.loads(bridge.getIndexStatus())["status"] == "policy_running"
    assert json.loads(bridge.getFolders()) == ["docs"]
    assert json.loads(bridge.getSettings())["collector"]["idle_enabled"] is True
    assert json.loads(bridge.getFailures())[0]["excluded"] is True
    assert bridge.retryFile("a.txt") == "policy_busy"
    assert json.loads(bridge.saveSettings('{}'))["code"] == "policy_busy"
    assert json.loads(bridge.excludeFiles('["b.txt"]'))["error"] == "policy_busy"
    assert json.loads(bridge.unexcludeFiles('["a.txt"]'))["error"] == "policy_busy"
    assert json.loads(bridge.addWatchFolder("other")) == ["docs"]
    assert json.loads(bridge.removeWatchFolder("docs")) == ["docs"]
    query_results = []
    bridge.responseReady.connect(lambda value: query_results.append(json.loads(value)))
    bridge.sendQuery(json.dumps(dict(query="synthetic", mode="knowledge", scopes=["local"],
                                    request_id="q", thread_id="t", view_generation=1)))
    assert query_results[0]["status"] == "rejected"
    assert worker.starts == 1
    bridge._failure_timer.stop()


@pytest.mark.parametrize("failure", ["before", "after"])
def test_start_exception_ownership_depends_on_actual_native_execution(failure):
    """A late start exception must not reopen admission over an active writer."""
    controller = FailurePolicyController()
    worker = PolicyWorker(start_error=failure)
    admission = controller.start(worker, "exclude", ("a.txt",))
    if failure == "before":
        assert not admission["accepted"]
        assert not controller.busy and not worker.pending
    else:
        assert admission["accepted"] and controller.busy
        worker.complete()
        assert controller.poll()[1] is None
        worker.running = False
        assert controller.poll()[1]["terminal"]


def test_uncertain_policy_latch_survives_join_and_only_reconcile_clears_it():
    """A finished writer does not make unconfirmed durable settings safe to use."""
    controller = FailurePolicyController()
    worker = PolicyWorker()
    controller.start(worker, "exclude", ("a.txt",))
    worker.complete(policy_unknown=True)
    worker.running = False
    controller.poll()
    assert controller.blocked and not controller.busy
    assert controller.start(worker, "unexclude", ("a.txt",))["error"] == "policy_busy"
    assert controller.start(worker, "reconcile", ())["accepted"]
    worker.complete(policy_unknown=False)
    worker.running = False
    controller.poll()
    assert not controller.blocked


def test_pending_preparation_blocks_policy_without_starting_a_thread(tmp_path):
    """Exclusion admission cannot steal an already queued PPT indexing request."""
    worker = PolicyWorker(tmp_path / "failure.json")
    bridge = Bridge(main_window=SimpleNamespace(_ppt_preparation=SimpleNamespace(start_pending=True)),
                    collector_worker=worker)
    assert json.loads(bridge.startFailurePolicyJob("exclude", '["a.txt"]'))["error"] == "busy"
    assert worker.starts == 0
    bridge._failure_timer.stop()


def test_policy_start_does_not_finalize_an_unobserved_previous_index_cycle(tmp_path):
    """Admission stays I/O-free when a normal worker has stopped before its GUI callback."""
    worker = PolicyWorker(tmp_path / "failure.json")
    worker.failure_cycle_id = 1
    worker.drain_failure_updates = lambda: pytest.fail("admission drained a normal cycle")
    bridge = Bridge(collector_worker=worker)
    bridge._compute_doc_mail_counts = lambda *_a, **_k: pytest.fail("admission performed GUI DB projection")
    response = json.loads(bridge.startFailurePolicyJob("exclude", '["a.txt"]'))
    assert response["error"] == "busy"
    assert worker.starts == 0
    bridge._failure_timer.stop()


def test_reservation_exception_retains_lease_until_command_rollback_is_confirmed():
    """A partially installed command cannot outlive an incorrectly released GUI lease."""
    controller = FailurePolicyController()
    worker = PolicyWorker()
    reserve = worker.request_policy_job
    clear = worker.clear_policy_command
    attempts = []

    def reserve_then_raise(*args):
        """Model failure after the command has been installed."""
        reserve(*args)
        raise RuntimeError("reservation published before error")

    def delayed_clear(job_id):
        """The first rollback failure must leave admission closed."""
        attempts.append(job_id)
        return False if len(attempts) == 1 else clear(job_id)

    worker.request_policy_job = reserve_then_raise
    worker.clear_policy_command = delayed_clear
    admission = controller.start(worker, "exclude", ("a.txt",))
    assert admission["accepted"] and controller.busy
    assert worker.starts == 0
    assert controller.poll()[1]["terminal"]
    assert not controller.busy and not worker.pending


@pytest.mark.parametrize("clear_failure", ["false", "throw"])
def test_native_nonstart_retries_failed_command_clear(clear_failure):
    """A transient rollback failure after start raises cannot strand the lease."""
    controller = FailurePolicyController()
    worker = PolicyWorker(start_error="before")
    clear = worker.clear_policy_command
    attempts = []

    def transient_clear(job_id: str) -> bool:
        """Fail the first cleanup observation and succeed on a later GUI poll."""
        attempts.append(job_id)
        if len(attempts) == 1:
            if clear_failure == "throw":
                raise RuntimeError("synthetic cleanup observation failure")
            return False
        return clear(job_id)

    worker.clear_policy_command = transient_clear
    admission = controller.start(worker, "exclude", ("a.txt",))
    assert admission["accepted"] and controller.busy
    assert not worker.running
    terminal = controller.poll()[1]
    assert terminal["terminal"] and not terminal["active"]
    assert terminal["settings_saved"] is False and terminal["status"] == "error"
    assert not worker.pending and not controller.busy


def test_late_start_exception_uses_real_terminal_when_started_command_cannot_clear():
    """An already executed command must retain its result and end through acknowledgement."""
    controller = FailurePolicyController()
    worker = PolicyWorker(start_error="after")
    worker.clear_policy_command = lambda _job_id: False
    assert controller.start(worker, "exclude", ("a.txt",))["accepted"]
    worker.complete()
    assert controller.poll()[1] is None
    worker.running = False
    terminal = controller.poll()[1]
    assert terminal["terminal"] and terminal["settings_saved"] is True
    assert worker.acks == 1 and not controller.busy


@pytest.mark.parametrize("boundary", ["isRunning", "wait", "acknowledge_policy_job"])
def test_native_completion_observation_errors_retain_lease_and_retry(boundary):
    """Transient Qt status/join/ack errors must never escape the GUI poll callback."""
    controller = FailurePolicyController()
    worker = PolicyWorker()
    controller.start(worker, "exclude", ("a.txt",))
    worker.complete()
    worker.running = False
    original = getattr(worker, boundary)
    attempts = []

    def transient(*args):
        """Fail one observation without changing the modeled native ownership."""
        attempts.append(args)
        if len(attempts) == 1:
            raise RuntimeError("synthetic native observation failure")
        return original(*args)

    setattr(worker, boundary, transient)
    assert controller.poll()[1] is None
    assert controller.busy and controller.status()["active"]
    assert controller.status()["error_code"] == "observation_failed"
    terminal = controller.poll()[1]
    assert terminal["terminal"] and not controller.busy
    assert worker.acks == 1
