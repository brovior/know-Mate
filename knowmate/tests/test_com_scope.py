"""Native ownership, import order and finished error-frame lifetime contracts."""
from __future__ import annotations

import sys
import threading
import weakref
from types import SimpleNamespace

import pytest

from knowmate.secure import com_scope


class NativeModel:
    """Record native-equivalent calls without pretending to exercise Windows."""

    def __init__(self, result: int = 0, apartment: tuple[int, int] = (0, 1)) -> None:
        self.result, self.mode = result, apartment
        self.events: list[str] = []

    def initialize(self) -> int:
        self.events.append("initialize")
        return self.result

    def apartment(self) -> tuple[int, int]:
        self.events.append("apartment")
        return self.mode

    def uninitialize(self) -> None:
        self.events.append("uninitialize")


def test_root_is_lazy_and_require_does_not_initialize():
    native = NativeModel()
    with com_scope.ComScope(native):
        with pytest.raises(com_scope.ComScopeUnavailable, match="not_acquired"):
            com_scope.require_current_scope()
    assert native.events == []
    with pytest.raises(com_scope.ComScopeUnavailable, match="required"):
        com_scope.require_current_scope()


@pytest.mark.parametrize("success", [0, 1])
def test_each_native_success_gets_one_same_thread_pair_and_nested_borrow(success):
    native = NativeModel(success)
    with com_scope.ComScope(native) as root:
        root.acquire()
        with com_scope.ComScope() as nested:
            assert com_scope.acquire_current_scope() is nested
            assert com_scope.require_current_scope() is nested
            nested.acquire()
        assert root.active
    assert native.events == ["initialize", "apartment", "uninitialize"]
    assert not root.active
    root.close()
    assert native.events.count("uninitialize") == 1


def test_failed_native_initialization_never_uninitializes():
    native = NativeModel(-2147417850)
    with com_scope.ComScope(native) as scope:
        with pytest.raises(com_scope.ComScopeUnavailable, match="initialize_failed"):
            scope.acquire()
    assert native.events == ["initialize"]


@pytest.mark.parametrize("mode", [(0, 0), (-1, 1), (0, 2)])
def test_unknown_or_wrong_mode_unwinds_only_own_success(mode):
    native = NativeModel(apartment=mode)
    with com_scope.ComScope(native) as scope:
        with pytest.raises(com_scope.ComScopeUnavailable, match="mta_unconfirmed"):
            scope.acquire()
        assert not scope.active
    assert native.events == ["initialize", "apartment", "uninitialize"]


def test_foreign_thread_cannot_acquire_or_uninitialize_owner_scope():
    native = NativeModel()
    errors = []
    with com_scope.ComScope(native) as scope:
        scope.acquire()

        def foreign():
            for operation in (scope.acquire, scope.close):
                try:
                    operation()
                except com_scope.ComScopeUnavailable as error:
                    errors.append(str(error))

        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        assert scope.active
        assert native.events == ["initialize", "apartment"]
    assert errors == ["com_scope_wrong_thread", "com_scope_wrong_thread"]
    assert native.events[-1] == "uninitialize"


def test_root_cannot_close_before_nested_scope():
    native = NativeModel()
    with com_scope.ComScope(native) as root:
        root.acquire()
        with com_scope.ComScope():
            with pytest.raises(com_scope.ComScopeUnavailable, match="close_order"):
                root.close()
        assert root.active
    assert native.events.count("uninitialize") == 1


def test_logging_failure_does_not_lose_native_ownership(monkeypatch):
    native = NativeModel()
    monkeypatch.setattr(com_scope.logger, "info", lambda *_args: (_ for _ in ()).throw(OSError("log")))
    with com_scope.ComScope(native) as scope:
        scope.acquire()
        assert scope.active
    assert native.events == ["initialize", "apartment", "uninitialize"]


def test_logging_interrupt_balances_the_already_successful_native_call(monkeypatch):
    native = NativeModel()
    monkeypatch.setattr(com_scope.logger, "info", lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with com_scope.ComScope(native) as scope:
        with pytest.raises(KeyboardInterrupt):
            scope.acquire()
        assert not scope.active
    assert native.events == ["initialize", "uninitialize"]


def test_reentrant_logging_cannot_admit_com_before_apartment_verification(monkeypatch):
    native = NativeModel()
    attempts = []
    def inspect_scope(*_args):
        try:
            com_scope.require_current_scope()
        except com_scope.ComScopeUnavailable:
            attempts.append("blocked")
        else:
            attempts.append("admitted")
    monkeypatch.setattr(com_scope.logger, "info", inspect_scope)
    with com_scope.ComScope(native) as scope:
        scope.acquire()
        assert scope.active
    assert attempts[:2] == ["blocked", "blocked"]
    assert native.events == ["initialize", "apartment", "uninitialize"]


def test_apartment_query_exception_unwinds_success_once():
    native = NativeModel()
    native.apartment = lambda: (_ for _ in ()).throw(OSError("query"))
    with com_scope.ComScope(native) as scope:
        with pytest.raises(com_scope.ComScopeUnavailable, match="mta_unconfirmed"):
            scope.acquire()
    assert native.events == ["initialize", "uninitialize"]


def test_native_initialize_api_exception_is_an_environment_deferral():
    native = NativeModel()
    native.initialize = lambda: (_ for _ in ()).throw(OSError("native call unavailable"))
    with com_scope.ComScope(native) as scope:
        with pytest.raises(com_scope.ComScopeUnavailable, match="initialize_failed"):
            scope.acquire()
    assert native.events == []


def test_native_abi_uses_explicit_32_bit_hresult_and_mta(monkeypatch):
    import ctypes
    calls = []

    class Function:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args):
            return self.fn(*args)

    def apartment(kind, qualifier):
        kind._obj.value = 1
        qualifier._obj.value = 0
        return 0

    ole = SimpleNamespace(CoInitializeEx=Function(lambda reserved, flags: calls.append((reserved, flags)) or 1),
                          CoGetApartmentType=Function(apartment), CoUninitialize=Function(lambda: calls.append("uninit")))
    with monkeypatch.context() as patch:
        patch.setattr(com_scope.sys, "platform", "win32")
        patch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: ole, raising=False)
        native = com_scope._NativeCom()
        assert native.initialize() == 1
        assert native.apartment() == (0, 1)
        native.uninitialize()
    assert calls == [(None, 0), "uninit"]
    assert ole.CoInitializeEx.restype is ctypes.c_int32
    assert ole.CoGetApartmentType.restype is ctypes.c_int32
    assert ole.CoUninitialize.restype is None


