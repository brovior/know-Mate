"""빌드 자체 점검(knowmate/app/selftest.py) 단위 테스트.

이 모듈의 목적은 "exe에서만 깨지는 번들 누락"을 배포 전에 잡는 것이라, 정작
사외 Linux에서는 실제 번들도 PyQt6 GUI도 없어 통합 검증이 불가능하다. 그래서
**개별 점검 함수의 판정 로직**과 **run_selftest의 집계·종료 코드 규약**을
주입으로 검증한다(selftest.py의 모든 무거운 import가 함수 안에 있어 가능).

가장 중요한 회귀 방어: **한 점검이 실패하거나 예외를 던져도 나머지 점검이
계속 실행되어야 한다** — 빌드 담당자가 누락을 한 번에 다 보고 고칠 수 있어야
하고, 점검 자체의 예외가 '조용한 통과'로 둔갑하면 게이트 의미가 사라진다.
"""
import sys

import pytest

from knowmate.app import selftest


class TestRunSelftestAggregation:
    def _patch_checks(self, monkeypatch, checks):
        """run_selftest가 도는 점검 목록을 통째로 교체한다."""
        monkeypatch.setattr(selftest, "_check_bundled_resources", checks[0])
        monkeypatch.setattr(selftest, "_check_webengine_process", checks[1])
        monkeypatch.setattr(selftest, "_check_lazy_imports", checks[2])
        monkeypatch.setattr(selftest, "_check_lancedb_version", checks[3])
        monkeypatch.setattr(selftest, "_check_log_dir_writable", checks[4])

    def test_all_pass_returns_zero(self, monkeypatch):
        noop = lambda failures: None
        self._patch_checks(monkeypatch, [noop] * 5)
        assert selftest.run_selftest() == 0

    def test_any_failure_returns_one(self, monkeypatch):
        noop = lambda failures: None
        fail = lambda failures: failures.append("누락 있음")
        self._patch_checks(monkeypatch, [noop, fail, noop, noop, noop])
        assert selftest.run_selftest() == 1

    def test_all_checks_run_even_after_a_failure(self, monkeypatch):
        """앞 점검이 실패해도 뒤 점검을 건너뛰지 않는다(누락을 한 번에 모아 보기)."""
        called = []

        def _make(name, should_fail):
            def _check(failures):
                called.append(name)
                if should_fail:
                    failures.append(f"{name} 실패")
            return _check

        self._patch_checks(monkeypatch, [
            _make("a", True), _make("b", False), _make("c", True),
            _make("d", False), _make("e", False),
        ])
        assert selftest.run_selftest() == 1
        assert called == ["a", "b", "c", "d", "e"]

    def test_check_raising_is_treated_as_failure_not_silent_pass(self, monkeypatch):
        """점검 함수 자체가 예외를 던지면 실패로 집계한다 — 조용히 통과하면 게이트가 무의미."""
        noop = lambda failures: None

        def _boom(failures):
            raise RuntimeError("점검 중 폭발(시뮬레이션)")

        self._patch_checks(monkeypatch, [noop, _boom, noop, noop, noop])
        assert selftest.run_selftest() == 1

    def test_check_raising_does_not_stop_later_checks(self, monkeypatch):
        called = []
        noop = lambda failures: None

        def _boom(failures):
            raise RuntimeError("boom")

        def _later(failures):
            called.append("later")

        self._patch_checks(monkeypatch, [_boom, noop, noop, noop, _later])
        selftest.run_selftest()
        assert called == ["later"]

    def test_failure_detail_written_to_stderr(self, monkeypatch, capsys):
        noop = lambda failures: None
        self._patch_checks(monkeypatch, [
            lambda failures: failures.append("UI 폴더 없음"), noop, noop, noop, noop,
        ])
        selftest.run_selftest()
        err = capsys.readouterr().err
        assert "UI 폴더 없음" in err
        assert "실패 1건" in err

    def test_non_frozen_run_warns(self, monkeypatch, capsys):
        """소스 실행(frozen 아님)에서 통과해도 '번들 검증이 아니다'를 알린다."""
        noop = lambda failures: None
        self._patch_checks(monkeypatch, [noop] * 5)
        monkeypatch.delattr(sys, "frozen", raising=False)

        assert selftest.run_selftest() == 0
        err = capsys.readouterr().err
        assert "frozen이 아닌" in err


class TestIndividualChecks:
    def test_log_dir_writable_passes_in_normal_env(self):
        failures = []
        selftest._check_log_dir_writable(failures)
        assert failures == []

    def test_log_dir_failure_is_reported(self, monkeypatch):
        import knowmate.config as config_module

        def _boom():
            raise PermissionError("access denied")

        monkeypatch.setattr(config_module, "get_data_dir", _boom)
        failures = []
        selftest._check_log_dir_writable(failures)
        assert len(failures) == 1
        assert "로그 폴더 쓰기 불가" in failures[0]

    def test_lancedb_version_mismatch_is_reported(self, monkeypatch):
        import knowmate.lancedb_compat as compat

        monkeypatch.setattr(compat, "check_lancedb_version", lambda: "버전 불일치(시뮬레이션)")
        failures = []
        selftest._check_lancedb_version(failures)
        assert len(failures) == 1
        assert "lancedb 버전" in failures[0]

    def test_lazy_import_missing_module_is_reported(self, monkeypatch):
        """hiddenimports에서 빠진 모듈은 import 실패로 잡힌다(win32timezone 사고 회귀 방어)."""
        monkeypatch.setattr(selftest, "_COMMON_IMPORTS", ("modulethatdoesnotexist_xyz",))
        monkeypatch.setattr(selftest, "_WINDOWS_LAZY_IMPORTS", ())
        failures = []
        selftest._check_lazy_imports(failures)
        assert len(failures) == 1
        assert "modulethatdoesnotexist_xyz" in failures[0]

    def test_windows_only_imports_skipped_off_windows(self, monkeypatch):
        """비Windows에서는 pywin32 계열을 점검하지 않는다(사외 테스트 통과 조건)."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(selftest, "_COMMON_IMPORTS", ())
        monkeypatch.setattr(selftest, "_WINDOWS_LAZY_IMPORTS", ("win32timezone",))
        failures = []
        selftest._check_lazy_imports(failures)
        assert failures == []
