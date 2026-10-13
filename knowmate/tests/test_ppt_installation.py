"""Installation contracts and fake native boundaries, without Office/registry writes."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowmate.secure import ppt_installation as installation


def result_json(**changes):
    value = dict(schema=1, operation="check", status="ready", reason="registration_ok",
                 required_version="1.0.0.0", installed_version="1.0.0.0", office_bitness=64,
                 scope="Machine", machine_safe=True, auto_install_validated=False)
    value.update(changes)
    return json.dumps(value)


def reservation(**changes):
    value = dict(schema=1, request_id="33e979c6-c5c4-40af-845a-64c80517d1ae",
                 required_version="1.0.0.0", helper_sid="S-1-5-21-1-2-3-1001", helper_pid=123,
                 helper_creation_ticks="638901000000000000", child_pid=None,
                 child_creation_ticks=None, state="complete")
    value.update(changes)
    return value


@pytest.mark.parametrize("changes,code", [({}, 0), ({"status": "missing", "reason": "not_installed",
    "installed_version": None}, 0)])
def test_valid_setup_contract(changes, code):
    assert installation.parse_setup_result(result_json(**changes), code).machine_safe


@pytest.mark.parametrize("changes,code", [({"schema": True}, 0), ({"operation": "prepare"}, 0),
    ({"required_version": "0.1.3.0"}, 0), ({"scope": "User"}, 0), ({"status": "ready"}, 13),
    ({"machine_safe": False}, 0), ({"office_bitness": True}, 0), ({"office_bitness": None}, 0),
    ({"reason": "body text"}, 0), ({"installed_version": "1.0.0"}, 0),
    ({"stdout_body": "confidential"}, 0), ({"machine_safe": 1}, 0), ({}, 42)])
def test_malformed_setup_never_ready(changes, code):
    value = installation.parse_setup_result(result_json(**changes), code)
    assert value.status == "unknown" and not value.machine_safe


def test_manifest_cannot_enable_auto_install():
    assert not installation.parse_setup_result(result_json(auto_install_validated=True), 0).auto_install_validated
    assert installation.AUTO_INSTALL_VALIDATED is False


@pytest.mark.parametrize("changes", [dict(schema=True), dict(helper_pid=True), dict(helper_creation_ticks="0"),
    dict(state="invalid"), dict(child_pid=5), dict(child_pid=True, child_creation_ticks="5"),
    dict(helper_sid="not-sid"), dict(required_version="evil"), dict(request_id="not-uuid"),
    dict(extra="secret"), dict(state="elevated")])
def test_corrupt_reservation_not_safe(changes):
    assert not installation._valid_reservation(reservation(**changes), reservation()["request_id"])


def test_old_completed_version_valid_but_filename_must_match():
    value = reservation(required_version="0.9.0.0")
    assert installation._valid_reservation(value, value["request_id"])
    assert not installation._valid_reservation(value, "a-different-uuid")


def fake_folders(monkeypatch, tmp_path):
    common = tmp_path / "common"
    program = tmp_path / "program"
    monkeypatch.setattr(installation, "_known_folder", lambda identifier:
        common if identifier.startswith("62AB") else program)
    return common / "AegisDesk" / "PptReaderRequests", program / "AegisDesk" / "PptReader"


def test_fresh_machine_has_no_pending_reservation(monkeypatch, tmp_path):
    fake_folders(monkeypatch, tmp_path)
    assert installation._machine_records_safe()


@pytest.mark.parametrize("kind", ["reserved", "unresolved", "elevated", "corrupt", "unexpected", "symlink", "journal"])
def test_pending_and_corrupt_records_never_expire(monkeypatch, tmp_path, kind):
    requests, installed = fake_folders(monkeypatch, tmp_path)
    requests.mkdir(parents=True)
    record = requests / (reservation()["request_id"] + ".json")
    if kind == "journal":
        installed.mkdir(parents=True)
        (installed / "journal.json").write_text("{}")
    elif kind == "unexpected":
        (requests / "foreign.tmp").write_text("{}")
    elif kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text(json.dumps(reservation()))
        record.symlink_to(target)
    elif kind == "corrupt":
        record.write_text(json.dumps({"schema": 1, "state": "complete"}))
    else:
        record.write_text(json.dumps(reservation(state=kind)))
    assert not installation._machine_records_safe()


def test_complete_records_safe_and_known_folder_failure_closed(monkeypatch, tmp_path):
    requests, _ = fake_folders(monkeypatch, tmp_path)
    requests.mkdir(parents=True)
    (requests / (reservation()["request_id"] + ".json")).write_text(json.dumps(reservation()))
    assert installation._machine_records_safe()
    monkeypatch.setattr(installation, "_known_folder", lambda _: (_ for _ in ()).throw(OSError()))
    assert not installation._machine_records_safe()


class NativeCall:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


def fake_mutex(monkeypatch, wait=0, existing=55):
    calls = []
    kernel = SimpleNamespace(**{name: NativeCall(callback) for name, callback in {
        "OpenMutexW": lambda rights, inherit, name: calls.append(("open", rights)) or existing,
        "CreateMutexW": lambda security, inherit, name: calls.append(("create", security is not None)) or 55,
        "WaitForSingleObject": lambda *args: wait,
        "ReleaseMutex": lambda *args: calls.append(("release", args)),
        "CloseHandle": lambda *args: calls.append(("close", args)),
        "LocalFree": lambda *args: None,
    }.items()})
    advapi = SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptorW=NativeCall(lambda *args: 1))
    monkeypatch.setattr(installation.ctypes, "WinDLL", lambda name, **kwargs: advapi if name == "advapi32" else kernel, raising=False)
    monkeypatch.setattr(installation, "os", SimpleNamespace(name="nt"))
    return calls


def test_guard_rechecks_under_owned_mutex_without_helper(monkeypatch):
    calls = fake_mutex(monkeypatch)
    monkeypatch.setattr(installation, "check_installation", lambda: pytest.fail("self-contending helper"))
    monkeypatch.setattr(installation, "_machine_records_safe", lambda: True)
    with installation.MachineReadGuard():
        assert not any(call[0] == "release" for call in calls)
    assert calls[0] == ("open", 0x00100001)
    assert [call[0] for call in calls][-2:] == ["release", "close"]


@pytest.mark.parametrize("wait,safe", [(0x80, False), (0, False), (258, True)])
def test_guard_busy_abandoned_unresolved_release(monkeypatch, wait, safe):
    calls = fake_mutex(monkeypatch, wait)
    with pytest.raises(installation.PptMachineBusy):
        with installation.MachineReadGuard(lambda: installation.SetupResult(machine_safe=safe)):
            pytest.fail("unsafe read")
    assert any(call[0] == "close" for call in calls)


def test_cross_user_creation_uses_explicit_acl(monkeypatch):
    calls = fake_mutex(monkeypatch, existing=0)
    with installation.MachineReadGuard(lambda: installation.SetupResult(machine_safe=True)):
        pass
    assert ("create", True) in calls


def test_missing_payload_fresh_machine_uses_safe_com(monkeypatch, tmp_path):
    fake_mutex(monkeypatch)
    monkeypatch.setattr(installation, "_validate_helper", lambda _: False)
    monkeypatch.setattr(installation, "_machine_records_safe", lambda: True)
    state = installation.check_installation(tmp_path / "PptReaderSetup.exe")
    assert state.machine_safe and state.reason == "payload_invalid"
    monkeypatch.setattr(installation, "_machine_records_safe", lambda: False)
    assert not installation.check_installation(tmp_path / "PptReaderSetup.exe").machine_safe


def test_detached_setup_gate_and_close_does_not_kill(monkeypatch):
    fake_mutex(monkeypatch)
    process = installation.DetachedSetupProcess(Path("PptReaderSetup.exe"))
    with pytest.raises(installation.PptMachineBusy, match="prototype_gate"):
        process.start()
    closed = []
    process._process = SimpleNamespace(_handle=SimpleNamespace(Close=lambda: closed.append(True)),
        kill=lambda: pytest.fail("kill"), terminate=lambda: pytest.fail("terminate"), poll=lambda: None)
    assert process.poll() is None
    process.close()
    assert closed == [True]


def test_setup_csharp_contract_without_installation(tmp_path):
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Windows .NET compiler required; setup native operations not executed")
    root = Path(__file__).resolve().parents[2]
    sources = [root / "scripts/ppt_reader/PptReaderSetup.cs", *sorted((root / "scripts/ppt_reader").glob("PptSetup*.cs")),
               root / "knowmate/tests/ppt_setup_harness.cs"]
    executable = tmp_path / "SetupHarness.exe"
    commands = [[str(compiler), "/nologo", "/main:SetupHarness", f"/out:{executable}",
                 "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll", "/reference:System.Security.dll",
                 *(str(path) for path in sources)], [str(executable), str(tmp_path)]]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stdout + result.stderr
