"""PowerPoint HWND 미지원 환경의 COM 창 식별 회귀 검증."""
import ctypes
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from knowmate.secure import com_reader, office_guard as guard


@pytest.mark.parametrize("kind,value_type,is_callable,is_none,reason", [
    ("none", "NoneType", False, True, "invalid_value"),
    ("text", "str", False, False, "invalid_value"),
    ("zero", "int", False, False, "zero"),
    ("method", "method", True, False, "invalid_value"),
    ("object", "ProtectedValue", True, False, "invalid_value"),
])
def test_hwnd_diagnostics_do_not_invoke_or_log_returned_values(caplog, kind, value_type, is_callable, is_none, reason):
    class ProtectedValue:
        def __repr__(self):
            raise AssertionError("HWND diagnostics must not format the returned object")

        def __call__(self):
            raise AssertionError("HWND diagnostics must not invoke the returned object")

    value = {
        "none": None, "text": "CONFIDENTIAL_HWND_VALUE", "zero": 0,
        "method": ProtectedValue().__call__, "object": ProtectedValue(),
    }[kind]
    caplog.set_level("INFO")
    assert guard._app_hwnd(SimpleNamespace(Hwnd=value, HWND=value), "POWERPNT.EXE") is None
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    for attr, message in zip(("Hwnd", "HWND"), messages):
        assert f"exe=POWERPNT.EXE property={attr} reason={reason}" in message
        assert f"value_type={value_type} callable={is_callable} is_none={is_none}" in message
    assert "CONFIDENTIAL" not in caplog.text


@pytest.fixture
def ppt_probe(monkeypatch):
    events = []
    document = SimpleNamespace(Close=lambda: events.append("close"))
    app = SimpleNamespace(
        Caption="PowerPoint",
        AutomationSecurity=1,
        Presentations=SimpleNamespace(Add=lambda with_window: events.append(("add", with_window)) or document),
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(guard, "_owned_pids", {})
    monkeypatch.setattr(guard, "_unverified_cleanup", {})
    monkeypatch.setattr(guard, "_cleanup_inflight", {})
    monkeypatch.setattr(guard, "_cleanup_retry_at", {})
    monkeypatch.setattr(com_reader._tls, "ownership_cleanup", {}, raising=False)
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", 20)])
    monkeypatch.setattr(guard, "_cached_processes", lambda: [("POWERPNT.EXE", 20)])
    monkeypatch.setattr(guard, "_pid_from_hwnd", lambda hwnd: 20 if hwnd == 123 else None)
    monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: 2020)

    def window(marker):
        assert app.Caption == marker
        assert marker.startswith("AegisDesk-ownership-")
        events.append("hwnd")
        return 123

    monkeypatch.setattr(guard, "_hwnd_for_caption_marker", window)
    return app, document, events


def test_ppt_without_hwnd_registers_bound_window_and_restores_caption(ppt_probe, caplog):
    app, _document, events = ppt_probe
    caplog.set_level("INFO")
    app.Caption = "CONFIDENTIAL original caption"
    assert guard.register_owned_app("POWERPNT.EXE", set(), app)
    assert app.Caption == "CONFIDENTIAL original caption"
    assert app.AutomationSecurity == 1
    assert events == [("add", -1), "hwnd", "hwnd", "close"]
    assert guard._owned_pids[20].terminable is False
    assert not guard.is_office_busy_for_ext(".ppt")
    assert "reason=unsupported" in caplog.text
    assert "Office 소유 등록 완료: exe=POWERPNT.EXE PID=20" in caplog.text
    assert "PowerPoint 확인용 창 정리 완료" in caplog.text
    assert "CONFIDENTIAL" not in caplog.text
    assert "AegisDesk-ownership-" not in caplog.text


