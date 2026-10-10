"""Office RPC poison 격리의 Linux fake 회귀 테스트."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from knowmate.collector import failure_state
from knowmate.secure import com_reader, office_guard


@pytest.fixture(autouse=True)
def _isolate_unverified_cleanup(monkeypatch):
    """미검증 정리 상태와 스레드 COM 참조를 테스트 사이에 분리한다."""
    monkeypatch.setattr(office_guard, "_unverified_cleanup", {})
    monkeypatch.setattr(com_reader._tls, "ownership_cleanup", {}, raising=False)


def test_windows_process_enumerator_uses_available_ansi_toolhelp_exports(monkeypatch):
    """ANSI Toolhelp는 A 접미사 없는 Process32First/Next를 사용한다."""
    import ctypes

    class _Fn:
        def __init__(self, result):
            self.result = result

        def __call__(self, *args):
            return self.result

    kernel32 = SimpleNamespace(
        CreateToolhelp32Snapshot=_Fn(1),
        Process32First=_Fn(False),
        Process32Next=_Fn(False),
        CloseHandle=_Fn(True),
        GetLastError=_Fn(18),
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False,
    )

    assert office_guard._enumerate_processes() == []


@pytest.mark.parametrize("first_ok", [False, True])
def test_process_enumeration_error_preserves_unverified_gate(monkeypatch, first_ok):
    """First/Next가 EOF 이외 오류면 빈 목록이나 부분 목록으로 gate를 해제하지 않는다."""
    import ctypes

    class _Fn:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    def first(handle, entry):
        entry._obj.szExeFile = b"EXCEL.EXE"
        entry._obj.th32ProcessID = 20
        return first_ok

    kernel32 = SimpleNamespace(
        CreateToolhelp32Snapshot=_Fn(lambda *args: 1),
        Process32First=_Fn(first),
        Process32Next=_Fn(lambda *args: False),
        CloseHandle=_Fn(lambda *args: True),
        GetLastError=_Fn(lambda: 5),  # ERROR_ACCESS_DENIED
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    office_guard.begin_unverified_cleanup("WINWORD.EXE")
    assert office_guard._enumerate_processes() is None
    assert not office_guard.office_processes_absent("WINWORD.EXE")
    office_guard.prune_unverified_cleanup()
    assert office_guard.unverified_cleanup_pending() == frozenset({"WINWORD.EXE"})
    kernel32.GetLastError = _Fn(lambda: 18)
    assert office_guard._enumerate_processes() == ([("EXCEL.EXE", 20)] if first_ok else [])
    office_guard.prune_unverified_cleanup()
    assert not office_guard.unverified_cleanup_pending()


class _OpenCollection:
    """지정한 Open 반환값을 내는 최소 COM collection 대역."""

    def __init__(self, result):
        self._result = result

    def Open(self, *args, **kwargs):
        return self._result


class _App:
    """Open(None) 경로용 최소 Office 앱 대역."""

    def __init__(self, collection_name: str):
        setattr(self, collection_name, _OpenCollection(None))


@pytest.mark.parametrize(("reader", "getter", "app", "exe"), [
    (lambda: com_reader.WordComReader(), "_get_word_app", _App("Documents"), "WINWORD.EXE"),
    (lambda: com_reader.ExcelComReader(), "_get_excel_app", _App("Workbooks"), "EXCEL.EXE"),
    (lambda: com_reader.PowerPointComReader(), "_get_ppt_app", _App("Presentations"), "POWERPNT.EXE"),
])
def test_open_none_is_poison_at_open_stage(monkeypatch, reader, getter, app, exe):
    """세 Office Open(None)은 후속 속성 오류 대신 open poison으로 끝난다."""
    monkeypatch.setattr(com_reader, getter, lambda: app)
    with pytest.raises(com_reader.OfficeComPoisonError) as raised:
        reader().parse("C:/private/document.xls")
    assert raised.value.exe_name == exe
    # StageTimer가 open 안에서 난 예외를 기록해야 failure_state가 OPEN_ERROR로 남긴다.
    stage = com_reader.com_stage.take_last_failed_stage()
    assert stage == "open"


@pytest.mark.parametrize("value", [0x800706BA, -2147023174, 0x800706BE, -2147023170])
def test_fatal_hresult_is_signed_unsigned_equivalent(value):
    """fatal RPC HRESULT는 부호 표기와 무관하게 poison으로 판정한다."""
    assert com_reader.is_fatal_transport_hresult(value)


@pytest.mark.parametrize("value", [0x80010001, 0x8001010A, 0x80004005, 0x80020009])
def test_busy_and_general_hresult_are_not_poison(value):
    """busy/retry와 일반 파일 오류는 TLS 폐기 대상이 아니다."""
    assert not com_reader.is_fatal_transport_hresult(value)


def test_poison_hresult_is_preserved_in_failure_state():
    """scheduler가 남기는 실패 상태는 poison의 HRESULT를 보존한다."""
    exc = com_reader.OfficeComPoisonError("EXCEL.EXE", -2147023174)
    kind, code = failure_state.classify(exc, failed_stage="open")
    assert kind == failure_state.KIND_OPEN_ERROR
    assert code == "0x800706BA"


def test_nonfatal_read_error_keeps_tls_app_and_closes_document(monkeypatch):
    """파일별 read 오류는 앱을 재사용하고 문서 Close는 그대로 수행한다."""
    class _BrokenSheet:
        Name = "S"
        UsedRange = type("U", (), {"Row": 1, "Column": 1,
            "Rows": type("R", (), {"Count": 1})(), "Columns": type("C", (), {"Count": 1})()})()
        def Cells(self, row, col):
            return (row, col)
        def Range(self, *args):
            raise RuntimeError("file specific")

    class _Workbook:
        Sheets = [_BrokenSheet()]
        closed = False
        def Close(self, save):
            self.closed = True

    wb = _Workbook()
    app = _App("Workbooks")
    app.Workbooks = _OpenCollection(wb)
    sentinel = object()
    monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)
    monkeypatch.setattr(com_reader._tls, "excel", sentinel, raising=False)
    with pytest.raises(RuntimeError):
        com_reader.ExcelComReader().parse("C:/private/x.xls")
    assert wb.closed is True
    assert com_reader._tls.excel is sentinel


def test_recover_poison_keeps_other_owned_apps(monkeypatch):
    """즉시 복구는 해당 EXE의 확인된 PID만 제거하고 다른 앱은 보존한다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    office_guard._owned_pids.update({
        10: office_guard.OwnedOfficeProcess("EXCEL.EXE", 1010),
        20: office_guard.OwnedOfficeProcess("WINWORD.EXE", 2020),
    })
    live = [("EXCEL.EXE", 10), ("WINWORD.EXE", 20), ("EXCEL.EXE", 99)]
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: list(live))
    killed = []
    monkeypatch.setattr(
        office_guard, "_terminate_and_confirm",
        lambda pid, record, timeout: (killed.append(pid) or "terminated"),
    )
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: {10: 1010, 20: 2020}.get(pid))
    monkeypatch.setattr(office_guard, "wait_for_owned_exit", lambda pids, timeout: (set(), 0.0))
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    assert office_guard.recover_poisoned_office("EXCEL.EXE") is True
    assert killed == [10]
    assert office_guard._owned_pids == {20: office_guard.OwnedOfficeProcess("WINWORD.EXE", 2020)}


