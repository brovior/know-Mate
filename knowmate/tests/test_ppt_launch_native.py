"""Native boundary contracts with synthetic process handles and PE headers."""
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowmate.secure import office_guard, ppt_installation, ppt_launch


class Function:
    def __init__(self, action):
        self.action = action

    def __call__(self, *args):
        return self.action(*args)


def kernel(monkeypatch, *, started=True, identity=777, identity_error=False):
    calls = []

    def create(application, command, process_acl, thread_acl, inherited, flags,
               environment, directory, startup, info):
        calls.append(("create", application, command.value, process_acl, thread_acl,
                      inherited, flags, environment, directory, startup._obj.cb))
        if started:
            info._obj.process, info._obj.thread, info._obj.pid = 101, 102, 123
        return started

    native = SimpleNamespace(CreateProcessW=Function(create),
                             CloseHandle=Function(lambda handle: calls.append(("close", handle)) or True))
    import ctypes
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: native, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)

    def read_identity(actual, handle):
        assert actual is native and handle == 101
        if identity_error:
            raise OSError("identity_failed")
        return identity

    monkeypatch.setattr(office_guard, "_process_creation_identity_from_handle", read_identity)
    return calls


@pytest.mark.parametrize("identity", [777, None])
def test_createprocess_is_explicit_noninherited_and_handles_close_once(monkeypatch, identity):
    calls = kernel(monkeypatch, identity=identity)
    path = Path("/Program Files/Office/POWERPNT.EXE")
    child = ppt_launch.LaunchedPowerPoint(path)
    assert child.pid == 123 and child.identity == identity
    call = calls[0]
    assert call[:3] == ("create", str(path), f'"{path}" /EMBEDDING')
    assert call[3:8] == (None, None, False, 0, None)
    assert call[8] == str(path.parent) and call[9] > 0
    assert calls[1:] == [("close", 102)]
    child.close()
    child.close()
    assert calls[1:] == [("close", 102), ("close", 101)]


def test_only_native_create_failure_is_explicitly_not_started(monkeypatch):
    calls = kernel(monkeypatch, started=False)
    with pytest.raises(ppt_launch.PptLaunchNotStarted):
        ppt_launch.LaunchedPowerPoint(Path("/Office/POWERPNT.EXE"))
    assert len(calls) == 1


def test_post_create_error_releases_both_handles_and_is_not_not_started(monkeypatch):
    calls = kernel(monkeypatch, identity_error=True)
    with pytest.raises(OSError) as error:
        ppt_launch.LaunchedPowerPoint(Path("/Office/POWERPNT.EXE"))
    assert not isinstance(error.value, ppt_launch.PptLaunchNotStarted)
    assert sorted(call[1] for call in calls if call[0] == "close") == [101, 102]


@pytest.fixture
def registry(monkeypatch, tmp_path):
    root = tmp_path / "TrustedOffice"
    root.mkdir()
    executable = root / "POWERPNT.EXE"
    image = bytearray(128)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 64)
    image[64:68] = b"PE\0\0"
    struct.pack_into("<H", image, 68, 0x8664)
    executable.write_bytes(image)
    state = {"path": str(executable), "kind": 1, "views": []}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def open_key(hive, name, reserved, access):
        assert hive == "HKLM"
        assert name == r"Software\Microsoft\Windows\CurrentVersion\App Paths\POWERPNT.EXE"
        state["views"].append(access)
        return Key()

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM", KEY_READ=1, KEY_WOW64_64KEY=256,
        KEY_WOW64_32KEY=512, REG_SZ=1, OpenKey=open_key,
        QueryValueEx=lambda key, name: (state["path"], state["kind"])))
    monkeypatch.setattr(ppt_installation, "_known_folder", lambda _guid: root)
    return root, executable, state


def test_registered_executable_and_office_bitness_are_verified(registry):
    _, executable, state = registry
    assert ppt_launch.trusted_powerpoint_path(64) == executable
    assert state["views"] == [257]
    with pytest.raises(ValueError, match="office_bitness"):
        ppt_launch.trusted_powerpoint_path(32)


@pytest.mark.parametrize("scenario", ["outside_root", "arguments", "invalid_kind", "bad_pe", "symlink"])
def test_unsafe_registered_executable_is_rejected(registry, scenario, tmp_path):
    root, executable, state = registry
    if scenario == "outside_root":
        outside = tmp_path / "POWERPNT.EXE"
        outside.write_bytes(executable.read_bytes())
        state["path"] = str(outside)
    elif scenario == "arguments":
        state["path"] = f'"{executable}" /unsafe'
    elif scenario == "invalid_kind":
        state["kind"] = 2
    elif scenario == "bad_pe":
        executable.write_bytes(b"MZ")
    else:
        original = root / "original.exe"
        executable.rename(original)
        executable.symlink_to(original)
    with pytest.raises((ValueError, OSError)):
        ppt_launch.trusted_powerpoint_path(64)