@pytest.mark.parametrize("rejection", ["baseline", "wrong_exe", "unknown_identity", "changed_hwnd", "missing_window", "changed_caption"])
def test_ppt_fallback_never_relaxes_process_proof(monkeypatch, ppt_probe, rejection):
    app, _document, events = ppt_probe
    baseline = {20} if rejection == "baseline" else set()
    if rejection == "wrong_exe":
        monkeypatch.setattr(guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 20)])
    elif rejection == "unknown_identity":
        monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: None)
    elif rejection == "changed_hwnd":
        handles = iter([123, 456])
        monkeypatch.setattr(guard, "_hwnd_for_caption_marker", lambda marker: next(handles))
    elif rejection == "missing_window":
        monkeypatch.setattr(guard, "_hwnd_for_caption_marker", lambda marker: None)
    elif rejection == "changed_caption":
        def changed_caption(marker):
            app.Caption = "changed"
            return 123
        monkeypatch.setattr(guard, "_hwnd_for_caption_marker", changed_caption)
    assert not guard.register_owned_app("POWERPNT.EXE", baseline, app)
    assert not guard._owned_pids
    assert app.Caption == "PowerPoint"
    assert app.AutomationSecurity == 1
    if rejection == "baseline":
        assert not events  # 알려진 사용자 앱에는 확인용 창조차 만들지 않는다.
    else:
        assert events[-1] == "close"


@pytest.mark.parametrize("registered", [False, True])
def test_ppt_blank_close_failure_stops_retry_and_preserves_cleanup(monkeypatch, ppt_probe, registered, caplog):
    app, document, events = ppt_probe
    caplog.set_level("INFO")
    document.Close = lambda: (_ for _ in ()).throw(RuntimeError("CONFIDENTIAL close failed"))
    if not registered:
        monkeypatch.setattr(guard, "_hwnd_for_caption_marker", lambda marker: None)
    monkeypatch.setattr(guard, "office_pids_live", lambda exe: set())
    monkeypatch.setattr(guard, "prepare_powerpoint_dispatch", lambda: (set(), {}))
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    client = SimpleNamespace(Dispatch=lambda prog_id: app)
    with pytest.raises(guard.OfficeCleanupPendingError):
        com_reader._dispatch_and_own(client, "PowerPoint.Application", "POWERPNT.EXE")
    assert events.count(("add", -1)) == 1
    assert app.Caption == "PowerPoint"
    assert app.AutomationSecurity == 1
    assert "정리 실패: stage=blank_close" in caplog.text
    assert "Office 소유 등록 완료" not in caplog.text
    assert "PowerPoint 확인용 창 정리 완료" not in caplog.text
    assert "CONFIDENTIAL" not in caplog.text
    if registered:
        assert guard._owned_pids[20].cleanup_pending
    else:
        assert com_reader._tls.ownership_cleanup["POWERPNT.EXE"].document is document
        assert guard.unverified_cleanup_pending() == frozenset({"POWERPNT.EXE"})


def test_ppt_fatal_probe_skips_further_com_and_retains_blank(monkeypatch, ppt_probe, caplog):
    app, document, events = ppt_probe

    class Fatal(Exception):
        hresult = -2147023174  # RPC_S_SERVER_UNAVAILABLE

    monkeypatch.setattr(guard, "_hwnd_for_caption_marker", lambda marker: (_ for _ in ()).throw(Fatal()))
    monkeypatch.setattr(guard, "office_pids_live", lambda exe: set())
    monkeypatch.setattr(guard, "prepare_powerpoint_dispatch", lambda: (set(), {}))
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    client = SimpleNamespace(Dispatch=lambda prog_id: app)
    with pytest.raises(com_reader.OfficeComPoisonError):
        com_reader._dispatch_and_own(client, "PowerPoint.Application", "POWERPNT.EXE")
    assert events == [("add", -1)]
    assert app.AutomationSecurity == 3  # fatal RPC 뒤에는 복원 요청도 하지 않는다.
    assert app.Caption.startswith("AegisDesk-ownership-")  # no COM restoration after fatal RPC
    assert com_reader._tls.ownership_cleanup["POWERPNT.EXE"].document is document
    assert guard.unverified_cleanup_pending() == frozenset({"POWERPNT.EXE"})
    assert "stage=window_verify" in caplog.text
    assert "HRESULT=2147944122" in caplog.text
    assert "정리 연기: reason=rpc_disconnected" in caplog.text


