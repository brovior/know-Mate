"""Exact-origin graceful PPT cleanup and cross-worker safety without Office."""
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowmate.secure import office_guard as guard
from knowmate.secure import ppt_launch, ppt_policy
from knowmate.secure import ppt_shutdown as shutdown


@pytest.fixture(autouse=True)
def clean_state():
    guard.clear_owned_pids()
    yield
    guard.clear_owned_pids()


@pytest.fixture
def launch(monkeypatch):
    app = SimpleNamespace(DisplayAlerts=1)
    client = SimpleNamespace(Dispatch=Mock(return_value=app))
    handles = []

    class Process:
        pid, identity = 10, 100
        def __init__(self, path):
            self.close = Mock()
            handles.append(self)

    @contextmanager
    def window(exe, app):
        yield lambda: 50
    monkeypatch.setattr(ppt_launch, "trusted_powerpoint_path", lambda bitness: Path("/trusted/POWERPNT.EXE"))
    monkeypatch.setattr(ppt_launch, "LaunchedPowerPoint", Process)
    monkeypatch.setattr(guard, "prepare_powerpoint_dispatch", lambda: (set(), {}))
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", 10)])
    monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: 100)
    monkeypatch.setattr(guard, "_ownership_window", window)
    monkeypatch.setattr(guard, "_pid_from_hwnd", lambda hwnd: 10 if hwnd else None)
    return app, client, handles


def test_exact_direct_launch_authority_and_handle_release(launch):
    app, client, handles = launch
    result = shutdown.launch_and_bind(client, 64)
    assert result[0] is app and result[1][0] == 10
    assert result[1][1].terminable is False
    assert shutdown._exact_origin(result[2], result[1])
    assert app.DisplayAlerts == 2
    handles[0].close.assert_called_once()
    client.Dispatch.assert_called_once_with("PowerPoint.Application")


@pytest.mark.parametrize("scenario", ["other_pid", "wrong_host", "unknown_identity", "closing"])
def test_ambiguous_created_host_never_acquires_quit_authority(launch, monkeypatch, scenario):
    app, client, handles = launch
    if scenario == "other_pid":
        monkeypatch.setattr(guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", 10), ("POWERPNT.EXE", 11)])
    elif scenario == "wrong_host":
        monkeypatch.setattr(guard, "_pid_from_hwnd", lambda hwnd: 11)
    elif scenario == "unknown_identity":
        monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: None)
    else:
        client.Dispatch.side_effect = lambda _: (guard.begin_application_shutdown(), app)[1]
    with pytest.raises(guard.OfficeCleanupPendingError):
        shutdown.launch_and_bind(client, 64)
    assert shutdown._ticket.state == "pending"
    assert shutdown.admission_reason() == "ppt_quit_pending"
    handles[0].close.assert_called_once()


def test_createprocess_inflight_cannot_be_cleared_or_relaunched_by_another_worker(launch, monkeypatch):
    _app, client, handles = launch
    entered, release = threading.Event(), threading.Event()
    original = ppt_launch.LaunchedPowerPoint
    def blocked(path):
        entered.set()
        assert release.wait(5)
        return original(path)
    monkeypatch.setattr(ppt_launch, "LaunchedPowerPoint", blocked)
    monkeypatch.setattr(guard, "_enumerate_processes", list)
    outcome = []
    def worker():
        try:
            shutdown.launch_and_bind(client, 64)
        except guard.OfficeCleanupPendingError:
            outcome.append("deferred")
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        ticket = shutdown._ticket
        with pytest.raises(guard.OfficeCleanupPendingError):
            shutdown.ensure_admitted()
        with pytest.raises(guard.OfficeCleanupPendingError):
            shutdown.launch_and_bind(client, 64)
        assert shutdown._ticket is ticket
    finally:
        release.set()
        thread.join(5)
    assert outcome == ["deferred"]
    assert len(handles) == 1
    handles[0].close.assert_called_once()


@pytest.mark.parametrize("failure", [ppt_launch.PptLaunchNotStarted, OSError])
def test_only_explicit_createprocess_failure_can_discard_origin(launch, monkeypatch, failure):
    _, client, _ = launch
    def fail(path):
        raise failure("failure")
    monkeypatch.setattr(ppt_launch, "LaunchedPowerPoint", fail)
    if failure is ppt_launch.PptLaunchNotStarted:
        assert shutdown.launch_and_bind(client, 64) is None
        assert shutdown._ticket is None
    else:
        with pytest.raises(OSError):
            shutdown.launch_and_bind(client, 64)
        assert shutdown._ticket.state == "pending"
        assert shutdown._ticket.pid is None


