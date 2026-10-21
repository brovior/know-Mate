"""COM-free observation of one retained original process handle."""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
_POLL_SEC = 0.05


@dataclass(frozen=True)
class ProcessObservation:
    """OS evidence only; a signaled handle does not establish shutdown cause."""

    status: str
    error_code: int | None = None


class RetainedProcess:
    """Own an observation handle and serialize every zero-time wait with close."""

    def __init__(self, pid: int, identity: int, handle: Any, kernel: Any) -> None:
        self.pid = pid
        self.identity = identity
        self._handle = handle
        self._kernel = kernel
        self._lock = threading.Lock()
        self._result = ProcessObservation("timeout")
        from ctypes import wintypes
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD

    def poll(self) -> ProcessObservation:
        """Inspect only the original handle without reopening a possibly reused PID."""
        with self._lock:
            if self._handle is None:
                return self._result
            try:
                status = int(self._kernel.WaitForSingleObject(self._handle, 0))
                error = ctypes.get_last_error() if status == 0xFFFFFFFF else None
                if status == 0:
                    self._result = ProcessObservation("exited")
                elif status == 258:
                    self._result = ProcessObservation("timeout")
                    return self._result
                else:
                    self._result = ProcessObservation("observation_failed", error)
            except Exception as exc:
                code = getattr(exc, "winerror", None)
                self._result = ProcessObservation("observation_failed", code if type(code) is int else None)
            self._close_locked()
            return self._result

    def wait_until(self, deadline: float) -> ProcessObservation:
        """Poll under one shared monotonic deadline without holding locks while sleeping."""
        while True:
            result = self.poll()
            left = deadline - time.monotonic()
            if result.status != "timeout" or left <= 0:
                return result
            time.sleep(min(_POLL_SEC, left))

    def close(self) -> None:
        """Release observation exactly once, after any current native poll returns."""
        with self._lock:
            if self._result.status == "timeout":
                self._result = ProcessObservation("target_unconfirmed")
            self._close_locked()

    def _close_locked(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if not self._kernel.CloseHandle(handle):
                error = ctypes.get_last_error()
                logger.warning("[ppt] observation close failed: pid=%s error_code=%s", self.pid, error)
        except Exception as exc:
            logger.warning("[ppt] observation close failed: pid=%s error_type=%s", self.pid, type(exc).__name__)
