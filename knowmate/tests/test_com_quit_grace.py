"""COM 종료 유예(Quit → 대기 → 잔존만 강제종료) 단위 테스트.

배경: `app.Quit()`은 종료를 요청만 하고 즉시 반환한다. 유예 없이 그 직후 바로
프로세스를 조회하면, 스스로 정리 중인(임시파일·애드인 정리 등) Office까지 매번
강제종료로 오판해 세이프모드 유발 표식이 반복 생성된다(레이스). `wait_for_owned_exit`
(office_guard.py)로 실제 종료를 기다리고, `quit_com_apps`(com_reader.py)가 남은
프로세스만 강제종료하도록 고쳤다.

실제 `ctypes.windll`(OpenProcess/WaitForSingleObject)은 Windows에만 있어 사외
Linux에서 직접 검증할 수 없다 — 이 저장소의 기존 관례(office_guard의 win32 ctypes
내부는 `sys.platform != "win32"` 가드만 테스트하고, 실제 분기는 의존성 주입으로
검증)를 따라 `wait_fn` 주입으로 quit_com_apps의 분기 로직을 검증한다.
"""
import sys

from knowmate.secure import com_reader, office_guard


class TestWaitForOwnedExitPlatformGuard:
    """ctypes.windll에 닿지 않는 플랫폼 가드만 사외에서 직접 검증 가능하다."""

    def test_empty_owned_returns_immediately(self):
        still_alive, elapsed = office_guard.wait_for_owned_exit(set(), 5.0)
        assert still_alive == set()
        assert elapsed == 0.0

    def test_non_windows_returns_owned_unresolved(self, monkeypatch):
        """비Windows에서는 대기하지 않고 owned를 그대로(미해결) 반환한다 —
        판단 불가 시 보수적으로 "살아있음" 취급하는 이 모듈의 기존 관례와 동일."""
        monkeypatch.setattr(sys, "platform", "linux")
        still_alive, elapsed = office_guard.wait_for_owned_exit({111, 222}, 5.0)
        assert still_alive == {111, 222}
        assert elapsed == 0.0


class _FakeApp:
    """win32com Dispatch가 반환하는 Office 애플리케이션 객체 대역."""

    def __init__(self, quit_raises: bool = False):
        self.quit_called = False
        self._quit_raises = quit_raises

    def Quit(self):
        self.quit_called = True
        if self._quit_raises:
            raise RuntimeError("Quit 실패(시뮬레이션)")