def test_register_owned_app_requires_new_matching_hwnd_pid(monkeypatch):
    """baseline 밖의 HWND PID가 예상 EXE일 때만 자동화 소유로 등록한다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    monkeypatch.setattr(
        office_guard,
        "_enumerate_processes",
        lambda: [("EXCEL.EXE", 10), ("EXCEL.EXE", 20)],
    )
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20)
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 2020)
    app = type("App", (), {"Hwnd": 1234})()

    assert office_guard.register_owned_app("EXCEL.EXE", {10}, app) is True
    assert office_guard._owned_pids == {20: office_guard.OwnedOfficeProcess("EXCEL.EXE", 2020)}


@pytest.mark.parametrize("baseline", [set(), {20}])
def test_word_without_application_hwnd_uses_unsaved_window(monkeypatch, baseline):
    """실제 Word처럼 Application.Hwnd가 없어도 창으로 검증하고 빈 문서를 닫는다."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(office_guard, "_owned_pids", {})
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("WINWORD.EXE", 20)])
    monkeypatch.setattr(office_guard, "_cached_processes", lambda: [("WINWORD.EXE", 20)])
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20 if hwnd == 123 else None)
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 2020)
    closed = []
    document = SimpleNamespace(
        Windows=SimpleNamespace(Item=lambda index: SimpleNamespace(Hwnd=123)),
        Close=lambda save: closed.append(save),
    )
    app = SimpleNamespace(Documents=SimpleNamespace(Add=lambda: document))
    assert office_guard.register_owned_app("WINWORD.EXE", baseline, app) is (not baseline)
    assert app.AutomationSecurity == 3
    assert closed == [False]
    assert office_guard.is_office_busy_for_ext(".doc") is bool(baseline)


def test_hwnd_unsupported_com_member_tries_uppercase(monkeypatch):
    """late binding의 member-not-found 예외도 PowerPoint HWND 폴백을 허용한다."""
    class _MissingMember(Exception):
        hresult = -2147352573  # DISP_E_MEMBERNOTFOUND

    class _Ppt:
        HWND = 123

        @property
        def Hwnd(self):
            raise _MissingMember()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(office_guard, "_owned_pids", {})
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", 20)])
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20)
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 2020)
    assert office_guard.register_owned_app("POWERPNT.EXE", set(), _Ppt())
    assert office_guard._owned_pids[20].terminable is False