def test_untrusted_office_path_never_calls_createprocess_or_dispatch(launch, monkeypatch):
    _, client, handles = launch
    monkeypatch.setattr(ppt_launch, "trusted_powerpoint_path", Mock(side_effect=ValueError("path")))
    assert shutdown.launch_and_bind(client, 64) is None
    assert not handles
    client.Dispatch.assert_not_called()


@pytest.fixture
def origin(launch, monkeypatch):
    app, client, _ = launch
    app, proof, ticket = shutdown.launch_and_bind(client, 64)
    service = SimpleNamespace(ProtocolVersion=lambda: 3, ReaderVersion=lambda: "1.0.5.0",
                              TryQuitIfIdle=Mock(return_value="quit_requested"))
    app.COMAddIns = SimpleNamespace(Item=Mock(return_value=SimpleNamespace(Connect=True, Object=service)))
    monkeypatch.setattr(ppt_policy, "auto_shutdown_enabled", lambda: True)
    return app, proof, ticket, SimpleNamespace(), service


def test_quit_once_then_new_worker_is_blocked_until_actual_exit(origin, monkeypatch):
    app, proof, ticket, cache, service = origin
    status, snapshot = shutdown.request_quit(app, proof, ticket, cache)
    assert status == "quit_requested"
    assert snapshot[10].cleanup_pending and not snapshot[10].terminable
    assert shutdown.request_quit(app, proof, ticket, cache)[0] == "not_supported"
    service.TryQuitIfIdle.assert_called_once()
    errors = []
    def next_worker():
        try:
            shutdown.ensure_admitted()
        except guard.OfficeCleanupPendingError:
            errors.append("blocked")
    worker = threading.Thread(target=next_worker)
    worker.start(); worker.join(5)
    assert errors == ["blocked"]
    result = shutdown.observe_exit(snapshot, 0, lambda records, timeout: (set(records), 0))
    assert result.remaining == frozenset({10}) and not result.confirmed_exited
    monkeypatch.setattr(guard, "_enumerate_processes", list)
    result = shutdown.observe_exit(snapshot, 1, lambda records, timeout: (set(), .1))
    assert result.confirmed_exited == frozenset({10})
    assert shutdown._ticket is None
    assert 10 not in guard.take_owned_processes()


@pytest.mark.parametrize("status", ["protected", "not_supported", "failed"])
def test_user_or_unsupported_host_is_yielded_without_force_cleanup(origin, status):
    app, proof, ticket, cache, service = origin
    service.TryQuitIfIdle.return_value = status
    assert shutdown.request_quit(app, proof, ticket, cache) == (status, {})
    assert shutdown._ticket is None
    assert 10 not in guard.take_owned_processes()
    service.TryQuitIfIdle.assert_called_once()


@pytest.mark.parametrize("scenario", ["missing_service", "disabled_service", "old_service", "no_direct_origin", "changed_proof", "poison", "pid_reuse", "unknown_identity"])
def test_no_quit_for_unproven_or_unsafe_hosts(origin, monkeypatch, scenario):
    app, proof, ticket, cache, service = origin
    if scenario == "missing_service":
        app.COMAddIns.Item.side_effect = RuntimeError("missing")
    elif scenario == "disabled_service":
        app.COMAddIns.Item.return_value.Connect = False
    elif scenario == "old_service":
        service.ProtocolVersion = lambda: 2
    elif scenario == "no_direct_origin":
        ticket = None
    elif scenario == "changed_proof":
        proof = (10, guard.OwnedOfficeProcess("POWERPNT.EXE", 100, False, proof[1].generation + 1))
    elif scenario == "pid_reuse":
        monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: 200)
    elif scenario == "unknown_identity":
        monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: None)
    status, _snapshot = shutdown.request_quit(app, proof, ticket, cache, poisoned=scenario == "poison")
    assert status in {"not_supported", "pending", "identity_unknown"}
    service.TryQuitIfIdle.assert_not_called()
    if scenario in {"poison", "pid_reuse", "unknown_identity"}:
        app.COMAddIns.Item.assert_not_called()


def test_unknown_quit_result_and_rpc_error_never_retry(origin):
    app, proof, ticket, cache, service = origin
    service.TryQuitIfIdle.side_effect = RuntimeError("disconnected")
    status, snapshot = shutdown.request_quit(app, proof, ticket, cache)
    assert status == "pending" and snapshot
    with pytest.raises(guard.OfficeCleanupPendingError):
        shutdown.ensure_admitted()
    service.TryQuitIfIdle.assert_called_once()


