"""Thread-owned native COM initialization, independent of pywin32 wrappers."""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Any

from knowmate.secure.office_guard import OfficeBusyError

logger = logging.getLogger(__name__)
_tls = threading.local()


def _record(message: str, *args: Any) -> None:
    try:
        logger.info(message, *args)
    except Exception:
        pass  # Optional diagnostics must never lose ownership of a native success.


class ComScopeUnavailable(OfficeBusyError):
    """Defer COM without treating an unavailable apartment as a corrupt file."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in {
            "com_native_unavailable", "com_scope_already_entered", "com_scope_wrong_thread",
            "com_initialize_failed", "com_mta_unconfirmed", "com_scope_close_order",
            "com_scope_required", "com_scope_not_acquired",
        } else "com_dependency_unavailable"
        super().__init__(reason)


class _NativeCom:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ComScopeUnavailable("com_native_unavailable")
        self._ole = ctypes.WinDLL("ole32", use_last_error=True)
        self._ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._ole.CoInitializeEx.restype = ctypes.c_int32
        self._ole.CoGetApartmentType.argtypes = [ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int32)]
        self._ole.CoGetApartmentType.restype = ctypes.c_int32
        self._ole.CoUninitialize.argtypes = []
        self._ole.CoUninitialize.restype = None

    def initialize(self) -> int:
        return int(self._ole.CoInitializeEx(None, 0))

    def apartment(self) -> tuple[int, int]:
        apartment, qualifier = ctypes.c_int32(), ctypes.c_int32()
        result = int(self._ole.CoGetApartmentType(ctypes.byref(apartment), ctypes.byref(qualifier)))
        return result, apartment.value

    def uninitialize(self) -> None:
        self._ole.CoUninitialize()


class ComScope:
    """Register a lazy MTA root; nested scopes borrow its single native pair."""

    def __init__(self, backend: Any = None) -> None:
        self._backend = backend
        self._owner: int | None = None
        self._parent: ComScope | None = None
        self._root = self
        self._entered = False
        self._initialized = False
        self._verified = False

    def __enter__(self) -> ComScope:
        if self._entered:
            raise ComScopeUnavailable("com_scope_already_entered")
        self._owner = threading.get_ident()
        self._parent = getattr(_tls, "scope", None)
        self._root = self._parent._root if self._parent is not None else self
        self._entered = True
        _tls.scope = self
        return self

    @property
    def active(self) -> bool:
        """Whether this root still owns a verified native initialization."""
        return self._entered and self._root._initialized and self._root._verified

    def _require_thread(self) -> None:
        if not self._entered or self._owner != threading.get_ident():
            raise ComScopeUnavailable("com_scope_wrong_thread")

    def acquire(self) -> ComScope:
        """Initialize once before importing pywin32, then verify actual MTA mode."""
        self._require_thread()
        root = self._root
        if root._initialized:
            if not root._verified:
                raise ComScopeUnavailable("com_mta_unconfirmed")
            return self
        backend = root._backend
        if backend is None:
            try:
                backend = root._backend = _NativeCom()
            except ComScopeUnavailable:
                raise
            except (OSError, AttributeError):
                raise ComScopeUnavailable("com_native_unavailable") from None
        try:
            result = backend.initialize()
        except Exception as exc:
            _record("[com-scope] initialize_failed: thread=%s error_type=%s", self._owner, type(exc).__name__)
            raise ComScopeUnavailable("com_initialize_failed") from None
        if type(result) is not int or result not in (0, 1):
            _record("[com-scope] initialize: thread=%s hresult=%s", self._owner, result)
            raise ComScopeUnavailable("com_initialize_failed")
        root._initialized = True
        try:
            _record("[com-scope] initialize: thread=%s hresult=%s pythoncom_preimported=%s",
                    self._owner, result, "pythoncom" in sys.modules)
            status, apartment = backend.apartment()
            _record("[com-scope] apartment: thread=%s hresult=%s apartment=%s",
                    self._owner, status, apartment)
            if status != 0 or apartment != 1:
                raise ComScopeUnavailable("com_mta_unconfirmed")
            root._verified = True
        except BaseException as exc:
            root._initialized = False
            root._verified = False
            backend.uninitialize()
            if isinstance(exc, Exception) and not isinstance(exc, ComScopeUnavailable):
                _record("[com-scope] apartment_failed: thread=%s error_type=%s", self._owner, type(exc).__name__)
                raise ComScopeUnavailable("com_mta_unconfirmed") from None
            raise
        return self

    def close(self) -> None:
        """Release only this root's successful native call on its owning thread."""
        if not self._entered and self._owner in (None, threading.get_ident()):
            return
        self._require_thread()
        if getattr(_tls, "scope", None) is not self:
            raise ComScopeUnavailable("com_scope_close_order")
        try:
            if self._root is self and self._initialized:
                self._initialized = False
                self._verified = False
                self._backend.uninitialize()
                _record("[com-scope] own_pair_complete: thread=%s", self._owner)
        finally:
            _tls.scope = self._parent
            self._entered = False

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def acquire_current_scope() -> ComScope:
    """Acquire the caller-owned current scope before any new COM import."""
    scope = getattr(_tls, "scope", None)
    if scope is None:
        raise ComScopeUnavailable("com_scope_required")
    return scope.acquire()


def require_current_scope() -> ComScope:
    """Require an active same-thread MTA scope without initializing COM again."""
    scope = getattr(_tls, "scope", None)
    if scope is None:
        raise ComScopeUnavailable("com_scope_required")
    scope._require_thread()
    if not scope.active:
        raise ComScopeUnavailable("com_scope_not_acquired")
    return scope
