"""Office Resiliency 표식 정리(knowmate/secure/office_resiliency.py) 단위 테스트.

실제 레지스트리는 Windows에만 있으므로 winreg를 가짜 모듈로 주입해 검증한다
(사외 Linux 환경에서도 전부 통과).

가장 중요한 회귀 방어: **DocumentRecovery는 절대 지우면 안 된다** — 사용자 본인이
저장하지 못하고 잃은 문서의 복구 목록이라, 인덱서가 지우면 실제 업무 데이터가
복구 불가능해진다.
"""
import sys

import pytest

from knowmate.secure import office_resiliency


class _FakeKey:
    """winreg.OpenKey가 반환하는 핸들 대역."""

    def __init__(self, reg: "_FakeWinreg", path: str) -> None:
        self.reg = reg
        self.path = path
        self.closed = False

    def Close(self) -> None:
        self.closed = True


class _FakeWinreg:
    """존재하는 키 경로 집합으로 동작하는 최소 winreg 대역."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_WRITE = 2

    def __init__(self, existing: set[str], deny: set[str] | None = None) -> None:
        self.existing = set(existing)
        self.deny = set(deny or ())
        self.deleted: list[str] = []
        self.opened: list[str] = []

    def OpenKey(self, root, path, reserved=0, access=0):
        self.opened.append(path)
        if path in self.deny:
            raise PermissionError(f"access denied: {path}")
        if path not in self.existing:
            raise FileNotFoundError(path)
        return _FakeKey(self, path)

    def EnumKey(self, key: _FakeKey, index: int) -> str:
        prefix = key.path + "\\"
        children = sorted(
            p[len(prefix):] for p in self.existing
            if p.startswith(prefix) and "\\" not in p[len(prefix):]
        )
        if index >= len(children):
            raise OSError("no more items")
        return children[index]

    def DeleteKey(self, root, path: str) -> None:
        if path in self.deny:
            raise PermissionError(f"access denied: {path}")
        self.existing.discard(path)
        self.deleted.append(path)


def _install(monkeypatch, reg: _FakeWinreg) -> None:
    """비Windows 테스트 환경에서 Windows 경로를 타도록 플랫폼·winreg를 대체한다."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", reg)


def _path(version: str, app: str, subkey: str) -> str:
    return f"Software\\Microsoft\\Office\\{version}\\{app}\\Resiliency\\{subkey}"


class TestClearResiliencyMarkers:
    def test_non_windows_is_noop(self, monkeypatch):
        """비Windows에서는 레지스트리를 건드리지 않고 0을 반환한다."""
        monkeypatch.setattr(sys, "platform", "linux")
        assert office_resiliency.clear_resiliency_markers("EXCEL.EXE") == 0

    def test_unknown_exe_returns_zero(self, monkeypatch):
        reg = _FakeWinreg(existing=set())
        _install(monkeypatch, reg)
        assert office_resiliency.clear_resiliency_markers("NOTEPAD.EXE") == 0
        assert reg.deleted == []

    def test_deletes_disabled_and_startup_items(self, monkeypatch):
        """설치된 버전의 DisabledItems·StartupItems를 지운다."""
        reg = _FakeWinreg(existing={
            _path("16.0", "Excel", "DisabledItems"),
            _path("16.0", "Excel", "StartupItems"),
        })
        _install(monkeypatch, reg)

        assert office_resiliency.clear_resiliency_markers("EXCEL.EXE") == 2
        assert sorted(reg.deleted) == sorted([
            _path("16.0", "Excel", "DisabledItems"),
            _path("16.0", "Excel", "StartupItems"),
        ])

    def test_never_deletes_document_recovery(self, monkeypatch):
        """**회귀 방어**: DocumentRecovery는 사용자의 미저장 문서 복구 목록이므로
        존재하더라도 절대 열지도, 지우지도 않는다."""
        recovery = _path("16.0", "Excel", "DocumentRecovery")
        reg = _FakeWinreg(existing={
            _path("16.0", "Excel", "DisabledItems"),
            recovery,
        })
        _install(monkeypatch, reg)

        office_resiliency.clear_resiliency_markers("EXCEL.EXE")

        assert recovery not in reg.deleted
        assert recovery in reg.existing          # 그대로 남아 있어야 한다
        assert recovery not in reg.opened        # 열어보지도 않는다

    def test_missing_keys_are_skipped_silently(self, monkeypatch):
        """설치되지 않은 Office 버전의 키는 없으므로 조용히 건너뛴다."""
        reg = _FakeWinreg(existing={_path("14.0", "Word", "StartupItems")})
        _install(monkeypatch, reg)

        assert office_resiliency.clear_resiliency_markers("WINWORD.EXE") == 1
        assert reg.deleted == [_path("14.0", "Word", "StartupItems")]

    def test_permission_error_does_not_stop_other_keys(self, monkeypatch):
        """한 키가 권한 오류여도 나머지 키 정리는 계속한다(best-effort)."""
        denied = _path("16.0", "Excel", "DisabledItems")
        ok = _path("16.0", "Excel", "StartupItems")
        reg = _FakeWinreg(existing={denied, ok}, deny={denied})
        _install(monkeypatch, reg)

        assert office_resiliency.clear_resiliency_markers("EXCEL.EXE") == 1
        assert reg.deleted == [ok]

    def test_exe_name_is_case_insensitive(self, monkeypatch):
        reg = _FakeWinreg(existing={_path("16.0", "Word", "DisabledItems")})
        _install(monkeypatch, reg)
        assert office_resiliency.clear_resiliency_markers("winword.exe") == 1

    def test_deletes_across_all_known_office_versions(self, monkeypatch):
        reg = _FakeWinreg(existing={
            _path(v, "PowerPoint", "DisabledItems") for v in ("16.0", "15.0", "14.0")
        })
        _install(monkeypatch, reg)
        assert office_resiliency.clear_resiliency_markers("POWERPNT.EXE") == 3

    def test_deletes_nested_subkeys_first(self, monkeypatch):
        """하위 키가 있으면 DeleteKey가 실패하므로 재귀적으로 먼저 지운다."""
        parent = _path("16.0", "Excel", "DisabledItems")
        reg = _FakeWinreg(existing={parent, f"{parent}\\child", f"{parent}\\child\\grand"})
        _install(monkeypatch, reg)

        assert office_resiliency.clear_resiliency_markers("EXCEL.EXE") == 1
        # 자식이 부모보다 먼저 삭제돼야 한다
        assert reg.deleted.index(f"{parent}\\child\\grand") < reg.deleted.index(f"{parent}\\child")
        assert reg.deleted.index(f"{parent}\\child") < reg.deleted.index(parent)