@pytest.mark.parametrize("mode", ["disabled", "candidate", "released"])
def test_build_only_shutdown_gate_never_enables_unverified_release(monkeypatch, mode):
    monkeypatch.setattr(ppt_policy.sys, "frozen", True, raising=False)
    monkeypatch.setattr(ppt_policy, "get_build_info", lambda: {"ppt_auto_install_mode": mode})
    assert ppt_policy.auto_shutdown_enabled() == (mode == "candidate")
    monkeypatch.setattr(ppt_policy.sys, "frozen", False)
    assert not ppt_policy.auto_shutdown_enabled()


def test_existing_worker_cleanup_releases_every_ppt_reference_before_os_wait(origin, monkeypatch):
    from knowmate.secure import com_reader
    app, proof, ticket, _cache, service = origin
    for name in ("word", "excel"):
        monkeypatch.setattr(com_reader._tls, name, None, raising=False)
    for name, value in (("ppt", app), ("ppt_proof", proof), ("ppt_launch_ticket", ticket),
                        ("ppt_service", None)):
        monkeypatch.setattr(com_reader._tls, name, value, raising=False)
    def wait(records, seconds):
        assert set(records) == {10}
        assert com_reader._tls.ppt is None
        assert com_reader._tls.ppt_proof is None
        assert com_reader._tls.ppt_launch_ticket is None
        assert com_reader._tls.ppt_service is None
        return set(records), seconds
    result = com_reader.quit_com_apps(grace_sec=.1, wait_fn=wait)
    assert result.remaining == frozenset({10})
    assert not result.forced_exited
    service.TryQuitIfIdle.assert_called_once()
    assert com_reader.quit_com_apps(grace_sec=0).forced_exited == frozenset()
    service.TryQuitIfIdle.assert_called_once()


@pytest.mark.parametrize("scenario,reason", [
    ("origin", "origin_mismatch"), ("policy", "policy_disabled"),
    ("service", "service_unavailable"), ("host", "service_return"),
])
def test_not_supported_diagnostics_distinguish_authority_from_host(origin, monkeypatch, caplog, scenario, reason):
    app, proof, ticket, cache, service = origin
    caplog.set_level("INFO", logger=shutdown.__name__)
    if scenario == "origin":
        ticket = None
    elif scenario == "policy":
        monkeypatch.setattr(ppt_policy, "auto_shutdown_enabled", lambda: False)
    elif scenario == "service":
        app.COMAddIns.Item.side_effect = RuntimeError("private-document-marker")
    else:
        service.TryQuitIfIdle.return_value = "not_supported"
    assert shutdown.request_quit(app, proof, ticket, cache) == ("not_supported", {})
    assert f"reason={reason}" in caplog.text
    assert "private-document-marker" not in caplog.text
    assert service.TryQuitIfIdle.call_count == (1 if scenario == "host" else 0)


def test_unknown_service_status_is_not_logged_or_retried(origin, caplog):
    app, proof, ticket, cache, service = origin
    caplog.set_level("INFO", logger=shutdown.__name__)
    service.TryQuitIfIdle.return_value = "private-document-marker"
    status, snapshot = shutdown.request_quit(app, proof, ticket, cache)
    assert status == "pending" and snapshot
    assert "reason=service_return status=unknown" in caplog.text
    assert "private-document-marker" not in caplog.text
    assert shutdown.request_quit(app, proof, ticket, cache) == ("not_supported", {})
    service.TryQuitIfIdle.assert_called_once()


def test_foreign_worker_diagnostics_do_not_grant_quit_authority(origin, caplog):
    app, proof, ticket, cache, service = origin
    caplog.set_level("INFO", logger=shutdown.__name__)
    result = []
    worker = threading.Thread(target=lambda: result.append(shutdown.request_quit(app, proof, ticket, cache)))
    worker.start(); worker.join(5)
    assert result == [("not_supported", {})]
    assert "reason=origin_mismatch" in caplog.text
    service.TryQuitIfIdle.assert_not_called()


