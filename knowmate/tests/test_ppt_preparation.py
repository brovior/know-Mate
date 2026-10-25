"""First-worker gate and detached setup lifetime, without Windows or Office."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication
import pytest

from knowmate.app.bridge import Bridge
from knowmate.secure import ppt_installation
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

    def read_result(self):
        return SetupResult(reason="diagnostic_unavailable")


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        QApplication.instance().processEvents()
        time.sleep(0.001)
    assert predicate()


@pytest.fixture(autouse=True)
def candidate_policy(monkeypatch):
    monkeypatch.setattr(ppt_installation, "auto_install_mode", lambda: "candidate")


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
        checker=lambda: SetupResult(status="missing", machine_safe=True, office_bitness=64, auto_install_mode="candidate"),
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
    results = iter([SetupResult(status="missing", machine_safe=True, office_bitness=64, auto_install_mode="candidate"),
                    SetupResult(status="ready", machine_safe=False)])
    process = Process(exit_code=0)
    gate, policies, _ = coordinator(tmp_path, checker=lambda: next(results), process_factory=lambda: process)
    gate.begin()
    wait_until(lambda: process.started == 1)
    gate._poll_setup()
    wait_until(lambda: bool(policies))
    assert policies == ["ppt_pending"]
    gate.shutdown()


@pytest.mark.parametrize("failure", ["none", "read", "close"])
def test_diagnostic_failures_do_not_replace_independent_postcheck(tmp_path, caplog, failure):
    class DiagnosticProcess(Process):
        def read_result(self):
            if failure == "read":
                raise OSError("confidential-marker")
            return SetupResult(status="failed", reason="acl_unavailable", installed_version="1.0.0.0",
                machine_safe=False, stage="parent_policy", error_type="policy")

        def close(self):
            super().close()
            if failure == "close":
                raise OSError("confidential-marker")

    result = SetupResult(status="outdated", reason="version_old", installed_version="1.0.0.0",
        machine_safe=True, office_bitness=64, auto_install_mode="candidate")
    process = DiagnosticProcess(exit_code=12)
    checks = []
    def check():
        checks.append(1)
        return result
    gate, policies, _ = coordinator(tmp_path, checker=check, process_factory=lambda: process)
    with caplog.at_level("INFO"):
        gate.begin()
        wait_until(lambda: process.started == 1)
        gate._poll_setup()
        wait_until(lambda: bool(policies))
    assert len(checks) == 2 and policies == ["com_only"]
    assert process.closed == 1 and "confidential-marker" not in caplog.text
    assert f"required_version={REQUIRED_VERSION} installed_version=1.0.0.0" in caplog.text
    if failure == "read":
        assert "reason=diagnostic_unavailable" in caplog.text
    else:
        assert "reason=acl_unavailable stage=parent_policy error_type=policy winerror=None" in caplog.text
    assert not (tmp_path / "ppt_reader_preparation.json").exists()
    gate.shutdown()


def test_invalid_or_forged_helper_ready_does_not_override_postcheck(tmp_path):
    class ReadyProcess(Process):
        def read_result(self):
            return SetupResult(status="ready", reason="registration_ok", machine_safe=True)
    process = ReadyProcess(exit_code=0)
    checks = iter([SetupResult(status="outdated", machine_safe=True, office_bitness=64,
        auto_install_mode="candidate"), SetupResult(machine_safe=False)])
    gate, policies, _ = coordinator(tmp_path, checker=lambda: next(checks), process_factory=lambda: process)
    gate.begin()
    wait_until(lambda: process.started == 1)
    gate._poll_setup()
    wait_until(lambda: bool(policies))
    assert policies == ["ppt_pending"]
    gate.shutdown()


@pytest.mark.parametrize("safe", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_native_helper_diagnostic_logs_fixed_fields_and_always_rechecks(tmp_path, caplog, safe, valid):
    class NativeProcess(Process):
        def read_result(self):
            return SetupResult(status="failed", reason="acl_unavailable", machine_safe=False,
                stage="request_commit", error_type="IOException", native_stage="commit_return",
                ntstatus="C0000035" if valid else "confidential-marker", io_status=None)

    results = iter([SetupResult(status="outdated", machine_safe=True, office_bitness=64,
        auto_install_mode="candidate"), SetupResult(status="outdated", machine_safe=safe)])
    checks = []
    def check():
        checks.append(1)
        return next(results)
    process = NativeProcess(exit_code=12)
    gate, policies, _ = coordinator(tmp_path, checker=check, process_factory=lambda: process)
    with caplog.at_level("INFO"):
        gate.begin()
        wait_until(lambda: process.started == 1)
        gate._poll_setup()
        wait_until(lambda: bool(policies))
    assert len(checks) == 2 and policies == (["com_only"] if safe else ["ppt_pending"])
    expected = "native_stage=commit_return ntstatus=C0000035 io_status=None" if valid else (
        "native_stage=None ntstatus=None io_status=None")
    assert expected in caplog.text and "confidential-marker" not in caplog.text
    assert process.closed == 1
    gate.shutdown()


@pytest.mark.parametrize("policy_reason", ["unknown_write", "reservation_released_retry_required",
    "confidential-marker", None, []])
def test_policy_diagnostic_logs_are_bounded_and_never_replace_postcheck(tmp_path, caplog, policy_reason: object) -> None:
    """Log only fixed policy diagnostics from checks and injected helper results."""
    class PolicyProcess(Process):
        def read_result(self) -> SetupResult:
            """Supply an untrusted diagnostic without granting readiness."""
            return SetupResult(status="failed", reason="acl_unavailable", policy_reason=policy_reason)

    checks = []
    results = iter([SetupResult(status="outdated", machine_safe=True, office_bitness=64,
        auto_install_mode="candidate", policy_reason=policy_reason),
        SetupResult(status="outdated", machine_safe=False, policy_reason=policy_reason)])

    def check() -> SetupResult:
        """Return independent initial and post-install safety observations."""
        checks.append(1)
        return next(results)

    process = PolicyProcess(exit_code=12)
    gate, policies, _ = coordinator(tmp_path, checker=check, process_factory=lambda: process)
    with caplog.at_level("INFO"):
        gate.begin()
        wait_until(lambda: process.started == 1)
        gate._poll_setup()
        wait_until(lambda: bool(policies))
    lines = [record.getMessage() for record in caplog.records
             if record.getMessage().startswith(("PPT 준비 점검:", "PPT 설치 도구 종료:"))]
    expected = policy_reason if isinstance(policy_reason, str) and policy_reason != "confidential-marker" else None
    assert len(lines) == 3 and all(f"policy_reason={expected}" in line for line in lines)
    assert "confidential-marker" not in caplog.text
    assert len(checks) == 2 and policies == ["ppt_pending"] and process.closed == 1
    gate.shutdown()


def test_decline_is_persisted_for_original_user_version(tmp_path):
    result = SetupResult(status="missing", machine_safe=True, office_bitness=64, auto_install_mode="candidate")
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
        checker=lambda: SetupResult(status="missing", machine_safe=True, office_bitness=64, auto_install_mode="candidate"))
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
        checker=lambda: SetupResult(status="missing", machine_safe=True, office_bitness=64, auto_install_mode="candidate"),
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
        checker=lambda: SetupResult(status="outdated", machine_safe=True, office_bitness=64, auto_install_mode="candidate"),
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


@pytest.mark.parametrize("exit_code", [10, 12, 13])
def test_non_decline_results_retry_on_next_app_run(tmp_path, exit_code):
    result = SetupResult(status="missing", machine_safe=True, office_bitness=64,
                         auto_install_mode="candidate")
    first = Process(exit_code=exit_code)
    gate, policies, _ = coordinator(tmp_path, checker=lambda: result, process_factory=lambda: first)
    gate.begin()
    wait_until(lambda: first.started == 1)
    gate._poll_setup()
    wait_until(lambda: bool(policies))
    assert not (tmp_path / "ppt_reader_preparation.json").exists()
    gate.begin()
    gate.request_start(retry_failures=True)
    assert first.started == 1
    gate.shutdown()
    second = Process()
    next_gate, _, _ = coordinator(tmp_path, checker=lambda: result, process_factory=lambda: second)
    next_gate.begin()
    wait_until(lambda: second.started == 1)
    next_gate.shutdown()


@pytest.mark.parametrize("reason", ["launch_failed", "check_failed", "mutation_failed"])
def test_legacy_failures_normalized_before_new_attempt(tmp_path, reason):
    record = tmp_path / "ppt_reader_preparation.json"
    record.write_text(json.dumps({"version": REQUIRED_VERSION, "reason": reason}))
    process = Process()
    gate, _, _ = coordinator(tmp_path, process_factory=lambda: process,
        checker=lambda: SetupResult(status="missing", machine_safe=True, office_bitness=64,
                                    auto_install_mode="candidate"))
    gate.begin()
    wait_until(lambda: process.started == 1)
    assert not record.exists()
    gate.shutdown()


def test_decline_survives_failed_postcheck(tmp_path):
    results = iter([SetupResult(status="missing", machine_safe=True, office_bitness=64,
                               auto_install_mode="candidate"), SetupResult()])
    process = Process(exit_code=11)
    gate, policies, _ = coordinator(tmp_path, checker=lambda: next(results), process_factory=lambda: process)
    gate.begin()
    wait_until(lambda: process.started == 1)
    gate._poll_setup()
    # Persist actual decline before the asynchronous postcheck can fail or shutdown.
    assert json.loads((tmp_path / "ppt_reader_preparation.json").read_text())["reason"] == "permission_declined"
    wait_until(lambda: bool(policies))
    assert policies == ["ppt_pending"]
    gate.shutdown()


def test_launch_exception_remains_pending_without_terminal_observation(tmp_path):
    class FailedProcess(Process):
        def start(self):
            super().start()
            raise OSError("launch_failed")
    process = FailedProcess()
    result = SetupResult(status="missing", machine_safe=True, office_bitness=64,
                         auto_install_mode="candidate")
    gate, policies, _ = coordinator(tmp_path, checker=lambda: result, process_factory=lambda: process)
    gate.begin()
    wait_until(lambda: bool(policies))
    assert policies == ["ppt_pending"] and process.closed == 1
    assert not (tmp_path / "ppt_reader_preparation.json").exists()
    gate.shutdown()


@pytest.mark.parametrize("safe,expected", [(True, "com_only"), (False, "ppt_pending")])
def test_confirmed_prelaunch_failure_uses_independent_postcheck(tmp_path, safe, expected):
    class NotLaunchedProcess(Process):
        def start(self):
            self.started += 1
            raise ppt_installation.SetupLaunchNotStarted("diagnostic_unavailable")
    process = NotLaunchedProcess()
    checks = iter([SetupResult(status="outdated", machine_safe=True, office_bitness=64,
        auto_install_mode="candidate"), SetupResult(status="outdated", machine_safe=safe)])
    gate, policies, _ = coordinator(tmp_path, checker=lambda: next(checks), process_factory=lambda: process)
    gate.begin()
    wait_until(lambda: bool(policies))
    assert policies == [expected] and process.closed == 1
    gate.shutdown()


def test_stale_and_duplicate_checks_never_duplicate_install_or_start(tmp_path):
    result = SetupResult(status="missing", machine_safe=True, office_bitness=64,
                         auto_install_mode="candidate")
    process = Process(exit_code=0)
    release = threading.Event()
    calls = []
    def check():
        calls.append(1)
        if len(calls) > 1:
            assert release.wait(3)
            return SetupResult(status="ready", machine_safe=True)
        return result
    gate, policies, workers = coordinator(tmp_path, checker=check, process_factory=lambda: process)
    gate.begin()
    gate.request_start()
    wait_until(lambda: process.started == 1)
    gate._checked((1, result))
    assert process.started == 1 and not workers
    gate._poll_setup()
    gate._checked((1, SetupResult(status="ready", machine_safe=True)))
    assert not workers
    release.set()
    wait_until(lambda: bool(workers))
    gate._checked((2, result))
    assert policies == ["ready"] and len(workers) == 1 and workers[0].started == 1
    gate.shutdown()


def test_missing_office_bitness_does_not_request_uac(tmp_path):
    gate, policies, _ = coordinator(tmp_path,
        checker=lambda: SetupResult(status="missing", machine_safe=True, auto_install_mode="candidate"),
        process_factory=lambda: pytest.fail("unknown Office bitness must not install"))
    gate.begin()
    wait_until(lambda: bool(policies))
    assert policies == ["com_only"]
    gate.shutdown()