def test_dispatch_retries_ownership_on_same_app_and_baseline(monkeypatch):
    """지연 준비된 HWND는 재생성 없이 같은 COM 앱·원래 baseline으로 재검증한다."""
    app = object()
    dispatched = []
    probes = []
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    monkeypatch.setattr(office_guard, "office_pids_live", lambda exe: {10})
    monkeypatch.setattr(office_guard, "prepare_powerpoint_dispatch", lambda: ({10}, {}))
    monkeypatch.setattr(com_reader.time, "sleep", lambda seconds: None)

    def register(exe, baseline, candidate, expected_owned=None):
        probes.append((baseline, candidate))
        return len(probes) == 2

    monkeypatch.setattr(office_guard, "register_owned_app", register)
    client = SimpleNamespace(Dispatch=lambda prog_id: dispatched.append(prog_id) or app)
    assert com_reader._dispatch_and_own(client, "PowerPoint.Application", "POWERPNT.EXE") is app
    assert dispatched == ["PowerPoint.Application"]
    assert probes == [({10}, app), ({10}, app)]


def test_register_owned_app_rejects_existing_or_wrong_exe_pid(monkeypatch):
    """사용자 baseline PID나 다른 실행 파일 PID는 소유로 오인하지 않는다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    app = type("App", (), {"Hwnd": 1234})()

    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 10)])
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 10)
    assert office_guard.register_owned_app("EXCEL.EXE", {10}, app) is False

    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("WINWORD.EXE", 20)])
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20)
    assert office_guard.register_owned_app("EXCEL.EXE", set(), app) is False
    assert office_guard._owned_pids == {}


def test_register_owned_app_rejects_identity_change_during_probe(monkeypatch):
    """등록 확인 도중 PID identity가 바뀌면 사용자 프로세스로 보고 거부한다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    monkeypatch.setattr(
        office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 20)],
    )
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20)
    identities = iter((100, 200))
    monkeypatch.setattr(
        office_guard, "_process_creation_identity", lambda pid: next(identities),
    )
    app = type("App", (), {"Hwnd": 1234})()

    assert office_guard.register_owned_app("EXCEL.EXE", set(), app) is False
    assert office_guard._owned_pids == {}


def test_recover_poison_accepts_already_exited_owned_process(monkeypatch):
    """RPC 오류와 함께 프로세스가 먼저 죽었으면 종료 확인 성공으로 처리한다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    office_guard._owned_pids.update({
        10: office_guard.OwnedOfficeProcess("EXCEL.EXE", 1010),
        20: office_guard.OwnedOfficeProcess("WINWORD.EXE", 2020),
    })
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("WINWORD.EXE", 20)])
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)

    assert office_guard.recover_poisoned_office("EXCEL.EXE") is True
    assert office_guard._owned_pids == {20: office_guard.OwnedOfficeProcess("WINWORD.EXE", 2020)}


def test_fatal_close_overrides_general_read_error_for_recovery(monkeypatch):
    """일반 읽기 오류 뒤 Close의 RPC 단절은 원인을 체인으로 보존해 poison 처리한다."""
    class _FatalCloseError(Exception):
        hresult = -2147023174  # RPC_S_SERVER_UNAVAILABLE

    class _BrokenSheet:
        Name = "S"
        UsedRange = type("U", (), {"Row": 1, "Column": 1,
            "Rows": type("R", (), {"Count": 1})(), "Columns": type("C", (), {"Count": 1})()})()
        def Cells(self, row, col):
            return (row, col)
        def Range(self, *args):
            raise RuntimeError("file specific")

    class _Workbook:
        Sheets = [_BrokenSheet()]
        def Close(self, save):
            raise _FatalCloseError("rpc disconnected")

    app = _App("Workbooks")
    app.Workbooks = _OpenCollection(_Workbook())
    monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)
    monkeypatch.setattr(com_reader._tls, "excel", app, raising=False)

    with pytest.raises(com_reader.OfficeComPoisonError) as raised:
        com_reader.ExcelComReader().parse("C:/private/x.xls")
    assert raised.value.hresult == 0x800706BA
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert com_reader._tls.excel is None


def test_word_excel_use_dispatchex_and_ppt_uses_dispatch(monkeypatch):
    """Word/Excel은 독립 인스턴스, MultiUse PPT는 기존 Dispatch 정책을 쓴다."""
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    monkeypatch.setattr(office_guard, "office_pids_live", lambda exe: set())
    monkeypatch.setattr(office_guard, "register_owned_app", lambda exe, baseline, app, expected_owned=None: True)
    calls = []

    class _Client:
        def DispatchEx(self, prog_id):
            calls.append(("DispatchEx", prog_id))
            return object()
        def Dispatch(self, prog_id):
            calls.append(("Dispatch", prog_id))
            return object()

    client = _Client()
    com_reader._dispatch_and_own(client, "Word.Application", "WINWORD.EXE")
    com_reader._dispatch_and_own(client, "Excel.Application", "EXCEL.EXE")
    com_reader._dispatch_and_own(client, "PowerPoint.Application", "POWERPNT.EXE")
    assert calls == [
        ("DispatchEx", "Word.Application"),
        ("DispatchEx", "Excel.Application"),
        ("Dispatch", "PowerPoint.Application"),
    ]


def test_xls_dynamic_fallback_reports_actual_com_and_hooks(monkeypatch):
    """xlrd 실패 뒤에만 워치독용 훅을 감싸고 실제 COM 사용을 보고한다."""
    from knowmate.secure import AutoReader

    reader = AutoReader()
    monkeypatch.setattr(reader._plain, "extract", lambda path: (_ for _ in ()).throw(RuntimeError("xlrd")))
    calls = []

    class _Com:
        def __init__(self, **kwargs):
            pass
        def extract(self, path):
            return "com text"

    monkeypatch.setattr(com_reader, "ComReader", _Com)
    reader.set_com_operation_hooks(lambda exe: calls.append(("begin", exe)), lambda: calls.append(("end", None)))
    assert reader.extract("C:/private/fallback.xls") == "com text"
    assert calls == [("begin", "EXCEL.EXE"), ("end", None)]
    assert reader.take_actual_com_used() is True


def test_identity_mismatch_never_kills_reused_pid(monkeypatch):
    """같은 EXE 이름으로 PID가 재사용돼도 생성 identity가 다르면 종료하지 않는다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    office_guard._owned_pids[10] = office_guard.OwnedOfficeProcess("EXCEL.EXE", 100)
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 10)])
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 200)
    killed = []
    monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda *_args: "identity_changed")
    assert office_guard.terminate_stuck_office("EXCEL.EXE") == 0
    assert killed == []