def test_ppt_fallback_app_is_reused_for_next_document(monkeypatch, ppt_probe):
    app, _document, events = ppt_probe
    dispatched = []
    client = SimpleNamespace(Dispatch=lambda prog_id: dispatched.append(prog_id) or app)
    monkeypatch.setattr(guard, "office_pids_live", lambda exe: set())
    monkeypatch.setattr(guard, "prepare_powerpoint_dispatch", lambda: (set(), {}))
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    monkeypatch.setattr(com_reader, "_ensure_com_initialized", lambda: None)
    monkeypatch.setattr(com_reader, "_require_win32com", lambda: client)
    monkeypatch.setattr(com_reader._tls, "ppt", None, raising=False)
    assert com_reader._get_ppt_app() is app
    assert com_reader._get_ppt_app() is app
    assert dispatched == ["PowerPoint.Application"]
    assert events.count(("add", -1)) == 1
    assert not guard.unverified_cleanup_pending()


def test_ppt_caption_restore_failure_preserves_registered_cleanup(monkeypatch, ppt_probe):
    source, document, events = ppt_probe

    class App:
        Presentations = source.Presentations
        AutomationSecurity = 1
        caption = "PowerPoint"

        @property
        def Caption(self):
            return self.caption

        @Caption.setter
        def Caption(self, value):
            if value == "PowerPoint":
                raise RuntimeError("restore failed")
            self.caption = value

    app = App()
    monkeypatch.setattr(guard, "_hwnd_for_caption_marker", lambda marker: 123)
    with pytest.raises(guard.OfficeOwnershipCleanupError) as raised:
        guard.register_owned_app("POWERPNT.EXE", set(), app)
    assert raised.value.document is document
    assert raised.value.registered is not None
    assert guard._owned_pids[20].cleanup_pending
    assert app.AutomationSecurity == 1
    assert "close" not in events