def test_poison_releases_all_worker_capabilities_without_more_com(origin, monkeypatch):
    from knowmate.secure import com_reader
    app, proof, ticket, _cache, service = origin
    for name, value in (("ppt", app), ("ppt_proof", proof), ("ppt_launch_ticket", ticket),
                        ("ppt_service", (app, service, "ready"))):
        monkeypatch.setattr(com_reader._tls, name, value, raising=False)
    com_reader._release_ppt_references(poisoned=True)
    assert shutdown._ticket.state == "pending"
    assert all(getattr(com_reader._tls, name) is None for name in
               ("ppt", "ppt_proof", "ppt_launch_ticket", "ppt_service"))
    app.COMAddIns.Item.assert_not_called()
    service.TryQuitIfIdle.assert_not_called()


def test_ppt_alerts_are_all_from_first_configuration_and_unsupported_cleanup(monkeypatch):
    from knowmate.secure import com_reader
    app = SimpleNamespace(DisplayAlerts=1, AutomationSecurity=1)
    monkeypatch.setattr(com_reader._tls, "ppt", None, raising=False)
    monkeypatch.setattr(com_reader._tls, "ppt_launch_ready", None, raising=False)
    monkeypatch.setattr(com_reader, "_ensure_com_initialized", lambda: False)
    monkeypatch.setattr(com_reader, "_require_win32com", lambda: None)
    monkeypatch.setattr(com_reader, "_dispatch_owned_app",
                        lambda *args: com_reader.DispatchedOfficeApp(app, None))
    assert com_reader._get_ppt_app() is app
    assert app.DisplayAlerts == 2
    assert shutdown.request_quit(app, None, None, SimpleNamespace()) == ("not_supported", {})
    assert app.DisplayAlerts == 2
    com_reader._release_ppt_references()


def test_initial_alert_setter_failure_releases_capabilities_and_never_quits(monkeypatch):
    from knowmate.secure import com_reader
    class App:
        @property
        def DisplayAlerts(self):
            return 1
        @DisplayAlerts.setter
        def DisplayAlerts(self, value):
            raise RuntimeError("alerts unavailable")
    app = App()
    monkeypatch.setattr(com_reader._tls, "ppt", None, raising=False)
    monkeypatch.setattr(com_reader._tls, "ppt_launch_ready", None, raising=False)
    monkeypatch.setattr(com_reader, "_ensure_com_initialized", lambda: False)
    monkeypatch.setattr(com_reader, "_require_win32com", lambda: None)
    monkeypatch.setattr(com_reader, "_dispatch_owned_app",
                        lambda *args: com_reader.DispatchedOfficeApp(app, None))
    with pytest.raises(RuntimeError, match="alerts unavailable"):
        com_reader._get_ppt_app()
    assert com_reader._tls.ppt is None
    assert com_reader._tls.ppt_proof is None
    assert com_reader._tls.ppt_launch_ticket is None


def test_stale_observation_cannot_delete_replacement_owned_generation(origin, monkeypatch):
    app, proof, ticket, cache, _service = origin
    _, snapshot = shutdown.request_quit(app, proof, ticket, cache)
    replacement = guard.OwnedOfficeProcess("POWERPNT.EXE", 200, False, proof[1].generation + 1)
    guard._owned_pids[10] = replacement
    monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: 200)
    result = shutdown.observe_exit(snapshot, 0, lambda records, timeout: (set(), 0))
    assert result.confirmed_exited == frozenset({10})
    assert guard.take_owned_processes()[10] is replacement


def test_installation_stamp_detects_registry_dll_and_record_mutations(monkeypatch, tmp_path):
    import hashlib
    import json
    import sys

    from knowmate.secure import ppt_installation
    class Key:
        def __init__(self, path):
            self.path = path
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    registry = {"value": "ready"}
    module = SimpleNamespace(HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2,
        KEY_READ=1, KEY_WOW64_32KEY=32, KEY_WOW64_64KEY=64,
        OpenKey=lambda hive, path, reserved, access: Key(path),
        QueryInfoKey=lambda key: (0, 1, 0),
        EnumValue=lambda key, index: ("version", registry["value"], 1))
    monkeypatch.setitem(sys.modules, "winreg", module)
    monkeypatch.setattr(ppt_installation, "_known_folder", lambda identifier: tmp_path)
    directory = tmp_path / "AegisDesk/PptReader"
    versioned = directory / "1.0.5.0"
    versioned.mkdir(parents=True)
    dll = versioned / "AegisDesk.PptReader.dll"
    dll.write_bytes(b"compiled candidate")
    events = versioned / "AegisDesk.PptReader.Events.dll"
    events.write_bytes(b"compiled events")
    record = directory / "installation.json"
    digest = hashlib.sha256(dll.read_bytes()).hexdigest()
    metadata = {"schema": 2, "version": "1.0.5.0", "sha256": digest,
                "files": {dll.name: digest, events.name: hashlib.sha256(events.read_bytes()).hexdigest()}}
    record.write_text(json.dumps(metadata))
    first = ppt_launch.installation_stamp()
    assert first is not None
    registry["value"] = "removed"
    assert ppt_launch.installation_stamp() != first
    registry["value"] = "ready"
    dll.write_bytes(b"different candidate")
    assert ppt_launch.installation_stamp() is None
    dll.write_bytes(b"compiled candidate")
    events.write_bytes(b"different events")
    assert ppt_launch.installation_stamp() is None
    events.write_bytes(b"compiled events")
    incomplete = dict(metadata, files={dll.name: digest})
    record.write_text(json.dumps(incomplete))
    assert ppt_launch.installation_stamp() is None
    record.write_text(json.dumps(metadata))
    assert ppt_launch.installation_stamp() == first
    record.unlink()
    assert ppt_launch.installation_stamp() != first