def test_identity_probe_failure_defers_recovery_without_kill(monkeypatch):
    """살아있는 PID의 생성 identity를 못 읽으면 종료됨으로 오판하지 않는다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    record = office_guard.OwnedOfficeProcess("EXCEL.EXE", 100)
    office_guard._owned_pids[10] = record
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 10)])
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: None)
    killed = []
    monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda *_args: "identity_unknown")

    assert office_guard.recover_poisoned_office("EXCEL.EXE") is False
    assert killed == []
    assert office_guard._owned_pids[10].cleanup_pending is True


def test_powerpoint_multiuse_is_tracked_but_never_terminable(monkeypatch):
    """PPT는 파싱용 세션만 추적하고 어떤 강제 종료 경로에도 넣지 않는다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", 30)])
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 30)
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 3030)
    app = type("App", (), {"Hwnd": 3})()
    assert office_guard.register_owned_app("POWERPNT.EXE", set(), app) is True
    assert office_guard._owned_pids[30].terminable is False
    killed = []
    monkeypatch.setattr(office_guard, "_terminate_and_confirm", lambda pid, *_args: killed.append(pid) or "terminated")
    assert office_guard.terminate_stuck_office("POWERPNT.EXE") == 0
    office_guard.terminate_owned_office_processes(dict(office_guard._owned_pids))
    assert killed == []


def test_cycle_cleanup_releases_ppt_without_quit(monkeypatch, caplog):
    """PPT MultiUse 세션은 cycle end에도 Quit/강제종료 대신 참조만 해제한다."""
    class _Ppt:
        quit_called = False
        def Quit(self):
            self.quit_called = True

    ppt = _Ppt()
    caplog.set_level("INFO")
    monkeypatch.setattr(com_reader._tls, "ppt", ppt, raising=False)
    monkeypatch.setattr(office_guard, "_owned_pids", {
        30: office_guard.OwnedOfficeProcess("POWERPNT.EXE", 3030, False),
    })
    com_reader.quit_com_apps(grace_sec=0)
    assert ppt.quit_called is False
    assert getattr(com_reader._tls, "ppt", None) is None
    assert "quit_requested=False reason=multiuse_protection" in caplog.text


