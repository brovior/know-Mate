"""Office RPC poison 격리의 Linux fake 회귀 테스트."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from knowmate.collector import failure_state
from knowmate.secure import com_reader, office_guard


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
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False,
    )

    assert office_guard._enumerate_processes() == []


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
        office_guard, "_terminate_pid",
        lambda pid, expected=None: (killed.append(pid) or True),
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
    monkeypatch.setattr(office_guard, "register_owned_app", lambda exe, baseline, app: True)
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
    monkeypatch.setattr(
        office_guard, "_terminate_pid",
        lambda pid, expected=None: (killed.append(pid) or True),
    )
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
    monkeypatch.setattr(
        office_guard, "_terminate_pid",
        lambda pid, expected=None: (killed.append(pid) or True),
    )

    assert office_guard.recover_poisoned_office("EXCEL.EXE") is False
    assert killed == []
    assert office_guard._owned_pids == {10: record}


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
    monkeypatch.setattr(
        office_guard, "_terminate_pid",
        lambda pid, expected=None: (killed.append(pid) or True),
    )
    assert office_guard.terminate_stuck_office("POWERPNT.EXE") == 0
    office_guard.terminate_owned_office_processes(dict(office_guard._owned_pids))
    assert killed == []


def test_cycle_cleanup_releases_ppt_without_quit(monkeypatch):
    """PPT MultiUse 세션은 cycle end에도 Quit/강제종료 대신 참조만 해제한다."""
    class _Ppt:
        quit_called = False
        def Quit(self):
            self.quit_called = True

    ppt = _Ppt()
    monkeypatch.setattr(com_reader._tls, "ppt", ppt, raising=False)
    monkeypatch.setattr(
        office_guard,
        "take_owned_processes",
        lambda: {30: office_guard.OwnedOfficeProcess("POWERPNT.EXE", 3030, False)},
    )
    com_reader.quit_com_apps(grace_sec=0)
    assert ppt.quit_called is False
    assert getattr(com_reader._tls, "ppt", None) is None


def test_ppt_poison_recovery_defers_same_cycle_without_kill(monkeypatch):
    """PPT MultiUse poison은 kill하지 못하므로 scheduler가 같은 앱을 연기하게 실패 반환한다."""
    monkeypatch.setattr(sys, "platform", "win32")
    office_guard.clear_owned_pids()
    office_guard._owned_pids[30] = office_guard.OwnedOfficeProcess("POWERPNT.EXE", 3030, False)
    monkeypatch.setattr(office_guard, "_enumerate_processes", lambda: [("POWERPNT.EXE", 30)])
    monkeypatch.setattr(office_guard, "_process_creation_identity", lambda pid: 3030)
    killed = []
    monkeypatch.setattr(
        office_guard, "_terminate_pid",
        lambda pid, expected=None: (killed.append(pid) or True),
    )
    assert office_guard.recover_poisoned_office("POWERPNT.EXE") is False
    assert killed == []
    assert office_guard._owned_pids == {}


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