def test_auto_reader_acquires_before_com_reader_construction(monkeypatch):
    from knowmate.secure import AutoReader, com_reader
    native = NativeModel()
    reader = AutoReader()
    monkeypatch.setattr(reader, "_guard_office_busy", lambda *_args: None)

    class Reader:
        def __init__(self, **_kwargs):
            assert com_scope.require_current_scope().active
            native.events.append("reader")

        def extract(self, path):
            return "synthetic"

    monkeypatch.setattr(com_reader, "ComReader", Reader)
    with com_scope.ComScope(native):
        assert reader.extract("fixture.doc") == "synthetic"
    assert native.events == ["initialize", "apartment", "reader", "uninitialize"]


def test_direct_com_reader_acquires_lazy_worker_scope(monkeypatch):
    from knowmate.secure import com_reader
    native = NativeModel()
    reader = com_reader.ComReader()

    def parse(path):
        assert com_scope.require_current_scope().active
        return "synthetic"

    monkeypatch.setattr(reader._word, "parse", parse)
    with com_scope.ComScope(native):
        assert reader.extract("fixture.doc") == "synthetic"
    assert native.events == ["initialize", "apartment", "uninitialize"]


def test_cached_com_app_cannot_bypass_scope(monkeypatch):
    from knowmate.secure import com_reader, office_guard, ppt_shutdown
    monkeypatch.setattr(office_guard, "ensure_application_open", lambda _exe: None)
    monkeypatch.setattr(ppt_shutdown, "ensure_admitted", lambda: None)
    monkeypatch.setattr(com_reader._tls, "ppt", object(), raising=False)
    with pytest.raises(com_scope.ComScopeUnavailable, match="scope_required"):
        com_reader._get_ppt_app()


def test_classified_error_frames_release_parser_refs_while_handler_is_running():
    from knowmate.secure.com_fault import make_fault, ComFailureEvidence, clear_com_error_frames
    from knowmate.secure.com_reader import OfficeComPoisonError
    refs = []

    class Application:
        pass

    def parse():
        app = Application()
        refs.append(weakref.ref(app))
        try:
            raise RuntimeError("synthetic error")
        except RuntimeError as error:
            fault = make_fault(error, "read", app_exe="POWERPNT.EXE")
            raise OfficeComPoisonError("POWERPNT.EXE", 0x80010108,
                                      evidence=ComFailureEvidence(fault, (), fault, 0x80010108)) from error

    try:
        parse()
    except OfficeComPoisonError as error:
        assert refs[0]() is not None
        clear_com_error_frames(error)
        assert refs[0]() is None
        assert error.hresult == 0x80010108
        assert error.evidence.primary.stage == "read"
        assert error.evidence.primary.original.__traceback__ is None
        # The executing handler survives, even though finished parser frames do not.
        assert sys.exception() is error


def test_classified_ownership_error_drops_document_and_nested_error_frames():
    from knowmate.secure.com_fault import clear_com_error_frames
    from knowmate.secure.office_guard import OfficeOwnershipCleanupError

    class Document:
        pass

    document = Document()
    reference = weakref.ref(document)
    close = RuntimeError("synthetic close failure")
    error = OfficeOwnershipCleanupError(None, close, document)
    del document
    clear_com_error_frames(error)
    assert reference() is None
    assert error.document is None and error.close_error is None


def test_fake_worker_never_loads_native_com(monkeypatch, tmp_path):
    from knowmate.collector.scheduler import CollectorWorker
    from knowmate.secure import com_reader
    monkeypatch.setattr(com_scope, "_NativeCom", lambda: pytest.fail("fake cycle loaded COM"))
    worker = CollectorWorker({"collector": {}}, SimpleNamespace(), None,
                             state_file=tmp_path / "state.json", com_cleanup_fn=lambda **_kwargs: None)
    monkeypatch.setattr(worker, "_run_cycle", lambda: None)
    worker.run()