def test_application_closing_allows_existing_worker_graceful_cleanup_only(origin, monkeypatch):
    from knowmate.secure import com_reader
    app, proof, ticket, cache, service = origin
    guard.begin_application_shutdown()
    status, snapshot = shutdown.request_quit(app, proof, ticket, cache)
    assert status == "quit_requested" and snapshot
    service.TryQuitIfIdle.assert_called_once()
    assert guard._application_closing.is_set()
    for name, value in (("ppt", None), ("ppt_launch_ready", None)):
        monkeypatch.setattr(com_reader._tls, name, value, raising=False)
    require = Mock()
    monkeypatch.setattr(com_reader, "_require_win32com", require)
    with pytest.raises(guard.OfficeCleanupPendingError, match="application_closing"):
        com_reader._get_ppt_app()
    with pytest.raises(guard.OfficeCleanupPendingError, match="application_closing"):
        shutdown.launch_and_bind(SimpleNamespace(Dispatch=Mock()), 64)
    require.assert_not_called()


def test_foreign_worker_cannot_use_another_threads_quit_capability(origin):
    app, proof, ticket, cache, service = origin
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(
        shutdown.request_quit(app, proof, ticket, cache)))
    worker.start()
    worker.join(5)
    assert outcome == [("not_supported", {})]
    assert shutdown._ticket is ticket
    app.COMAddIns.Item.assert_not_called()
    service.TryQuitIfIdle.assert_not_called()


def test_repeated_and_new_worker_cleanup_never_reports_pending_host_complete(origin, monkeypatch):
    from knowmate.secure import com_reader
    app, proof, ticket, _cache, service = origin
    for name in ("word", "excel"):
        monkeypatch.setattr(com_reader._tls, name, None, raising=False)
    for name, value in (("ppt", app), ("ppt_proof", proof), ("ppt_launch_ticket", ticket),
                        ("ppt_service", None)):
        monkeypatch.setattr(com_reader._tls, name, value, raising=False)
    first = com_reader.quit_com_apps(grace_sec=0, wait_fn=lambda records, timeout: (set(records), 0))
    assert not first.successful and first.remaining == frozenset({10})
    second = com_reader.quit_com_apps(grace_sec=0)
    assert not second.successful and second.remaining == frozenset({10})
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(com_reader.quit_com_apps(grace_sec=0)))
    worker.start(); worker.join(5)
    assert len(outcomes) == 1 and not outcomes[0].successful
    assert outcomes[0].remaining == frozenset({10})
    service.TryQuitIfIdle.assert_called_once()
    monkeypatch.setattr(guard, "_enumerate_processes", list)
    assert com_reader.quit_com_apps(grace_sec=0).successful
    assert shutdown._ticket is None
    service.TryQuitIfIdle.assert_called_once()


def test_uncertain_launch_without_pid_keeps_cleanup_incomplete_until_os_absence(monkeypatch):
    from knowmate.secure import com_reader
    shutdown._ticket = shutdown.LaunchTicket(1000, threading.get_ident(), state="pending")
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", 10)])
    for name in ("word", "excel", "ppt"):
        monkeypatch.setattr(com_reader._tls, name, None, raising=False)
    first = com_reader.quit_com_apps(grace_sec=0)
    assert not first.successful
    assert first.ownership_pending == frozenset({"POWERPNT.EXE"})
    monkeypatch.setattr(guard, "_enumerate_processes", list)
    assert com_reader.quit_com_apps(grace_sec=0).successful