class TestWatchdogIntegration:
    def test_terminate_stuck_office_clears_markers_after_kill(self, monkeypatch):
        """강제 종료로 표식이 생기므로, 종료 직후 바로 정리해야 다음 기동에서
        세이프모드 프롬프트 → 재행오버 루프가 이어지지 않는다."""
        import knowmate.secure.office_guard as og

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(og, "_enumerate_processes", lambda: [("EXCEL.EXE", 4242)])
        monkeypatch.setattr(og, "_owned_snapshot", lambda: {4242})
        monkeypatch.setattr(og, "_terminate_pid", lambda pid: None)

        cleared: list[str] = []
        monkeypatch.setattr(
            office_resiliency, "clear_resiliency_markers",
            lambda exe: cleared.append(exe) or 0,
        )

        assert og.terminate_stuck_office("EXCEL.EXE") == 1
        assert cleared == ["EXCEL.EXE"]

    def test_marker_cleanup_failure_does_not_propagate(self, monkeypatch):
        """워치독 daemon 타이머에서 호출되므로, 정리 실패가 밖으로 나가면 안 된다
        (타이머 스레드가 조용히 죽는다)."""
        import knowmate.secure.office_guard as og

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(og, "_enumerate_processes", lambda: [("EXCEL.EXE", 4242)])
        monkeypatch.setattr(og, "_owned_snapshot", lambda: {4242})
        monkeypatch.setattr(og, "_terminate_pid", lambda pid: None)

        def _boom(exe):
            raise RuntimeError("레지스트리 정리 실패(시뮬레이션)")

        monkeypatch.setattr(office_resiliency, "clear_resiliency_markers", _boom)

        assert og.terminate_stuck_office("EXCEL.EXE") == 1  # 예외 없이 정상 반환


class TestComReaderWiring:
    def test_dispatch_clears_markers_before_launching_office(self, monkeypatch):
        """세이프모드 프롬프트는 Dispatch가 반환하기 전에 뜨므로, 정리는 반드시
        Dispatch **직전**이어야 한다."""
        import knowmate.secure.com_reader as com_mod
        import knowmate.secure.office_guard as og

        order: list[str] = []
        monkeypatch.setattr(
            office_resiliency, "clear_resiliency_markers",
            lambda exe: order.append(f"clear:{exe}") or 0,
        )
        monkeypatch.setattr(og, "office_pids_live", lambda exe: set())
        monkeypatch.setattr(og, "register_owned_pids", lambda pids: None)

        class _FakeWin32Com:
            def Dispatch(self, prog_id):
                order.append(f"dispatch:{prog_id}")
                return object()

        com_mod._dispatch_and_own(_FakeWin32Com(), "Excel.Application", "EXCEL.EXE")

        assert order == ["clear:EXCEL.EXE", "dispatch:Excel.Application"]