def test_ppt_poison_recovery_defers_same_cycle_without_kill(monkeypatch):
    """PPT MultiUse poison은 kill하지 못하므로 scheduler가 같은 앱을 연기하게 실패 반환한다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    office_guard._owned_pids[30] = office_guard.OwnedOfficeProcess("POWERPNT.EXE", 3030, False)
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", 30)])
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 3030)
    killed = []
    monkeypatch.setattr(
        office_guard, "_terminate_and_confirm",
        lambda pid, record, timeout: (killed.append(pid) or "terminated"),
    )
    assert office_guard.recover_poisoned_office("POWERPNT.EXE") is False
    assert killed == []
    assert office_guard._owned_pids == {30: office_guard.OwnedOfficeProcess("POWERPNT.EXE", 3030, False)}


def test_fatal_hwnd_probe_becomes_poison(monkeypatch):
    """HWND 확인 자체의 fatal RPC 오류는 Busy로 숨기지 않고 poison으로 전달한다."""
    class _Fatal(Exception):
        hresult = -2147023174

    class _App:
        @property
        def Hwnd(self):
            raise _Fatal("rpc")

    class _Client:
        def DispatchEx(self, prog_id):
            return _App()
        def Dispatch(self, prog_id):
            return _App()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    monkeypatch.setattr(office_guard, "office_pids_live", lambda exe: set())
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("EXCEL.EXE", 10)])
    with pytest.raises(com_reader.OfficeComPoisonError):
        com_reader._dispatch_and_own(_Client(), "Excel.Application", "EXCEL.EXE")


def test_ppt_shape_fatal_hresult_is_not_swallowed():
    """PPT 도형 분기에서 RPC 단절은 broad except를 뚫고 poison으로 전파한다."""
    class _Fatal(Exception):
        hresult = -2147023174

    class _Shape:
        @property
        def Type(self):
            raise _Fatal("rpc")

    with pytest.raises(com_reader.OfficeComPoisonError):
        com_reader._ppt_shape_texts(_Shape())


def test_dynamic_com_begin_failure_still_calls_end(monkeypatch):
    """watchdog arm 직후 실패해도 begin_com_op 컨텍스트 해제 훅은 실행된다."""
    from knowmate.secure import AutoReader

    reader = AutoReader()
    monkeypatch.setattr(reader._plain, "extract", lambda path: (_ for _ in ()).throw(RuntimeError("xlrd")))
    calls = []

    def _begin(exe):
        calls.append(("begin", exe))
        raise RuntimeError("arm failed")

    reader.set_com_operation_hooks(_begin, lambda: calls.append(("end", None)))
    with pytest.raises(RuntimeError, match="arm failed"):
        reader.extract("C:/private/fallback.xls")
    assert calls == [("begin", "EXCEL.EXE"), ("end", None)]
    assert reader.take_actual_com_used() is False


@pytest.mark.parametrize(("probe_fatal", "close_fatal"), [(True, False), (False, True)])
def test_word_probe_and_close_errors_preserve_fatal_rpc(monkeypatch, probe_fatal, close_fatal):
    """조회·닫기 오류가 겹쳐도 어느 쪽의 fatal RPC도 Busy로 숨기지 않는다."""
    class _Fatal(Exception):
        hresult = -2147023174

    fatal = _Fatal("rpc disconnected")
    added = []
    closed = []

    class _Window:
        @property
        def Hwnd(self):
            if probe_fatal:
                raise fatal
            return 123

    class _Document:
        Windows = SimpleNamespace(Item=lambda index: _Window())

        def Close(self, save):
            closed.append(save)
            raise fatal if close_fatal else RuntimeError("close failed")

    app = SimpleNamespace(Documents=SimpleNamespace(Add=lambda: added.append(1) or _Document()))
    client = SimpleNamespace(Dispatch=lambda prog_id: app)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(office_guard, "_owned_pids", {})
    monkeypatch.setattr(office_guard, "office_pids_live", lambda exe: set())
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("WINWORD.EXE", 20)])
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20)
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 2020)
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    with pytest.raises(com_reader.OfficeComPoisonError) as raised:
        com_reader._dispatch_and_own(client, "Word.Application", "WINWORD.EXE")
    assert raised.value.__cause__ is fatal
    assert added == [1]
    assert closed == ([] if probe_fatal else [False])
    if close_fatal:
        assert office_guard._owned_pids[20].cleanup_pending
    else:
        assert not office_guard._owned_pids


def test_word_close_failure_gates_next_dispatch_without_more_blank_documents(monkeypatch):
    """검증 뒤 닫기 실패는 해당 PID를 정리 대기로 남겨 문서·프로세스 누적을 막는다."""
    added = []
    dispatched = []
    closed = []

    def close(save):
        closed.append(save)
        raise RuntimeError("close failed")

    document = SimpleNamespace(
        Windows=SimpleNamespace(Item=lambda index: SimpleNamespace(Hwnd=123)), Close=close,
    )
    app = SimpleNamespace(Documents=SimpleNamespace(Add=lambda: added.append(1) or document))
    client = SimpleNamespace(Dispatch=lambda prog_id: dispatched.append(prog_id) or app)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(office_guard, "_owned_pids", {
        30: office_guard.OwnedOfficeProcess("WINWORD.EXE", 3030),
        40: office_guard.OwnedOfficeProcess("EXCEL.EXE", 4040),
    })
    monkeypatch.setattr(office_guard, "_cleanup_retry_at", {})
    monkeypatch.setattr(office_guard, "_cleanup_inflight", {})
    monkeypatch.setattr(office_guard, "office_pids_live", lambda exe: {30})
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("WINWORD.EXE", 20)])
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20)
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 2020)
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    snapshots = []

    def cleanup(owned, timeout_sec):
        snapshots.append(owned)
        return office_guard.OfficeCleanupResult(remaining=frozenset(owned))

    monkeypatch.setattr(office_guard, "cleanup_owned_processes", cleanup)
    for _ in range(2):
        with pytest.raises(office_guard.OfficeCleanupPendingError):
            com_reader._dispatch_and_own(client, "Word.Application", "WINWORD.EXE")
    assert added == [1]
    assert dispatched == ["Word.Application"]
    assert closed == [False]
    assert list(snapshots[0]) == [20]
    assert office_guard._owned_pids[20].cleanup_pending
    assert not office_guard._owned_pids[30].cleanup_pending
    assert not office_guard._owned_pids[40].cleanup_pending


@pytest.mark.parametrize("hresult", [
    0x800706BA, 0x800706BE, 0x800706BF, 0x80010006,
    0x80010007, 0x80010012, 0x80010108, 0x800401FD,
])
@pytest.mark.parametrize("probe_read", [1, 2])
@pytest.mark.parametrize("wrapped", [False, True])
def test_word_fatal_ownership_probe_skips_blocking_close(monkeypatch, hresult, probe_read, wrapped):
    """RPC 단절 뒤 멈추는 Close 없이 워커가 poison·문서 보존·정리 대기로 끝난다."""
    import threading

    fatal = RuntimeError(hresult - (1 << 32), "rpc disconnected")
    close_entered = threading.Event()
    release_close = threading.Event()
    outcome = {}
    reads = []
    added = []
    dispatched = []

    class _Window:
        @property
        def Hwnd(self):
            reads.append(1)
            if len(reads) == probe_read:
                if wrapped:
                    try:
                        raise fatal
                    except RuntimeError as exc:
                        raise office_guard.OfficeOwnershipProbeError("wrapped probe") from exc
                raise fatal
            return 123

    def close(save):
        close_entered.set()
        release_close.wait()

    document = SimpleNamespace(Windows=SimpleNamespace(Item=lambda index: _Window()), Close=close)
    app = SimpleNamespace(Documents=SimpleNamespace(Add=lambda: added.append(1) or document))
    client = SimpleNamespace(Dispatch=lambda prog_id: dispatched.append(prog_id) or app)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(office_guard, "_owned_pids", {})
    monkeypatch.setattr(office_guard, "_cleanup_inflight", {})
    monkeypatch.setattr(office_guard, "_cleanup_retry_at", {})
    monkeypatch.setattr(office_guard, "office_pids_live", lambda exe: set())
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("WINWORD.EXE", 20)])
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20)
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 2020)
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)

    def run():
        try:
            com_reader._dispatch_and_own(client, "Word.Application", "WINWORD.EXE")
        except Exception as exc:
            outcome["error"] = exc
            outcome["candidate"] = getattr(com_reader._tls, "ownership_cleanup", {}).get("WINWORD.EXE")
            try:
                com_reader._dispatch_and_own(client, "Word.Application", "WINWORD.EXE")
            except office_guard.OfficeCleanupPendingError:
                outcome["blocked"] = True
            outcome["cleanup"] = com_reader.quit_com_apps(grace_sec=0)
        finally:
            com_reader.release_unverified_com_refs()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        worker.join(timeout=1)
        assert not worker.is_alive(), "fatal probe must not wait for Close"
        assert not close_entered.is_set()
        error = outcome["error"]
        assert isinstance(error, com_reader.OfficeComPoisonError)
        assert error.hresult == hresult and error.__cause__ is fatal
        candidate = outcome["candidate"]
        assert candidate.app is app and candidate.document is document
        assert outcome["blocked"]
        assert added == [1] and dispatched == ["Word.Application"]
        assert len(reads) == probe_read
        assert not office_guard._owned_pids
        assert office_guard.unverified_cleanup_pending() == frozenset({"WINWORD.EXE"})
        assert not outcome["cleanup"].successful
        assert outcome["cleanup"].ownership_pending == frozenset({"WINWORD.EXE"})
    finally:
        release_close.set()
        worker.join(timeout=1)


@pytest.mark.parametrize("baseline", [set(), {20}])
def test_unregistered_word_close_failure_preserves_gate_without_new_com_calls(monkeypatch, baseline):
    """등록 전 이중 오류도 EXE 차단을 보존하고 종료 중 COM을 재조회하지 않는다."""
    from knowmate.secure import AutoReader

    class _Busy(Exception):
        hresult = -2147418111  # RPC_E_CALL_REJECTED

    reads = []
    state = {"busy": True}

    class _Window:
        @property
        def Hwnd(self):
            reads.append(1)
            if state["busy"]:
                raise _Busy()
            return 123

    added = []
    closed = []
    dispatched = []
    killed = []
    live = [("WINWORD.EXE", 20)]

    def close(save):
        closed.append(save)
        raise _Busy()

    document = SimpleNamespace(Windows=SimpleNamespace(Item=lambda index: _Window()), Close=close)
    app = SimpleNamespace(Documents=SimpleNamespace(Add=lambda: added.append(1) or document))
    client = SimpleNamespace(Dispatch=lambda prog_id: dispatched.append(prog_id) or app)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(office_guard, "_owned_pids", {})
    monkeypatch.setattr(office_guard, "_cleanup_inflight", {})
    monkeypatch.setattr(office_guard, "_cleanup_retry_at", {})
    monkeypatch.setattr(office_guard, "office_pids_live", lambda exe: baseline)
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: list(live))
    monkeypatch.setattr(office_guard, "_cached_processes", lambda: list(live))
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20)
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 2020)
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    monkeypatch.setattr(com_reader._tls, "word", None, raising=False)
    monkeypatch.setattr(com_reader._tls, "excel", None, raising=False)
    monkeypatch.setattr(com_reader._tls, "ppt", None, raising=False)

    def terminate(pid, record, timeout):
        killed.append(pid)
        live.clear()
        return "terminated"

    monkeypatch.setattr(office_guard, "_terminate_and_confirm", terminate)
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        com_reader._dispatch_and_own(client, "Word.Application", "WINWORD.EXE")
    candidate = com_reader._tls.ownership_cleanup["WINWORD.EXE"]
    assert candidate.app is app and candidate.document is document
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        AutoReader._guard_office_busy(".doc", "sample.doc")
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        com_reader._dispatch_and_own(client, "Word.Application", "WINWORD.EXE")
    assert added == [1] and closed == [False] and dispatched == ["Word.Application"]
    assert not office_guard._owned_pids

    state["busy"] = False
    before_cleanup_reads = len(reads)
    result = com_reader.quit_com_apps(grace_sec=0)
    assert added == [1] and closed == [False]
    assert len(reads) == before_cleanup_reads
    assert com_reader._tls.ownership_cleanup["WINWORD.EXE"] is candidate
    com_reader.release_unverified_com_refs()
    assert not com_reader._tls.ownership_cleanup
    assert not result.successful
    assert result.ownership_pending == frozenset({"WINWORD.EXE"})
    assert killed == []
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        office_guard.ensure_office_available("WINWORD.EXE")
    live.clear()
    office_guard.ensure_office_available("WINWORD.EXE")
    assert not office_guard._owned_pids
    assert not office_guard.unverified_cleanup_pending()


def test_unverified_gate_only_clears_after_successful_absence_probe(monkeypatch):
    """열거 실패나 다른 Office 프로세스는 미검증 gate 해제 근거가 아니다."""
    monkeypatch.setattr(office_guard, "_owned_pids", {})
    monkeypatch.setattr(office_guard, "_cleanup_inflight", {})
    office_guard.begin_unverified_cleanup("WINWORD.EXE")
    for procs in (None, [("WINWORD.EXE", 99)]):
        monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: procs)
        with pytest.raises(office_guard.OfficeCleanupPendingError):
            office_guard.ensure_office_available("WINWORD.EXE")
        office_guard.ensure_office_available("EXCEL.EXE")
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [])
    office_guard.ensure_office_available("WINWORD.EXE")
    assert not office_guard.unverified_cleanup_pending()


def test_unverified_cleanup_token_does_not_clear_other_request():
    """이전 워커 요청 해제가 같은 EXE의 새 정리 요청을 지우지 않는다."""
    first = office_guard.begin_unverified_cleanup("WINWORD.EXE")
    second = office_guard.begin_unverified_cleanup("WINWORD.EXE")
    office_guard.finish_unverified_cleanup("WINWORD.EXE", first)
    assert office_guard.unverified_cleanup_pending() == frozenset({"WINWORD.EXE"})
    office_guard.finish_unverified_cleanup("WINWORD.EXE", second)
    assert not office_guard.unverified_cleanup_pending()


def test_unverified_document_does_not_block_other_office_quit(monkeypatch):
    """미검증 문서 COM은 건드리지 않고 다른 Office 종료를 진행한다."""
    class _Document:
        @property
        def Windows(self):
            raise AssertionError("cleanup must not query COM")

    quit_calls = []
    monkeypatch.setattr(office_guard, "_owned_pids", {})
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("WINWORD.EXE", 20)])
    office_guard.begin_unverified_cleanup("WINWORD.EXE")
    com_reader._tls.ownership_cleanup["WINWORD.EXE"] = com_reader._PendingOwnershipCleanup(
        object(), _Document(),
    )
    monkeypatch.setattr(com_reader._tls, "word", None, raising=False)
    monkeypatch.setattr(com_reader._tls, "ppt", None, raising=False)
    monkeypatch.setattr(com_reader._tls, "excel", SimpleNamespace(Quit=lambda: quit_calls.append("excel")), raising=False)
    result = com_reader.quit_com_apps(grace_sec=0)
    assert quit_calls == ["excel"]
    assert result.ownership_pending == frozenset({"WINWORD.EXE"})
    assert not result.successful
    com_reader.release_unverified_com_refs()
    assert not com_reader._tls.ownership_cleanup


def test_unverified_absence_snapshot_preserves_new_token(monkeypatch):
    """부재 확인과 요청 해제 사이 추가된 정리 토큰은 보존한다."""
    office_guard.begin_unverified_cleanup("WINWORD.EXE")

    def absent(exe):
        office_guard.begin_unverified_cleanup(exe)
        return True

    monkeypatch.setattr(office_guard, "office_processes_absent", absent)
    office_guard.prune_unverified_cleanup()
    assert office_guard.unverified_cleanup_pending() == frozenset({"WINWORD.EXE"})


@pytest.mark.parametrize(("exe", "prog_id", "ext"), [
    ("WINWORD.EXE", "Word.Application", ".doc"),
    ("EXCEL.EXE", "Excel.Application", ".xls"),
    ("POWERPNT.EXE", "PowerPoint.Application", ".ppt"),
])
@pytest.mark.parametrize("failure", ["false", "probe", "fatal", "unexpected"])
def test_all_registration_failures_retain_app_and_gate(monkeypatch, exe, prog_id, ext, failure):
    """일반·fatal·예상 밖 등록 실패 모두 앱 보존과 정확한 정리 대기 결과로 끝난다."""
    from knowmate.secure import AutoReader

    class _Fatal(Exception):
        hresult = -2147023174

    app = object()
    dispatched = []
    attempts = []

    def register(exe, baseline, candidate, expected_owned=None):
        attempts.append(candidate)
        if failure == "false":
            return False
        if failure == "unexpected":
            raise ValueError("unexpected probe failure")
        original = _Fatal() if failure == "fatal" else RuntimeError("probe rejected")
        raise office_guard.OfficeOwnershipProbeError("probe") from original

    client = SimpleNamespace(Dispatch=lambda prog_id: dispatched.append(prog_id) or app)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(office_guard, "_owned_pids", {})
    monkeypatch.setattr(office_guard, "_cleanup_inflight", {})
    monkeypatch.setattr(office_guard, "_cleanup_retry_at", {})
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [(exe, 20)])
    monkeypatch.setattr(office_guard, "_cached_processes", lambda: [(exe, 20)])
    monkeypatch.setattr(office_guard, "office_pids_live", lambda exe: set())
    monkeypatch.setattr(office_guard, "register_owned_app", register)
    monkeypatch.setattr(office_guard, "prepare_powerpoint_dispatch", lambda: (set(), {}))
    monkeypatch.setattr(com_reader.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    for attr in ("word", "excel", "ppt"):
        monkeypatch.setattr(com_reader._tls, attr, None, raising=False)
    expected = (com_reader.OfficeComPoisonError if failure == "fatal" else
                ValueError if failure == "unexpected" else office_guard.OfficeCleanupPendingError)
    with pytest.raises(expected):
        com_reader._dispatch_and_own(client, prog_id, exe)
    assert com_reader._tls.ownership_cleanup[exe].app is app
    assert len(attempts) == (1 if failure in {"fatal", "unexpected"} else 3)
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        AutoReader._guard_office_busy(ext, "sample" + ext)
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        com_reader._dispatch_and_own(client, prog_id, exe)
    assert dispatched == [prog_id]
    assert not office_guard._owned_pids
    result = com_reader.quit_com_apps(grace_sec=0)
    assert not result.successful
    assert result.ownership_pending == frozenset({exe})
    com_reader.release_unverified_com_refs()
    assert not com_reader._tls.ownership_cleanup
    assert office_guard.unverified_cleanup_pending() == frozenset({exe})


def test_ppt_hwnd_not_ready_exhaustion_is_pending_not_external_busy(monkeypatch):
    """실제 등록 경로의 HWND=0 재시도 소진도 PPT 앱을 보존하고 새 생성을 차단한다."""
    from knowmate.secure import AutoReader
    ready = {"value": False}
    reads = []
    dispatched = []

    class _Ppt:
        @property
        def Hwnd(self):
            reads.append(1)
            return 123 if ready["value"] else 0

        def Quit(self):
            raise AssertionError("unverified PPT must not be quit")

    app = _Ppt()
    client = SimpleNamespace(Dispatch=lambda prog_id: dispatched.append(prog_id) or app)
    live = [("POWERPNT.EXE", 20)]
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(office_guard, "_owned_pids", {})
    monkeypatch.setattr(office_guard, "_cleanup_inflight", {})
    monkeypatch.setattr(office_guard, "_cleanup_retry_at", {})
    monkeypatch.setattr(office_guard, "office_pids_live", lambda exe: set())
    monkeypatch.setattr(office_guard, "prepare_powerpoint_dispatch", lambda: (set(), {}))
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: list(live))
    monkeypatch.setattr(office_guard, "_cached_processes", lambda: list(live))
    monkeypatch.setattr(office_guard, "_pid_from_hwnd", lambda hwnd: 20 if hwnd else None)
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 2020)
    monkeypatch.setattr(com_reader.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("knowmate.secure.office_resiliency.clear_resiliency_markers", lambda exe: None)
    for attr in ("word", "excel", "ppt"):
        monkeypatch.setattr(com_reader._tls, attr, None, raising=False)
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        com_reader._dispatch_and_own(client, "PowerPoint.Application", "POWERPNT.EXE")
    assert com_reader._tls.ownership_cleanup["POWERPNT.EXE"].app is app
    ready["value"] = True
    before_cleanup_reads = len(reads)
    with pytest.raises(office_guard.OfficeCleanupPendingError):
        AutoReader._guard_office_busy(".ppt", "next.ppt")
    result = com_reader.quit_com_apps(grace_sec=0)
    assert not result.successful
    assert result.ownership_pending == frozenset({"POWERPNT.EXE"})
    assert len(reads) == before_cleanup_reads
    assert dispatched == ["PowerPoint.Application"]
    assert not office_guard._owned_pids
    com_reader.release_unverified_com_refs()
    live.clear()
    AutoReader._guard_office_busy(".ppt", "next.ppt")
    assert not office_guard.unverified_cleanup_pending()