@pytest.mark.parametrize("record", [object(), None])
def test_stale_ticket_diagnostics_preserve_rejection_of_unusable_record(origin, monkeypatch, record):
    app, _proof, ticket, cache, service = origin
    monkeypatch.setattr(shutdown, "_ticket", None)
    proof = (123, record)
    assert shutdown._exact_origin(ticket, proof) is False
    assert shutdown.request_quit(app, proof, ticket, cache) == ("not_supported", {})
    app.COMAddIns.Item.assert_not_called()
    service.TryQuitIfIdle.assert_not_called()


def test_stale_ticket_diagnostics_never_inspect_record_or_log_under_lock(origin, monkeypatch):
    app, _proof, ticket, cache, service = origin
    monkeypatch.setattr(shutdown, "_ticket", None)
    class UnreadableRecord:
        def __getattribute__(self, name):
            raise AssertionError("rejected record must not be inspected")
    class ObservedLock:
        held = False
        def __enter__(self):
            self.held = True
        def __exit__(self, *args):
            self.held = False
    lock = ObservedLock()
    monkeypatch.setattr(shutdown, "_lock", lock)
    calls = []
    def log(*args):
        assert not lock.held
        calls.append(args)
    monkeypatch.setattr(shutdown.logger, "info", log)
    assert shutdown.request_quit(app, (123, UnreadableRecord()), ticket, cache) == ("not_supported", {})
    assert len(calls) == 1
    app.COMAddIns.Item.assert_not_called()
    service.TryQuitIfIdle.assert_not_called()


@pytest.mark.parametrize("reason,hwnd,bound_pid,processes,identity", [
    ("hwnd_missing", None, None, None, None),
    ("pid_mismatch", 50, 20, None, None),
    ("process_enumeration_unavailable", 50, 10, None, None),
    ("process_set_mismatch", 50, 10, [("POWERPNT.EXE", 10), ("POWERPNT.EXE", 20)], None),
    ("identity_mismatch", 50, 10, [("POWERPNT.EXE", 10)], 200),
    ("identity_mismatch", 50, 10, [("POWERPNT.EXE", 10)], None),
])
def test_exact_hwnd_rejection_logs_preserve_probe_short_circuit(
        launch, monkeypatch, caplog, reason, hwnd, bound_pid, processes, identity):
    _app, client, handles = launch
    caplog.set_level("INFO", logger=shutdown.__name__)
    read = Mock(return_value=hwnd)
    @contextmanager
    def window(*args):
        yield read
    enumeration = Mock(side_effect=[[("POWERPNT.EXE", 10)], processes])
    pid_probe = Mock(return_value=bound_pid)
    identity_probe = Mock(return_value=identity)
    monkeypatch.setattr(guard, "_ownership_window", window)
    monkeypatch.setattr(guard, "_enumerate_processes", enumeration)
    monkeypatch.setattr(guard, "_pid_from_hwnd", pid_probe)
    monkeypatch.setattr(guard, "_process_creation_identity", identity_probe)
    def register(exe, baseline, exact_hwnd, records):
        assert exact_hwnd() is None
        return None
    monkeypatch.setattr(guard, "_register_owned_window", register)
    with pytest.raises(guard.OfficeCleanupPendingError):
        shutdown.launch_and_bind(client, 64)
    assert f"reason={reason}" in caplog.text
    read.assert_called_once_with()
    pid_probe.assert_called_once_with(hwnd)
    assert enumeration.call_count == (1 if reason in {"hwnd_missing", "pid_mismatch"} else 2)
    assert identity_probe.call_count == (1 if reason == "identity_mismatch" else 0)
    handles[0].close.assert_called_once()
    assert shutdown._ticket.state == "pending"


@pytest.mark.parametrize("live_pid,expected_remaining", [(10, frozenset({10})), (20, frozenset())])
def test_pending_cleanup_observes_launch_ticket_not_unowned_replacement(monkeypatch, live_pid, expected_remaining):
    ticket = shutdown.LaunchTicket(1, threading.get_ident(), state="pending", pid=10, identity=100)
    monkeypatch.setattr(shutdown, "_ticket", ticket)
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", live_pid)])
    monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: 100)
    result = shutdown.pending_cleanup_result()
    assert result.remaining == expected_remaining
    assert result.successful == (live_pid != 10)
    assert result.confirmed_exited == frozenset()
    assert (shutdown._ticket is None) == (live_pid != 10)