class TestQuitComAppsGraceLogic:
    """quit_com_apps()의 유예 분기를 wait_fn 주입으로 검증한다(ctypes 미의존)."""

    def _install_owned(self, monkeypatch, owned: set):
        monkeypatch.setattr(office_guard, "clear_owned_pids", lambda: set(owned))

    def test_no_owned_pids_skips_wait_and_terminate(self, monkeypatch):
        """소유 PID가 없으면 대기도 강제종료도 하지 않는다."""
        self._install_owned(monkeypatch, set())
        wait_calls = []
        terminate_calls = []
        monkeypatch.setattr(
            office_guard, "wait_for_owned_exit",
            lambda owned, timeout: (wait_calls.append((owned, timeout)) or (set(), 0.0)),
        )
        monkeypatch.setattr(
            office_guard, "terminate_owned_office_processes",
            lambda owned: terminate_calls.append(owned),
        )

        com_reader.quit_com_apps(grace_sec=5.0)

        assert wait_calls == []
        assert terminate_calls == []

    def test_all_exit_within_grace_skips_terminate(self, monkeypatch):
        """유예 안에 전부 스스로 종료하면 강제종료를 호출하지 않는다(핵심 회귀 방어)."""
        self._install_owned(monkeypatch, {111, 222})
        terminate_calls = []
        monkeypatch.setattr(
            office_guard, "wait_for_owned_exit",
            lambda owned, timeout: (set(), 0.8),  # 아무도 안 남음, 0.8초 만에 종료
        )
        monkeypatch.setattr(
            office_guard, "terminate_owned_office_processes",
            lambda owned: terminate_calls.append(owned),
        )

        com_reader.quit_com_apps(grace_sec=5.0)

        assert terminate_calls == []

    def test_still_alive_after_grace_terminates_only_those(self, monkeypatch):
        """유예 초과 후 남은 PID만 강제종료한다(스스로 종료한 것은 건드리지 않음)."""
        self._install_owned(monkeypatch, {111, 222, 333})
        terminate_calls = []
        monkeypatch.setattr(
            office_guard, "wait_for_owned_exit",
            lambda owned, timeout: ({222}, 5.0),  # 222만 유예 끝까지 살아남음
        )
        monkeypatch.setattr(
            office_guard, "terminate_owned_office_processes",
            lambda owned: terminate_calls.append(owned),
        )

        com_reader.quit_com_apps(grace_sec=5.0)

        assert terminate_calls == [{222}]

    def test_grace_sec_zero_skips_wait_fn_entirely(self, monkeypatch):
        """grace_sec=0이면 대기 없이 즉시 강제종료(이전 동작과 동일 — 비상 스위치)."""
        self._install_owned(monkeypatch, {111, 222})
        wait_calls = []
        terminate_calls = []
        monkeypatch.setattr(
            office_guard, "wait_for_owned_exit",
            lambda owned, timeout: (wait_calls.append((owned, timeout)) or (set(), 0.0)),
        )
        monkeypatch.setattr(
            office_guard, "terminate_owned_office_processes",
            lambda owned: terminate_calls.append(owned),
        )

        com_reader.quit_com_apps(grace_sec=0)

        assert wait_calls == []  # 대기 함수 자체를 호출하지 않음
        assert terminate_calls == [{111, 222}]  # owned 전체를 즉시 강제종료

    def test_wait_fn_parameter_overrides_default(self, monkeypatch):
        """wait_fn을 직접 주입하면 office_guard.wait_for_owned_exit 대신 그것을 쓴다."""
        self._install_owned(monkeypatch, {111})
        terminate_calls = []
        monkeypatch.setattr(
            office_guard, "terminate_owned_office_processes",
            lambda owned: terminate_calls.append(owned),
        )
        injected_calls = []

        def _injected_wait(owned, timeout):
            injected_calls.append((owned, timeout))
            return set(), 0.1

        com_reader.quit_com_apps(grace_sec=3.0, wait_fn=_injected_wait)

        assert injected_calls == [({111}, 3.0)]
        assert terminate_calls == []

    def test_apps_quit_called_before_owned_cleared(self, monkeypatch):
        """word/excel/ppt에 앱이 설정돼 있으면 각각 Quit()을 호출하고 thread-local을 비운다."""
        word_app = _FakeApp()
        excel_app = _FakeApp()
        monkeypatch.setattr(com_reader._tls, "word", word_app, raising=False)
        monkeypatch.setattr(com_reader._tls, "excel", excel_app, raising=False)
        self._install_owned(monkeypatch, set())

        com_reader.quit_com_apps(grace_sec=5.0)

        assert word_app.quit_called is True
        assert excel_app.quit_called is True
        assert getattr(com_reader._tls, "word", None) is None
        assert getattr(com_reader._tls, "excel", None) is None

    def test_quit_exception_does_not_prevent_cleanup(self, monkeypatch):
        """Quit() 자체가 예외를 던져도 소유 PID 정리는 계속 진행된다."""
        word_app = _FakeApp(quit_raises=True)
        monkeypatch.setattr(com_reader._tls, "word", word_app, raising=False)
        self._install_owned(monkeypatch, {111})
        terminate_calls = []
        monkeypatch.setattr(
            office_guard, "wait_for_owned_exit",
            lambda owned, timeout: ({111}, 5.0),
        )
        monkeypatch.setattr(
            office_guard, "terminate_owned_office_processes",
            lambda owned: terminate_calls.append(owned),
        )

        com_reader.quit_com_apps(grace_sec=5.0)  # 예외 없이 완료

        assert getattr(com_reader._tls, "word", None) is None
        assert terminate_calls == [{111}]