@pytest.mark.parametrize(("titles", "enum_ok", "expected"), [
    ({123: "Presentation1 - unique-token", 456: "PowerPoint"}, True, 123),
    ({123: "PowerPoint"}, True, None),
    ({123: "unique-token", 456: "unique-token"}, True, None),
    ({123: "unique-token"}, False, None),
])
def test_native_marker_lookup_requires_complete_unique_result(monkeypatch, titles, enum_ok, expected):
    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    def enumerate_windows(callback, param):
        for hwnd in titles:
            callback(hwnd, param)
        return enum_ok

    def get_text(hwnd, buffer, capacity):
        buffer.value = titles[hwnd][:capacity - 1]
        return len(buffer.value)

    user32 = SimpleNamespace(
        EnumWindows=Function(enumerate_windows),
        GetWindowTextLengthW=Function(lambda hwnd: len(titles[hwnd])),
        GetWindowTextW=Function(get_text),
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    assert guard._hwnd_for_caption_marker("unique-token") == expected


@pytest.fixture
def ppt_flow(monkeypatch):
    """AutoReader부터 실제 소유 가드와 문서 Open/Close까지 가짜 OS/COM으로 연결한다."""
    from knowmate.secure import AutoReader

    events = []
    live = []
    identity = {20: 2020}

    class App:
        security = 1
        caption = "PowerPoint"

        @property
        def AutomationSecurity(self):
            events.append("security_read")
            return self.security

        @AutomationSecurity.setter
        def AutomationSecurity(self, value):
            events.append(("security_set", value))
            self.security = value

        @property
        def Caption(self):
            return self.caption

        @Caption.setter
        def Caption(self, value):
            events.append("caption_set")
            self.caption = value

        def Quit(self):
            raise AssertionError("PowerPoint must never receive Quit")

    app = App()
    blank = SimpleNamespace(Close=lambda: events.append("blank_close"))

    def open_target(path, **kwargs):
        assert kwargs == {"ReadOnly": True, "WithWindow": False}
        assert app.AutomationSecurity == 3
        events.append("target_open")
        return SimpleNamespace(Slides=[], Close=lambda: events.append("target_close"))

    app.Presentations = SimpleNamespace(
        Add=lambda with_window: events.append("blank_add") or blank,
        Open=open_target,
    )

    def dispatch(prog_id):
        assert prog_id == "PowerPoint.Application"
        events.append("dispatch")
        if not live:
            live.append(("POWERPNT.EXE", 20))
        return app

    monkeypatch.setattr(sys, "platform", "win32")
    for name in ("_owned_pids", "_unverified_cleanup", "_cleanup_inflight", "_cleanup_retry_at"):
        monkeypatch.setattr(guard, name, {})
    monkeypatch.setattr(guard, "_cache", {"ts": time.monotonic(), "procs": []})
    monkeypatch.setattr(guard, "_enumerate_processes", lambda: list(live))
    monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: identity.get(pid))
    monkeypatch.setattr(guard, "_pid_from_hwnd", lambda hwnd: 20 if hwnd == 123 else None)
    monkeypatch.setattr(guard, "_hwnd_for_caption_marker", lambda marker: 123 if app.Caption == marker else None)
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    monkeypatch.setattr(com_reader, "_ensure_com_initialized", lambda: None)
    monkeypatch.setattr(com_reader, "_require_win32com", lambda: SimpleNamespace(Dispatch=dispatch))
    monkeypatch.setattr(com_reader.time, "sleep", lambda seconds: None)
    for name in ("word", "excel", "ppt"):
        monkeypatch.setattr(com_reader._tls, name, None, raising=False)
    monkeypatch.setattr(com_reader._tls, "ownership_cleanup", {}, raising=False)
    return SimpleNamespace(app=app, blank=blank, events=events, live=live, identity=identity, reader=AutoReader())


@pytest.mark.parametrize("cache_has_external", [False, True])
def test_external_ppt_is_untouched_even_when_auto_reader_cache_is_stale(ppt_flow, cache_has_external):
    ppt_flow.live.append(("POWERPNT.EXE", 20))
    if cache_has_external:
        guard._cache["procs"] = list(ppt_flow.live)
    with pytest.raises(guard.OfficeBusyError):
        ppt_flow.reader.extract("external.ppt")
    assert ppt_flow.events == []  # Dispatch뿐 아니라 security/Caption 조회·변경과 Add/Open도 없다.
    assert ppt_flow.app.security == 1
    assert not guard._owned_pids
    assert not guard.unverified_cleanup_pending()


@pytest.mark.parametrize("documents_before_cleanup", [1, 30])
def test_ppt_reconnects_after_cycle_or_periodic_reference_release(ppt_flow, documents_before_cleanup, caplog):
    caplog.set_level("INFO")
    for i in range(documents_before_cleanup):
        assert ppt_flow.reader.extract(f"before_{i}.ppt") == ""
    record = guard._owned_pids[20]
    assert com_reader.quit_com_apps(grace_sec=0).successful
    assert com_reader._tls.ppt is None
    assert guard._owned_pids[20] is record
    assert ppt_flow.reader.extract("after.ppt") == ""
    assert guard._owned_pids[20] is record  # 이전 generation을 바꾸거나 종료 대기를 덮어쓰지 않는다.
    assert ppt_flow.events.count("dispatch") == 2
    assert ppt_flow.events.count("blank_add") == 2
    assert ppt_flow.events.count("blank_close") == 2
    assert ppt_flow.events.count("target_open") == documents_before_cleanup + 1
    assert ppt_flow.events.count("target_close") == documents_before_cleanup + 1
    assert "PowerPoint 기존 소유 재연결 확인" in caplog.text
    assert not guard.unverified_cleanup_pending()


