"""First-worker gate and detached setup lifetime, without Windows or Office."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication
import pytest

from knowmate.app.bridge import Bridge
from knowmate.app.ppt_preparation import PptPreparationCoordinator, preparation_policy
from knowmate.secure.ppt_installation import REQUIRED_VERSION, SetupResult


class Worker:
    def __init__(self):
        self.started = 0
        self.retried = 0
        self.running = False

    def isRunning(self):
        return self.running

    def request_failure_retry(self):
        self.retried += 1

    def start(self):
        self.started += 1
        self.running = True


class Process:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.started = self.closed = 0

    def start(self):
        self.started += 1

    def poll(self):
        return self.exit_code

    def close(self):
        self.closed += 1

    def kill(self):
        raise AssertionError("setup must outlive observer")

    terminate = kill


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        QApplication.instance().processEvents()
        time.sleep(0.001)
    assert predicate()


def coordinator(tmp_path, **kwargs):
    policies = []
    workers = []
    def make_worker():
        assert policies  # policy is applied before CollectorWorker construction
        worker = Worker()
        workers.append(worker)
        return worker
    gate = PptPreparationCoordinator(
        extractor=SimpleNamespace(set_ppt_preparation_policy=policies.append),
        extractor_mode="auto", reader_mode=kwargs.pop("reader_mode", "auto"),
        data_dir=tmp_path, make_worker=make_worker, platform=kwargs.pop("platform", "win32"),
        **kwargs,
    )
    return gate, policies, workers


@pytest.mark.parametrize("mode", ["auto", "addin", "com"])
@pytest.mark.parametrize("status", ["ready", "missing", "failed", "disabled", "conflict", "unknown"])
@pytest.mark.parametrize("safe", [True, False])
def test_machine_safety_precedes_mode(mode, status, safe):
    result = SetupResult(status=status, machine_safe=safe)
    expected = ("ppt_pending" if not safe else "com_only" if mode == "com" else
                "ready" if status == "ready" else "ppt_pending" if mode == "addin" else "com_only")
    assert preparation_policy(mode, result) == expected


def test_check_is_async_and_all_requests_coalesce(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def check():
        entered.set()
        assert release.wait(3)
        return SetupResult(status="ready", machine_safe=True)
    gate, policies, workers = coordinator(tmp_path, checker=check)
    gate.begin()
    assert entered.wait(1)
    gate.request_start()
    gate.request_start(retry_failures=True)
    gate.request_start()
    assert not workers and not policies
    release.set()
    wait_until(lambda: bool(workers))
    assert policies == ["ready"]
    assert workers[0].started == workers[0].retried == 1
    gate.request_start(retry_failures=True)
    assert workers[0].started == 1
    gate.shutdown()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_non_windows_skips_setup(tmp_path, platform):
    def check():
        raise AssertionError("no installation check on non-Windows")
    gate, policies, workers = coordinator(tmp_path, checker=check, platform=platform)
    gate.begin()
    assert policies == ["ready"] and len(workers) == 1
    assert workers[0].started == 0  # installation readiness does not enable indexing
    gate.shutdown()


@pytest.mark.parametrize("mode,expected", [("auto", "com_only"), ("addin", "ppt_pending"), ("com", "com_only")])
def test_prototype_gate_never_launches_helper(tmp_path, mode, expected):
    def factory():
        raise AssertionError("Windows prototype has not been validated")
    gate, policies, _ = coordinator(tmp_path, reader_mode=mode, process_factory=factory,
        checker=lambda: SetupResult(status="missing", machine_safe=True))
    gate.begin()
    wait_until(lambda: bool(policies))
    assert policies == [expected]
    gate.shutdown()


@pytest.mark.parametrize("ending", ["timeout", "shutdown"])
def test_installation_observer_never_kills_child(tmp_path, ending):
    process = Process()
    gate, policies, workers = coordinator(tmp_path, process_factory=lambda: process,
        checker=lambda: SetupResult(status="missing", machine_safe=True, auto_install_validated=True),
        observation_sec=60)
    gate.begin()
    wait_until(lambda: process.started == 1)
    gate.request_start()
    assert not workers
    if ending == "timeout":
        gate._deadline = 0
        gate._poll_setup()
        assert policies == ["ppt_pending"] and workers[0].started == 1
    else:
        gate.shutdown()
        assert not policies and not workers
    assert process.closed == 1
    gate.shutdown()


def test_helper_exit_requires_safe_recheck(tmp_path):
    results = iter([SetupResult(status="missing", machine_safe=True, auto_install_validated=True),
                    SetupResult(status="ready", machine_safe=False)])
    process = Process(exit_code=0)
    gate, policies, _ = coordinator(tmp_path, checker=lambda: next(results), process_factory=lambda: process)
    gate.begin()
    wait_until(lambda: process.started == 1)
    gate._poll_setup()
    wait_until(lambda: bool(policies))
    assert policies == ["ppt_pending"]
    gate.shutdown()


def test_decline_is_persisted_for_original_user_version(tmp_path):
    result = SetupResult(status="missing", machine_safe=True, auto_install_validated=True)
    process = Process(exit_code=11)
    gate, policies, _ = coordinator(tmp_path, checker=lambda: result, process_factory=lambda: process)
    gate.begin()
    wait_until(lambda: process.started == 1)
    gate._poll_setup()
    wait_until(lambda: bool(policies))
    assert policies == ["com_only"]
    assert json.loads((tmp_path / "ppt_reader_preparation.json").read_text()) == {
        "version": REQUIRED_VERSION, "reason": "permission_declined"}
    gate.shutdown()
    second, second_policy, _ = coordinator(tmp_path, checker=lambda: result,
        process_factory=lambda: pytest.fail("must not repeat version's UAC"))
    second.begin()
    wait_until(lambda: bool(second_policy))
    assert second_policy == ["com_only"]
    second.shutdown()


def test_shutdown_during_read_only_check_does_not_start_worker(tmp_path):
    release = threading.Event()
    def check():
        assert release.wait(3)
        return SetupResult(status="ready", machine_safe=True)
    gate, policies, workers = coordinator(tmp_path, checker=check)
    gate.begin()
    gate.request_start(retry_failures=True)
    gate.shutdown()
    release.set()
    for _ in range(5):
        QApplication.instance().processEvents()
        time.sleep(0.001)
    assert not policies and not workers


def test_bridge_manual_and_targeted_retry_use_gate_before_worker(tmp_path, monkeypatch):
    from knowmate.collector import failure_state
    monkeypatch.setattr("knowmate.config.get_data_dir", lambda: tmp_path)
    records = {}
    target = str(tmp_path / "broken.ppt")
    failure_state.note_failure(records, target, failure_state.KIND_UNKNOWN_TRANSIENT, None, None,
                               mtime=1, size=1, now=1000)
    failure_state.save_failures(tmp_path / "index_failure.json", records)
    starts = []
    bridge = Bridge(main_window=SimpleNamespace(_ppt_preparation=object(),
                        _request_index_start=lambda **kwargs: starts.append(kwargs)))
    bridge.startReindex()
    assert bridge.retryFile(target) == "ok"
    assert starts == [{"retry_failures": True}, {}]
    assert failure_state.load_failures(tmp_path / "index_failure.json")[target].force_retry


def test_tray_and_idle_share_start_gate():
    from knowmate.app.main import MainWindow
    starts = []
    fake = SimpleNamespace(_request_index_start=lambda **kwargs: starts.append(kwargs))
    MainWindow._tray_reindex(fake)
    MainWindow._trigger_idle_index(fake)
    assert starts == [{"retry_failures": True}, {}]


def test_queued_worker_construction_exception_is_reported(tmp_path):
    def failed_factory():
        raise RuntimeError("construction_failure")
    gate = PptPreparationCoordinator(
        extractor=SimpleNamespace(), extractor_mode="fake", reader_mode="auto",
        data_dir=tmp_path, make_worker=failed_factory, platform="linux")
    messages = []
    gate.failed.connect(messages.append)
    gate.request_start()
    gate.begin()
    assert messages == ["수집기가 초기화되지 않았습니다."]
    assert gate._worker is None
    gate.shutdown()


def test_cancel_removes_pending_intent_without_stopping_setup(tmp_path):
    process = Process()
    gate, policies, workers = coordinator(tmp_path, process_factory=lambda: process,
        checker=lambda: SetupResult(status="missing", machine_safe=True, auto_install_validated=True))
    gate.begin()
    wait_until(lambda: process.started == 1)
    gate.request_start(retry_failures=True)
    gate.cancel_start()
    gate._deadline = 0
    gate._poll_setup()
    assert policies == ["ppt_pending"]
    assert workers[0].started == workers[0].retried == 0
    assert process.closed == 1
    gate.shutdown()


@pytest.mark.parametrize("contents", ["{broken", '{"version":"1.0.0.0","reason":[]}'])
def test_corrupt_user_record_suppresses_same_run_install(tmp_path, contents):
    (tmp_path / "ppt_reader_preparation.json").write_text(contents)
    gate, policies, _ = coordinator(tmp_path,
        checker=lambda: SetupResult(status="missing", machine_safe=True, auto_install_validated=True),
        process_factory=lambda: pytest.fail("corrupt record must not cause repeated UAC"))
    gate.begin()
    wait_until(lambda: bool(policies))
    assert policies == ["com_only"]
    gate.shutdown()


def test_future_required_version_can_request_once(tmp_path):
    (tmp_path / "ppt_reader_preparation.json").write_text(
        json.dumps({"version": "0.9.0.0", "reason": "permission_declined"}))
    process = Process()
    gate, _, _ = coordinator(tmp_path,
        checker=lambda: SetupResult(status="outdated", machine_safe=True, auto_install_validated=True),
        process_factory=lambda: process)
    gate.begin()
    wait_until(lambda: process.started == 1)
    gate.begin()
    assert process.started == 1
    gate.shutdown()


def test_init_failure_keeps_manual_start_alert():
    from knowmate.app.main import MainWindow
    window = SimpleNamespace()
    window._request_index_start = lambda **kwargs: MainWindow._request_index_start(window, **kwargs)
    bridge = Bridge(main_window=window)
    window._bridge = bridge
    alerts = []
    bridge.indexAlert.connect(alerts.append)
    bridge.startReindex()
    assert alerts == ["수집기가 초기화되지 않았습니다."]


def test_no_worker_during_preparation_preserves_start_instead_of_alert():
    from knowmate.app.main import MainWindow
    starts = []
    window = SimpleNamespace(_ppt_preparation=SimpleNamespace(request_start=lambda **kwargs: starts.append(kwargs)))
    window._request_index_start = lambda **kwargs: MainWindow._request_index_start(window, **kwargs)
    bridge = Bridge(main_window=window)
    window._bridge = bridge
    alerts = []
    bridge.indexAlert.connect(alerts.append)
    bridge.startReindex()
    assert starts == [{"retry_failures": True}]
    assert alerts == []


def test_init_failure_retry_is_error_without_modifying_failure_sidecar(tmp_path, monkeypatch):
    from knowmate.collector import failure_state
    from knowmate.app.main import MainWindow
    monkeypatch.setattr("knowmate.config.get_data_dir", lambda: tmp_path)
    records = {}
    target = str(tmp_path / "failed.ppt")
    failure_state.note_failure(records, target, failure_state.KIND_READ_ERROR, "read", None, 1, 1, 100)
    failure_file = tmp_path / "index_failure.json"
    failure_state.save_failures(failure_file, records)
    before = failure_file.read_bytes()
    window = SimpleNamespace()
    window._request_index_start = lambda **kwargs: MainWindow._request_index_start(window, **kwargs)
    bridge = Bridge(main_window=window)
    window._bridge = bridge
    alerts = []
    bridge.indexAlert.connect(alerts.append)
    assert bridge.retryFile(target) == "error"
    assert alerts == ["수집기가 초기화되지 않았습니다."]
    assert failure_file.read_bytes() == before
