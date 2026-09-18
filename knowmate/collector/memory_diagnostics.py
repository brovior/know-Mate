"""수집 사이클의 Python·Arrow·프로세스 메모리 진단 계측."""
from __future__ import annotations

import ctypes
import gc
import logging
import os
from pathlib import Path
import tracemalloc
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024


class _ProcessMemoryCountersEx(ctypes.Structure):
    """Windows PROCESS_MEMORY_COUNTERS_EX 구조체."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def get_process_private_bytes() -> int | None:
    """현재 Windows 프로세스의 Private Bytes를 반환한다."""
    if os.name != "nt":
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCountersEx),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    counters = _ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        return None
    return int(counters.PrivateUsage)


def _default_arrow_pool() -> Any:
    """PyArrow 기본 메모리 풀을 지연 로드해 반환한다."""
    import pyarrow as pa

    return pa.default_memory_pool()


def _format_mib(value: int | None) -> str:
    """바이트 값을 로그용 MiB 문자열로 바꾼다."""
    return "n/a" if value is None else f"{value / _MIB:.1f}"


class MemoryDiagnostics:
    """설정으로 활성화되는 수집 사이클 메모리 계측기."""

    def __init__(
        self,
        enabled: bool,
        *,
        private_bytes_reader: Callable[[], int | None] = get_process_private_bytes,
        arrow_pool_getter: Callable[[], Any] = _default_arrow_pool,
    ) -> None:
        self.enabled = enabled
        self._private_bytes_reader = private_bytes_reader
        self._arrow_pool_getter = arrow_pool_getter
        self._owns_tracing = False
        self._started = False
        self._cycle_snapshot = None
        self._before_optimize_snapshot = None

    def start(self) -> None:
        """필요할 때만 tracemalloc 추적을 시작하고 사이클 peak를 초기화한다."""
        if not self.enabled or self._started:
            return
        try:
            if not tracemalloc.is_tracing():
                tracemalloc.start(10)
                self._owns_tracing = True
            else:
                logger.info(
                    "[memory] external tracemalloc tracing detected; preserving its depth and lifetime"
                )
            if self._owns_tracing:
                tracemalloc.reset_peak()
            self._cycle_snapshot = tracemalloc.take_snapshot() if tracemalloc.is_tracing() else None
        except Exception:
            logger.debug("[memory] tracemalloc 시작 실패", exc_info=True)
        self._started = True

    def log(self, phase: str) -> None:
        """한 시점의 Private/Python/Arrow 메모리를 한 줄 INFO로 기록한다."""
        if not self.enabled:
            return

        private_bytes: int | None = None
        python_current: int | None = None
        python_peak: int | None = None
        arrow_current: int | None = None
        arrow_peak: int | None = None
        arrow_backend = "n/a"

        try:
            private_bytes = self._private_bytes_reader()
        except Exception:
            logger.debug("[memory] Windows Private Bytes 조회 실패", exc_info=True)
        try:
            if tracemalloc.is_tracing():
                python_current, python_peak = tracemalloc.get_traced_memory()
        except Exception:
            logger.debug("[memory] tracemalloc 조회 실패", exc_info=True)
        try:
            pool = self._arrow_pool_getter()
            arrow_current = int(pool.bytes_allocated())
            arrow_peak_value = pool.max_memory()
            arrow_peak = None if arrow_peak_value is None else int(arrow_peak_value)
            arrow_backend = str(pool.backend_name)
        except Exception:
            logger.debug("[memory] PyArrow 메모리 풀 조회 실패", exc_info=True)

        logger.info(
            "[memory] phase=%s private_mib=%s python_current_mib=%s "
            "python_peak_mib=%s arrow_current_mib=%s arrow_peak_mib=%s arrow_backend=%s",
            phase,
            _format_mib(private_bytes),
            _format_mib(python_current),
            _format_mib(python_peak),
            _format_mib(arrow_current),
            _format_mib(arrow_peak),
            arrow_backend,
        )
        self._log_snapshot_diff(phase)

    @staticmethod
    def _display_trace_filename(filename: str) -> str:
        """Keep diagnostic locations useful without exposing user/AppData paths."""
        normalized = filename.replace("\\", "/")
        marker = "/knowmate/"
        if marker in normalized:
            return "knowmate/" + normalized.split(marker, 1)[1]
        for package in ("/site-packages/", "/lib/python"):
            if package in normalized:
                return normalized.split(package, 1)[1]
        return Path(normalized).name

    def _log_snapshot_diff(self, phase: str) -> None:
        """Log top code-location deltas only; snapshots never leave process memory."""
        if not self.enabled or not tracemalloc.is_tracing():
            return
        try:
            snapshot = tracemalloc.take_snapshot()
            if phase.startswith("before_optimize_"):
                self._before_optimize_snapshot = snapshot
                return
            baseline = self._before_optimize_snapshot if phase.startswith("after_optimize_") else self._cycle_snapshot
            if baseline is None or not (phase.startswith("after_optimize_") or phase in {"after_mail", "after_gc_collect"}):
                return
            stats = [
                stat for stat in snapshot.compare_to(baseline, "lineno")
                if stat.size_diff > 0
            ][:5]
            items = [
                f"{self._display_trace_filename(stat.traceback[0].filename)}:{stat.traceback[0].lineno} "
                f"size_diff={stat.size_diff} count_diff={stat.count_diff}"
                for stat in stats if stat.traceback
            ]
            if items:
                logger.info("[memory] snapshot_diff phase=%s top=%s", phase, " | ".join(items))
        except Exception:
            logger.debug("[memory] tracemalloc snapshot diff failed", exc_info=True)

    def collect_and_log(self) -> None:
        """진단 모드에서만 Python GC를 실행한 뒤 마지막 메모리를 기록한다."""
        if not self.enabled:
            return
        try:
            gc.collect()
        except Exception:
            logger.debug("[memory] gc.collect 실패", exc_info=True)
        self.log("after_gc_collect")

    def stop(self) -> None:
        """이 계측기가 시작한 tracemalloc 추적만 종료한다."""
        try:
            if self._owns_tracing and tracemalloc.is_tracing():
                tracemalloc.stop()
        except Exception:
            logger.debug("[memory] tracemalloc 종료 실패", exc_info=True)
        self._owns_tracing = False
        self._started = False
        self._cycle_snapshot = None
        self._before_optimize_snapshot = None