@pytest.mark.parametrize("unavailable", ["processes", "identity", "reused_pid"])
def test_ppt_preflight_unknown_state_or_reused_pid_never_dispatches(monkeypatch, ppt_flow, unavailable):
    if unavailable == "processes":
        monkeypatch.setattr(guard, "_enumerate_processes", lambda: None)
    else:
        ppt_flow.live.append(("POWERPNT.EXE", 20))
        guard._owned_pids[20] = guard.OwnedOfficeProcess("POWERPNT.EXE", 2020, False, 7)
        ppt_flow.identity[20] = None if unavailable == "identity" else 3030
    expected = guard.OfficeBusyError if unavailable == "reused_pid" else guard.OfficeCleanupPendingError
    with pytest.raises(expected):
        com_reader._dispatch_and_own(com_reader._require_win32com(), "PowerPoint.Application", "POWERPNT.EXE")
    assert ppt_flow.events == []
    assert not guard.unverified_cleanup_pending()


@pytest.mark.parametrize("pending", ["record", "inflight", "unverified"])
def test_pending_ppt_never_dispatches_or_changes_settings(ppt_flow, pending):
    ppt_flow.live.append(("POWERPNT.EXE", 20))
    guard._owned_pids[20] = guard.OwnedOfficeProcess("POWERPNT.EXE", 2020, False, 7)
    if pending == "record":
        guard._mark_owned_cleanup_pending(20, guard._owned_pids[20])
        guard._cleanup_retry_at["POWERPNT.EXE"] = time.monotonic() + 30
    elif pending == "inflight":
        guard.begin_shutdown_cleanup("POWERPNT.EXE")
    else:
        guard.begin_unverified_cleanup("POWERPNT.EXE")
    tokens = guard.unverified_cleanup_pending()
    with pytest.raises(guard.OfficeCleanupPendingError):
        com_reader._dispatch_and_own(com_reader._require_win32com(), "PowerPoint.Application", "POWERPNT.EXE")
    assert ppt_flow.events == []
    assert guard.unverified_cleanup_pending() == tokens
    assert not com_reader._tls.ownership_cleanup


@pytest.mark.parametrize("change", ["generation", "pending", "inflight", "unverified"])
def test_reconnect_confirmation_never_overwrites_concurrent_registry_state(monkeypatch, ppt_probe, change):
    app, _document, events = ppt_probe
    previous = guard.OwnedOfficeProcess("POWERPNT.EXE", 2020, False, 7)
    guard._owned_pids[20] = previous
    reads = []

    def hwnd(marker):
        reads.append(marker)
        if len(reads) == 2:
            if change == "generation":
                guard._owned_pids[20] = replace(previous, generation=8)
            elif change == "pending":
                guard._mark_owned_cleanup_pending(20, previous)
            elif change == "inflight":
                guard.begin_shutdown_cleanup("POWERPNT.EXE")
            else:
                guard.begin_unverified_cleanup("POWERPNT.EXE")
        return 123

    monkeypatch.setattr(guard, "_hwnd_for_caption_marker", hwnd)
    assert not guard.register_owned_app("POWERPNT.EXE", {20}, app, {20: previous})
    assert guard._owned_pids[20].generation == (8 if change == "generation" else 7)
    assert guard._owned_pids[20].cleanup_pending is (change == "pending")
    assert app.AutomationSecurity == 1
    assert events[-1] == "close"


@pytest.mark.parametrize("change", ["generation", "pending", "inflight", "unverified", "identity"])
def test_reconnect_rejects_changes_before_mutable_probe(monkeypatch, ppt_probe, change):
    app, _document, events = ppt_probe
    previous = guard.OwnedOfficeProcess("POWERPNT.EXE", 2020, False, 7)
    guard._owned_pids[20] = previous
    if change == "generation":
        guard._owned_pids[20] = replace(previous, generation=8)
    elif change == "pending":
        guard._mark_owned_cleanup_pending(20, previous)
    elif change == "inflight":
        guard.begin_shutdown_cleanup("POWERPNT.EXE")
    elif change == "unverified":
        guard.begin_unverified_cleanup("POWERPNT.EXE")
    else:
        monkeypatch.setattr(guard, "_process_creation_identity", lambda pid: 3030)
    assert not guard.register_owned_app("POWERPNT.EXE", {20}, app, {20: previous})
    assert not events
    assert app.AutomationSecurity == 1
    assert app.Caption == "PowerPoint"


def test_ppt_security_restored_after_nonfatal_probe_error(monkeypatch, ppt_probe):
    app, _document, events = ppt_probe
    app.Presentations.Add = lambda with_window: (_ for _ in ()).throw(RuntimeError("create rejected"))
    with pytest.raises(guard.OfficeOwnershipProbeError):
        guard.register_owned_app("POWERPNT.EXE", set(), app)
    assert app.AutomationSecurity == 1
    assert app.Caption == "PowerPoint"
    assert not events


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("fatal_restore", [False, True])
def test_ppt_close_and_security_restore_errors_preserve_causes_and_gate(monkeypatch, ppt_probe, registered, fatal_restore):
    source, document, events = ppt_probe
    close_error = RuntimeError("close rejected")
    document.Close = lambda: (_ for _ in ()).throw(close_error)

    class Fatal(Exception):
        hresult = -2147023174

    fatal = Fatal()
    restore_error = RuntimeError("restore rejected")
    if fatal_restore:
        restore_error.__cause__ = fatal

    class App:
        Caption = "PowerPoint"
        Presentations = source.Presentations
        security = 1

        @property
        def AutomationSecurity(self):
            return self.security

        @AutomationSecurity.setter
        def AutomationSecurity(self, value):
            if value == 1:
                raise restore_error
            self.security = value

    app = App()
    monkeypatch.setattr(guard, "prepare_powerpoint_dispatch", lambda: (set(), {}))
    monkeypatch.setattr(guard, "_hwnd_for_caption_marker", lambda marker: 123 if registered else None)
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    expected = com_reader.OfficeComPoisonError if fatal_restore else guard.OfficeCleanupPendingError
    with pytest.raises(expected) as raised:
        com_reader._dispatch_and_own(SimpleNamespace(Dispatch=lambda prog_id: app), "PowerPoint.Application", "POWERPNT.EXE")
    assert events.count(("add", -1)) == 1
    assert app.Caption == "PowerPoint"
    if fatal_restore:
        assert raised.value.__cause__ is fatal
        assert restore_error.__cause__ is fatal  # 앞선 close 오류로 fatal cause를 덮어쓰지 않는다.
    else:
        assert raised.value.__cause__.close_error is close_error
        assert raised.value.__cause__.restore_error is restore_error
    if registered:
        assert guard._owned_pids[20].cleanup_pending
    if not registered or fatal_restore:
        assert com_reader._tls.ownership_cleanup["POWERPNT.EXE"].app is app
        assert com_reader._tls.ownership_cleanup["POWERPNT.EXE"].document is document
        assert guard.unverified_cleanup_pending() == frozenset({"POWERPNT.EXE"})


def test_fatal_blank_close_skips_security_restore(monkeypatch, ppt_probe):
    app, document, _events = ppt_probe

    class Fatal(Exception):
        hresult = -2147023174

    document.Close = lambda: (_ for _ in ()).throw(Fatal())
    with pytest.raises(guard.OfficeOwnershipCleanupError):
        guard.register_owned_app("POWERPNT.EXE", set(), app)
    assert app.Caption == "PowerPoint"
    assert app.AutomationSecurity == 3
