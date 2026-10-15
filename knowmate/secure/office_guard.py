"""사용자가 직접 열어둔 Office 인스턴스 감지 (COM 점유 충돌 예방).

COM 자동화는 대상 Office 프로세스가 이미 떠 있으면 그 인스턴스에 붙는다.
Office는 사용자당 하나의 인스턴스만 실행되는 구조라, 백그라운드 인덱싱이
`Dispatch("Word.Application")`을 호출하면 사용자가 열어둔 창을 그대로 점유한다.
이 상태에서 저장 확인 등 모달 대기가 걸리면 Office가 응답 없음이 될 수 있다.

**우리 자신 vs 사용자 구분(핵심)**: 인덱싱이 DRM/구형 문서를 읽으려고 COM으로
직접 띄운 Office도 같은 실행 파일(WINWORD.EXE 등)이라, 단순히 "프로세스가
있나?"로 판정하면 *우리가 띄운 인스턴스를 우리가 다시 점유로 오판*해 뒷부분
문서를 전부 스킵하는 자기 감지 버그가 생긴다. 이를 막기 위해:
  - `com_reader`가 Word/Excel COM을 띄울 때 PID·실행파일·생성 identity를
    `register_owned_app`으로 검증해 "우리 소유"로 등록한다.
  - 가드(`is_office_busy_for_ext`)는 **우리 소유가 아닌** Office 프로세스가
    있을 때만 점유로 판정한다 → 사용자가 진짜 연 Office는 계속 보호하되,
    우리 자동화 인스턴스는 무시한다.
  - `quit_com_apps`가 사이클 종료 시 `terminate_owned_office_processes`로
    Quit되지 않고 남은 우리 소유 프로세스를 강제 종료(좀비 방지)한다.

이 모듈은 프로세스 열거/종료만 수행하고 COM 객체는 생성·연결하지 않는다.

Windows 전용. 비Windows(사외 테스트)에서는 항상 "실행 중 아님"으로 판단해
COM 라우팅 로직에 영향을 주지 않는다.

CLAUDE.md 원칙3(보안·Office 의존 코드는 secure/ 안에 격리) 준수.
"""
import logging
from contextlib import contextmanager
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator, Literal

from knowmate.secure.com_fault import fatal_hresult, outer_hresult

logger = logging.getLogger(__name__)

# 확장자 → 서비스하는 Office 실행 파일명 (대문자 정규화)
_EXT_TO_PROCESS = {
    ".doc": "WINWORD.EXE",
    ".docx": "WINWORD.EXE",
    ".xls": "EXCEL.EXE",
    ".xlsx": "EXCEL.EXE",
    ".ppt": "POWERPNT.EXE",
    ".pptx": "POWERPNT.EXE",
}
_OFFICE_EXES = set(_EXT_TO_PROCESS.values())

# 프로세스 목록 캐시 (한 사이클에서 수천 건 가드 확인 시 매번 열거하지 않도록 짧은 TTL)
_CACHE_TTL_SEC = 2.0
_cache: dict[str, object] = {"ts": 0.0, "procs": None, "epoch": 0}  # procs: list[(name, pid)] | None

# 우리 자동화(COM Dispatch)가 띄운 Office 프로세스 PID와 실행 파일명.
# 워치독(별도 스레드)이 행오버 시 이 목록을 읽어 강제 종료해야 하므로 모듈
# 레벨 + 락으로 관리한다(COM 자체는 단일 워커 스레드에서만 쓰임).
_owned_lock = threading.Lock()


@dataclass(frozen=True)
class OwnedOfficeProcess:
    """검증된 Office 소유권: exe와 PID 생성 시각을 함께 보관한다."""

    exe: str
    creation_identity: int | None
    terminable: bool = True
    generation: int = field(default=0, compare=False)
    cleanup_pending: bool = False
    cleanup_generation: int = field(default=0, compare=False)


_owned_pids: dict[int, OwnedOfficeProcess] = {}
_guard_epoch = 0
_ownership_generation = 0
_shutdown_generation = 0
_cleanup_retry_at: dict[str, float] = {}
_cleanup_inflight: dict[str, int] = {}
_unverified_cleanup: dict[str, set[int]] = {}
_application_closing = threading.Event()


@dataclass(frozen=True)
class ProcessSnapshot:
    """Immutable OS list tied to the Python ownership state it observed."""

    epoch: int
    procs: tuple[tuple[str, int], ...] | None
    enumerated_at: float
    source: Literal["cache", "live"]


@dataclass(frozen=True)
class OfficeGuardDecision:
    """Fixed fields for a coherent Office admission query."""

    status: Literal["available", "busy", "deferred"]
    exe: str | None
    reason: str
    epoch: int
    source: str = "none"
    cache_age_sec: float | None = None
    listed_pids: tuple[int, ...] = ()
    external_pids: tuple[int, ...] = ()
    identity_unknown_pids: tuple[int, ...] = ()
    retries: int = 0


def _state_changed_unlocked() -> None:
    """Invalidate observations in the same transaction as an ownership mutation."""
    global _guard_epoch
    _guard_epoch += 1
    _cache.update(procs=None, ts=0.0, epoch=_guard_epoch)


def guard_epoch() -> int:
    """Return the current diagnostic mutation sequence."""
    with _owned_lock:
        return _guard_epoch


def _gate_reason_unlocked(exe: str) -> str | None:
    if exe in _OFFICE_EXES and _application_closing.is_set():
        return "application_closing"
    if exe == "POWERPNT.EXE":
        from knowmate.secure.ppt_shutdown import admission_reason
        reason = admission_reason()
        if reason:
            return reason
    if _unverified_cleanup.get(exe):
        return "unverified_cleanup"
    if _cleanup_inflight.get(exe):
        return "cleanup_inflight"
    if any(record.cleanup_pending for record in _owned_pids.values() if record.exe == exe):
        return "cleanup_pending"
    return None


def _assert_registered_app_admitted_unlocked(exe: str, pid: int, record: OwnedOfficeProcess) -> None:
    current = _owned_pids.get(pid)
    reason = _gate_reason_unlocked(exe)
    if reason or current is None or current.cleanup_pending:
        raise OfficeCleanupPendingError(exe, reason or "registered_app_unavailable")
    if current.exe != exe or not _record_matches_unlocked(current, record):
        raise OfficeCleanupPendingError(exe, "registered_app_changed")


def assert_registered_app_admitted(exe: str, pid: int, record: OwnedOfficeProcess) -> None:
    """Require the exact active proof and all admission gates to remain valid."""
    with _owned_lock:
        _assert_registered_app_admitted_unlocked(exe.upper(), pid, record)


def begin_application_shutdown() -> None:
    """Block new Office automation without waiting on the ownership lock."""
    _application_closing.set()


def ensure_application_open(exe: str) -> None:
    """Reject Office reads and activations once application exit begins."""
    if exe.upper() in _OFFICE_EXES and _application_closing.is_set():
        raise OfficeCleanupPendingError(exe, "application_closing")


def shutdown_cleanup_inflight() -> bool:
    """Report active Word/Excel cleanup callbacks under the ownership lock."""
    with _owned_lock:
        return any(_cleanup_inflight.get(exe, 0) for exe in ("WINWORD.EXE", "EXCEL.EXE"))


@dataclass(frozen=True)
class OfficeCleanupResult:
    """Outcome of a single verified process cleanup attempt."""

    confirmed_exited: frozenset[int] = frozenset()
    remaining: frozenset[int] = frozenset()
    identity_unknown: frozenset[int] = frozenset()
    failed: frozenset[int] = frozenset()
    forced_exited: frozenset[int] = frozenset()
    ownership_pending: frozenset[str] = frozenset()

    @property
    def successful(self) -> bool:
        """Whether all target processes are confirmed gone or safely reused."""
        return not (self.remaining or self.identity_unknown or self.failed or self.ownership_pending)


class OfficeCleanupPendingError(RuntimeError):
    """An owned Office process is still shutting down, so COM must be deferred."""

    def __init__(self, exe: str, detail: str, *, decision: OfficeGuardDecision | None = None) -> None:
        self.decision = decision
        self.exe = exe.upper()
        super().__init__(detail)

# 현재 진행 중인 COM 파싱 작업의 컨텍스트. Dispatch-hang 시에도 baseline 차집합만
# 으로는 사용자 동시 실행과 구별할 수 없으므로, 이 정보는 진단용으로만 보존하며
# 소유권이 검증되지 않은 PID를 종료하지 않는다.
_op_lock = threading.Lock()
_op: dict = {"exe": None, "baseline": frozenset()}


class OfficeBusyError(RuntimeError):
    """대상 Office 앱이 사용자에 의해 실행 중이라 COM 점유를 피해 건너뛸 때 발생한다."""

    def __init__(self, detail: str, *, decision: OfficeGuardDecision | None = None) -> None:
        self.decision = decision
        super().__init__(detail)


class OfficeOwnershipProbeError(RuntimeError):
    """HWND 소유권 확인 중 COM 오류가 나 원본 예외를 호출자에게 전달할 때 발생한다."""

    def __init__(self, message: str, document: Any = None) -> None:
        self.document = document
        super().__init__(message)


class OfficeOwnershipCleanupError(OfficeOwnershipProbeError):
    """소유 확인용 문서를 닫지 못해 같은 앱의 등록 재시도를 중단한다."""

    def __init__(
        self, probe_error: Exception | None, close_error: Exception, document: Any,
        restore_error: Exception | None = None,
    ) -> None:
        self.probe_error = probe_error
        self.close_error = close_error
        self.restore_error = restore_error
        self.document = document
        self.registered: tuple[int, OwnedOfficeProcess] | None = None
        self.probe_phase: str | None = getattr(probe_error, "probe_phase", None)
        self.close_phase = "blank_close"
        super().__init__("Office 소유권 확인용 창 정리 실패", document)


def process_for_ext(ext: str) -> str | None:
    """확장자를 서비스하는 Office 실행 파일명을 반환한다. 대상 아니면 None."""
    return _EXT_TO_PROCESS.get(ext.lower())


def _owned_snapshot() -> set[int]:
    """소유 PID 집합의 사본을 반환한다(락 보호)."""
    with _owned_lock:
        return set(_owned_pids)


def _owned_for_exe(exe: str) -> dict[int, OwnedOfficeProcess]:
    """현재 exe에 검증 등록된 소유 프로세스만 반환한다."""
    up = exe.upper()
    with _owned_lock:
        return {pid: owned for pid, owned in _owned_pids.items() if owned.exe == up}


def _enumerate_processes():
    """현재 실행 중인 (실행파일명 대문자, PID) 목록을 반환한다.

    Windows에서 Toolhelp32 스냅샷으로 열거한다. 실패하거나 비Windows면 None.
    None은 "판단 불가" — 호출부는 이를 "차단하지 않음(정상 진행)"으로 처리한다.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        # Toolhelp의 ANSI 엔트리는 A 접미사 없는 Process32First/Next이고,
        # 유니코드 엔트리만 W 접미사를 쓴다.
        kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        kernel32.Process32First.restype = wintypes.BOOL
        kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        kernel32.Process32Next.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return None
        out: list = []
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return out if kernel32.GetLastError() == 18 else None  # ERROR_NO_MORE_FILES
            while True:
                name = entry.szExeFile.decode("ascii", "ignore").upper()
                out.append((name, int(entry.th32ProcessID)))
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    return out if kernel32.GetLastError() == 18 else None
        finally:
            kernel32.CloseHandle(snapshot)
        return out
    except Exception as exc:  # ctypes/권한 등 예외 시 "판단 불가"
        logger.debug("프로세스 열거 실패(무시): error_type=%s", type(exc).__name__)
        return None


def _cached_process_snapshot() -> ProcessSnapshot | None:
    """Enumerate once outside the lock; discard stale publication."""
    with _owned_lock:
        epoch = _guard_epoch
        started = time.monotonic()
        if (_cache["procs"] is not None and _cache.get("epoch") == epoch
                and started - float(_cache["ts"]) < _CACHE_TTL_SEC):
            return ProcessSnapshot(epoch, tuple(_cache["procs"]), float(_cache["ts"]), "cache")
    processes = _enumerate_processes()
    immutable = tuple(processes) if processes is not None else None
    with _owned_lock:
        if epoch != _guard_epoch:
            return None
        result = ProcessSnapshot(epoch, immutable, started, "live")
        if immutable is not None and time.monotonic() - started < _CACHE_TTL_SEC:
            if _cache["procs"] is None or started >= float(_cache["ts"]):
                _cache.update(procs=immutable, ts=started, epoch=epoch)
        return result


def _cached_processes():
    """Compatibility list query; admission uses the coherent decision instead."""
    snapshot = _cached_process_snapshot()
    return snapshot.procs if snapshot is not None else None


def _pids_for(exe: str, procs) -> set:
    """열거 결과에서 해당 실행 파일명의 PID 집합을 뽑는다."""
    if not procs:
        return set()
    up = exe.upper()
    return {pid for (name, pid) in procs if name == up}


def office_pids_live(exe: str) -> set:
    """캐시를 무시하고 즉시 열거한 해당 exe의 PID 집합을 반환한다.

    COM Dispatch 전후로 새로 뜬 프로세스를 정확히 잡기 위한 소유 등록 전용
    (가드 확인은 캐시를 쓰는 _cached_processes를 사용)."""
    return _pids_for(exe, _enumerate_processes())


def prepare_powerpoint_dispatch() -> tuple[set[int], dict[int, OwnedOfficeProcess]]:
    """Fresh PPT preflight with bounded state/identity rechecks before Dispatch."""
    if sys.platform != "win32":
        return set(), {}
    for attempt in range(2):
        with _owned_lock:
            epoch = _guard_epoch
            records = {pid: record for pid, record in _owned_pids.items() if record.exe == "POWERPNT.EXE"}
            reason = _gate_reason_unlocked("POWERPNT.EXE")
        if reason:
            raise OfficeCleanupPendingError("POWERPNT.EXE", reason)
        processes = _enumerate_processes()
        if processes is None:
            raise OfficeCleanupPendingError("POWERPNT.EXE", "process_enumeration_unavailable")
        baseline = _pids_for("POWERPNT.EXE", processes)
        reusable = {}
        external = False
        unknown = False
        for pid in baseline:
            record = records.get(pid)
            if record is None or record.terminable or record.creation_identity is None:
                external = True
                continue
            identity = _process_creation_identity(pid)
            if identity is None:
                unknown = True
            elif identity != record.creation_identity:
                external = True
            else:
                reusable[pid] = record
        with _owned_lock:
            reason = _gate_reason_unlocked("POWERPNT.EXE")
            if reason:
                raise OfficeCleanupPendingError("POWERPNT.EXE", reason)
            if epoch != _guard_epoch:
                continue
            if external:
                raise OfficeBusyError("사용자 PowerPoint가 실행 중입니다")
            if unknown:
                raise OfficeCleanupPendingError("POWERPNT.EXE", "identity_unavailable")
            return baseline, reusable
    raise OfficeCleanupPendingError("POWERPNT.EXE", "guard_state_changed")


def register_owned_pids(pids: set[int]) -> None:
    """우리(COM 자동화)가 띄운 Office 프로세스 PID를 소유로 등록한다."""
    if not pids:
        return
    # 이전 공개 API 호환용. 생성 identity가 없으므로 이 경로의 PID는 절대
    # 강제 종료 대상이 되지 않는다. 새 생성 경로는 register_owned_app만 쓴다.
    with _owned_lock:
        changed = False
        for pid in pids:
            record = OwnedOfficeProcess("", None)
            current = _owned_pids.get(pid)
            if current is None or not _record_matches_unlocked(current, record):
                _owned_pids[pid] = record
                changed = True
        if changed:
            _state_changed_unlocked()
    logger.debug("우리 소유 Office PID 등록: %s", sorted(pids))


def _pid_from_hwnd(hwnd: int) -> int | None:
    """Office 창 HWND의 프로세스 PID를 반환한다(Windows 외에서는 None)."""
    if sys.platform != "win32" or not isinstance(hwnd, int) or hwnd == 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:
        return None


def _process_creation_identity(pid: int) -> int | None:
    """Windows GetProcessTimes의 생성 FILETIME을 PID 재사용 방지 identity로 반환한다."""
    if sys.platform != "win32" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            return _process_creation_identity_from_handle(kernel32, handle)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _process_creation_identity_from_handle(kernel32, handle) -> int | None:
    """이미 연 프로세스 핸들의 생성 FILETIME을 반환한다.

    종료 직전에는 같은 핸들로 identity 확인과 TerminateProcess를 연속 수행해야
    확인 뒤 PID가 재사용되는 TOCTOU 경합을 피할 수 있다.
    """
    try:
        import ctypes
        from ctypes import wintypes

        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    except Exception:
        return None


def _app_hwnd(app: Any, exe: str = "") -> int | None:
    """앱 HWND를 읽고 COM의 미지원 속성 오류만 건너뛴다."""
    for attr in ("Hwnd", "HWND"):
        try:
            hwnd = getattr(app, attr)
        except AttributeError as exc:
            if fatal_hresult(exc, app_exe=exe.upper()) is not None:
                wrapped = OfficeOwnershipProbeError("Office HWND 소유권 확인 실패")
                wrapped.probe_phase = "window_verify"
                raise wrapped from exc
            logger.info("Office HWND 조회: exe=%s property=%s reason=unsupported", exe, attr)
            continue
        except Exception as exc:
            code = outer_hresult(exc)
            if fatal_hresult(exc, app_exe=exe.upper()) is None and code in {0x80020003, 0x80020006}:
                logger.info(
                    "Office HWND 조회: exe=%s property=%s reason=unsupported HRESULT=%s",
                    exe, attr, code & 0xFFFFFFFF,
                )
                continue
            logger.warning(
                "Office HWND 조회 실패: exe=%s property=%s error_type=%s HRESULT=%s",
                exe, attr, type(exc).__name__, (code & 0xFFFFFFFF) if isinstance(code, int) else None,
            )
            wrapped = OfficeOwnershipProbeError("Office HWND 소유권 확인 실패")
            wrapped.probe_phase = "window_verify"
            raise wrapped from exc
        if isinstance(hwnd, int) and hwnd != 0:
            return hwnd
        logger.info(
            "Office HWND 조회: exe=%s property=%s reason=%s value_type=%s callable=%s is_none=%s",
            exe, attr, "zero" if isinstance(hwnd, int) and hwnd == 0 else "invalid_value",
            type(hwnd).__name__, callable(hwnd), hwnd is None,
        )
    return None


@contextmanager
def _ppt_ownership_window(app: Any) -> Iterator[Callable[[], int | None]]:
    """HWND 미지원 PowerPoint는 COM으로 지정한 고유 창 제목으로 확인한다."""
    from knowmate.secure.com_fault import outer_hresult as hresult_of

    document = None
    original_caption = None
    caption_changed = False
    original_security = None
    security_changed = False
    probe_error: Exception | None = None
    probe_phase = None
    marker = "AegisDesk-ownership-" + uuid.uuid4().hex
    phase = "configure"
    logger.info("[com] PowerPoint 소유 확인 대체 경로 시작: method=caption_marker stage=%s", phase)
    try:
        original_security = app.AutomationSecurity
        security_changed = True  # setter가 일부 적용된 뒤 실패해도 복원한다.
        app.AutomationSecurity = 3
        original_caption = app.Caption
        phase = "blank_create"
        logger.info("[com] PowerPoint 소유 확인 단계 시작: stage=%s", phase)
        document = app.Presentations.Add(-1)  # msoTrue: 창이 있는 빈 프레젠테이션
        # setter가 일부 적용된 뒤 실패해도 원래 제목을 복원해야 한다.
        caption_changed = True
        phase = "caption_set"
        logger.info("[com] PowerPoint 소유 확인 단계 시작: stage=%s", phase)
        app.Caption = marker

        def read_hwnd() -> int | None:
            if app.Caption != marker:
                logger.warning("[com] PowerPoint 소유 확인 실패: reason=caption_changed")
                return None
            return _hwnd_for_caption_marker(marker)

        phase = "window_verify"
        logger.info("[com] PowerPoint 소유 확인 단계 시작: stage=%s", phase)
        yield read_hwnd
    except OfficeOwnershipProbeError as exc:
        probe_error = exc
        probe_phase = phase
        exc.document = document
        exc.probe_phase = phase
        original = exc.__cause__ or exc
        logger.warning(
            "[com] PowerPoint 소유 확인 오류: stage=%s error_type=%s HRESULT=%s",
            phase, type(original).__name__, hresult_of(original),
        )
        raise
    except Exception as exc:
        probe_error = exc
        probe_phase = phase
        logger.warning(
            "[com] PowerPoint 소유 확인 오류: stage=%s error_type=%s HRESULT=%s",
            phase, type(exc).__name__, hresult_of(exc),
        )
        wrapped = OfficeOwnershipProbeError("PowerPoint 확인용 창 소유권 확인 실패", document)
        wrapped.probe_phase = phase
        raise wrapped from exc
    finally:
        fatal_probe = probe_error is not None and fatal_hresult(probe_error, app_exe="POWERPNT.EXE") is not None
        # 끊어진 RPC에는 제목 복원/Close를 다시 요청하지 않는다.
        if fatal_probe:
            logger.warning("[com] PowerPoint 확인용 창 정리 연기: reason=rpc_disconnected")
        else:
            cleanup_error = None
            cleanup_phase = None
            restore_error = None
            try:
                if caption_changed:
                    phase = "caption_restore"
                    logger.info("[com] PowerPoint 소유 확인 단계 시작: stage=%s", phase)
                    app.Caption = original_caption
                if document is not None:
                    phase = "blank_close"
                    logger.info("[com] PowerPoint 소유 확인 단계 시작: stage=%s", phase)
                    document.Close()  # 확인용 빈 프레젠테이션만 닫고 저장하지 않는다.
            except Exception as exc:
                logger.warning(
                    "[com] PowerPoint 확인용 창 정리 실패: stage=%s error_type=%s HRESULT=%s",
                    phase, type(exc).__name__, hresult_of(exc),
                )
                cleanup_error = exc
                cleanup_phase = phase
            # 일반 제목 복원/닫기 실패에도 사용자 앱의 보안 설정은 복원한다.
            # 정리 자체에서 fatal RPC가 발생했다면 COM 호출을 더 하지 않는다.
            fatal_cleanup = cleanup_error is not None and fatal_hresult(cleanup_error, app_exe="POWERPNT.EXE") is not None
            if security_changed and not fatal_cleanup:
                try:
                    phase = "security_restore"
                    logger.info("[com] PowerPoint 소유 확인 단계 시작: stage=%s", phase)
                    app.AutomationSecurity = original_security
                except Exception as exc:
                    logger.warning(
                        "[com] PowerPoint 확인용 설정 복원 실패: stage=%s error_type=%s HRESULT=%s",
                        phase, type(exc).__name__, hresult_of(exc),
                    )
                    if cleanup_error is None:
                        cleanup_error = exc
                        cleanup_phase = phase
                    else:
                        restore_error = exc
            if cleanup_error is not None:
                wrapped = OfficeOwnershipCleanupError(probe_error, cleanup_error, document, restore_error)
                wrapped.probe_phase = probe_phase
                wrapped.close_phase = cleanup_phase
                raise wrapped from cleanup_error
            if document is not None or caption_changed:
                logger.info("[com] PowerPoint 확인용 창 정리 완료")


def _hwnd_for_caption_marker(marker: str) -> int | None:
    """고유 COM 제목을 포함한 최상위 창 하나만 반환하고 열거 실패는 거부한다."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    matches: list[int] = []
    failures: list[Exception] = []

    def visit(hwnd, _param):
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                title = ctypes.create_unicode_buffer(min(length + 1, 32768))
                if user32.GetWindowTextW(hwnd, title, len(title)) and marker in title.value:
                    matches.append(int(hwnd))
        except Exception as exc:
            failures.append(exc)
        return True

    if not user32.EnumWindows(callback_type(visit), 0) or failures:
        logger.warning("PowerPoint 소유 확인용 창 열거 실패")
        return None
    if len(matches) != 1:
        logger.warning(
            "PowerPoint 소유 확인용 창 식별 실패: reason=%s matched_windows=%d",
            "window_not_found" if not matches else "window_ambiguous", len(matches),
        )
    return matches[0] if len(matches) == 1 else None


@contextmanager
def _ownership_window(exe: str, app: Any) -> Iterator[Callable[[], int | None]]:
    """앱 HWND가 없으면 Word/PPT의 확인용 창으로 COM 소유권을 확인한다."""
    hwnd = _app_hwnd(app, exe)
    if hwnd is None and exe.upper() == "POWERPNT.EXE":
        with _ppt_ownership_window(app) as read_hwnd:
            yield read_hwnd
        return
    if hwnd is not None or exe.upper() != "WINWORD.EXE":
        yield lambda: _app_hwnd(app, exe)
        return
    document = None
    probe_error: Exception | None = None
    phase = "configure"
    try:
        app.AutomationSecurity = 3
        phase = "blank_create"
        document = app.Documents.Add()
        phase = "window_verify"
        yield lambda: document.Windows.Item(1).Hwnd
    except OfficeOwnershipProbeError as exc:
        probe_error = exc
        exc.document = document
        exc.probe_phase = phase
        raise
    except Exception as exc:
        probe_error = exc
        wrapped = OfficeOwnershipProbeError("Word 빈 문서 창 소유권 확인 실패", document)
        wrapped.probe_phase = phase
        raise wrapped from exc
    finally:
        fatal_probe = probe_error is not None and fatal_hresult(probe_error, app_exe=exe.upper()) is not None
        # 이미 끊어진 RPC에 Close를 다시 요청하면 소유 등록 전 워커가 멈출 수 있다.
        # 닫지 않은 문서는 probe 예외로 전달해 생성 워커의 TLS에 보존한다.
        if document is not None and not fatal_probe:
            try:
                document.Close(False)
            except Exception as exc:
                wrapped = OfficeOwnershipCleanupError(probe_error, exc, document)
                wrapped.probe_phase = phase
                raise wrapped from exc


def _register_owned_window(
    exe: str, baseline: set[int], read_hwnd: Callable[[], int | None],
    expected_owned: dict[int, OwnedOfficeProcess] | None = None,
) -> tuple[int, OwnedOfficeProcess] | None:
    """창의 PID·EXE·생성 identity를 검증하고 등록한 프로세스 기록을 반환한다."""
    with _owned_lock:
        captured_epoch = _guard_epoch
        captured_records = dict(_owned_pids)
    current_procs = _enumerate_processes()
    if current_procs is None:
        logger.warning("Office 소유 등록 실패: exe=%s reason=process_enumeration_unavailable", exe)
        return None
    hwnd = read_hwnd()
    if hwnd is None:
        logger.warning("Office 소유 등록 실패: exe=%s reason=hwnd_unavailable", exe)
        return None
    pid = _pid_from_hwnd(hwnd)
    up = exe.upper()
    current = _pids_for(up, current_procs)
    creation_identity = _process_creation_identity(pid) if pid is not None else None
    previous = (expected_owned or {}).get(pid)
    reconnect = (
        up == "POWERPNT.EXE" and previous is not None and not previous.terminable
        and not previous.cleanup_pending and previous.creation_identity == creation_identity
    )
    if pid is None or (pid in baseline and not reconnect) or pid not in current or creation_identity is None:
        logger.warning(
            "Office 소유 등록 실패: exe=%s PID=%s baseline=%s live=%s identity_available=%s",
            up, pid, pid in baseline, pid in current, creation_identity is not None,
        )
        return None

    # HWND→PID 확인과 생성 identity 조회 사이 원래 프로세스가 끝나 PID가 사용자
    # Office에 재사용될 수 있다. identity 획득 뒤 COM 객체의 HWND, PID, EXE,
    # identity를 한 번 더 확인해 연결이 바뀐 경우 소유 등록을 거부한다. 이후 종료
    # 시점에도 같은 identity를 동일 프로세스 핸들에서 다시 검증한다.
    try:
        confirmed_hwnd = read_hwnd()
    except OfficeOwnershipProbeError:
        raise
    except Exception as exc:
        wrapped = OfficeOwnershipProbeError("Office HWND 소유권 재확인 실패")
        wrapped.probe_phase = "window_verify"
        raise wrapped from exc
    confirmed_pid = _pid_from_hwnd(confirmed_hwnd)
    confirmed_procs = _enumerate_processes()
    confirmed_identity = (
        _process_creation_identity(confirmed_pid) if confirmed_pid is not None else None
    )
    if (
        confirmed_hwnd != hwnd
        or confirmed_pid != pid
        or confirmed_procs is None
        or pid not in _pids_for(up, confirmed_procs)
        or confirmed_identity != creation_identity
    ):
        logger.warning("Office 소유 등록 실패: exe=%s PID=%s reason=confirmation_changed", up, pid)
        return None
    global _ownership_generation, _shutdown_generation
    with _owned_lock:
        current_record = _owned_pids.get(pid)
        captured_record = captured_records.get(pid)
        if ((captured_record is None) != (current_record is None) or
                (captured_record is not None and not _record_matches_unlocked(current_record, captured_record))):
            return None
        late_gate = _gate_reason_unlocked(up)
        if up == "POWERPNT.EXE":
            if captured_epoch != _guard_epoch or late_gate:
                return None
            if previous is not None and (current_record is None or not _record_matches_unlocked(current_record, previous)):
                return None
            if previous is None and current_record is not None:
                return None
        elif captured_epoch != _guard_epoch and not late_gate:
            return None
        if reconnect:
            return pid, previous
        _ownership_generation += 1
        record = OwnedOfficeProcess(up, creation_identity, up != "POWERPNT.EXE", _ownership_generation)
        if up in {"WINWORD.EXE", "EXCEL.EXE"} and late_gate:
            _shutdown_generation += 1
            record = replace(record, cleanup_pending=True, cleanup_generation=_shutdown_generation)
        _owned_pids[pid] = record
        _state_changed_unlocked()
    logger.debug("우리 소유 Office PID 등록: %s=%d", up, pid)
    return pid, record


def _register_owned_app_record(
    exe: str, baseline: set[int], app: Any,
    expected_owned: dict[int, OwnedOfficeProcess] | None = None,
) -> tuple[int, OwnedOfficeProcess] | None:
    """Return the exact HWND/identity proof registered for this app."""
    if sys.platform != "win32":
        return None
    if exe.upper() == "POWERPNT.EXE":
        with _owned_lock:
            captured_epoch = _guard_epoch
            records = {pid: record for pid, record in _owned_pids.items() if record.exe == exe.upper()}
        procs = _enumerate_processes()
        if procs is None:
            return None
        expected_owned = expected_owned if expected_owned is not None else records
        valid = True
        for pid in baseline & _pids_for(exe, procs):
            record = expected_owned.get(pid)
            if (record is None or record.terminable or record.cleanup_pending
                    or record.creation_identity is None or _process_creation_identity(pid) != record.creation_identity):
                valid = False
        with _owned_lock:
            if captured_epoch != _guard_epoch or _gate_reason_unlocked(exe.upper()) or not valid:
                return None
            if any(pid not in _owned_pids or not _record_matches_unlocked(_owned_pids[pid], rec)
                   for pid, rec in expected_owned.items()):
                return None
    registered = None
    try:
        with _ownership_window(exe, app) as read_hwnd:
            registered = _register_owned_window(exe, baseline, read_hwnd, expected_owned)
    except OfficeOwnershipCleanupError as exc:
        exc.registered = registered
        if registered is not None:
            pid, record = registered
            _mark_owned_cleanup_pending(pid, record)
        raise
    if registered is not None:
        pid, record = registered
        if exe.upper() == "POWERPNT.EXE" and (expected_owned or {}).get(pid) is record:
            logger.info("[com] PowerPoint 기존 소유 재연결 확인: PID=%d generation=%d", pid, record.generation)
        logger.info(
            "Office 소유 등록 완료: exe=%s PID=%d generation=%d terminable=%s",
            record.exe, pid, record.generation, record.terminable,
        )
    return registered


def register_owned_app(
    exe: str, baseline: set[int], app: Any,
    expected_owned: dict[int, OwnedOfficeProcess] | None = None,
) -> bool:
    """Compatibility facade for the exact ownership registration core."""
    if sys.platform != "win32":
        return True
    return _register_owned_app_record(exe, baseline, app, expected_owned) is not None


def _mark_owned_cleanup_pending(pid: int, record: OwnedOfficeProcess) -> OwnedOfficeProcess | None:
    """등록 generation이 같은 기록만 정리 대기로 바꾼다."""
    global _shutdown_generation
    with _owned_lock:
        current = _owned_pids.get(pid)
        if current is None or not _record_matches_unlocked(current, record):
            return None
        if current.cleanup_pending:
            return current
        _shutdown_generation += 1
        pending = replace(record, cleanup_pending=True, cleanup_generation=_shutdown_generation)
        _owned_pids[pid] = pending
        _state_changed_unlocked()
        return pending


def begin_unverified_cleanup(exe: str) -> int:
    """소유권 미확인 정리를 EXE 단위로 차단하고 해제용 토큰을 반환한다."""
    global _shutdown_generation
    with _owned_lock:
        _shutdown_generation += 1
        token = _shutdown_generation
        _unverified_cleanup.setdefault(exe.upper(), set()).add(token)
        _state_changed_unlocked()
        return token


def finish_unverified_cleanup(exe: str, token: int) -> None:
    """일치하는 미검증 정리 요청만 해제한다."""
    with _owned_lock:
        tokens = _unverified_cleanup.get(exe.upper())
        if tokens is not None and token in tokens:
            tokens.remove(token)
            _state_changed_unlocked()
            if not tokens:
                _unverified_cleanup.pop(exe.upper(), None)


def unverified_cleanup_pending() -> frozenset[str]:
    """소유권 미확인으로 차단된 Office EXE 목록을 반환한다."""
    with _owned_lock:
        return frozenset(_unverified_cleanup)


def office_processes_absent(exe: str) -> bool:
    """열거에 성공하고 해당 Office 프로세스가 하나도 없을 때만 True를 반환한다."""
    procs = _enumerate_processes()
    return procs is not None and not _pids_for(exe, procs)


def prune_unverified_cleanup() -> None:
    """COM 호출 없이 실제 Office 부재가 확인된 미검증 요청만 해제한다."""
    with _owned_lock:
        snapshot = {exe: set(tokens) for exe, tokens in _unverified_cleanup.items()}
    for exe, tokens in snapshot.items():
        if office_processes_absent(exe):
            for token in tokens:
                finish_unverified_cleanup(exe, token)


def clear_owned_pids() -> set[int]:
    """테스트/명시적 초기화용으로 소유 기록과 재시도 시각을 비운다."""
    with _owned_lock:
        prev = set(_owned_pids)
        _owned_pids.clear()
        _cleanup_retry_at.clear()
        _unverified_cleanup.clear()
        _application_closing.clear()
        _state_changed_unlocked()
    from knowmate.secure.ppt_shutdown import reset_for_tests
    reset_for_tests()
    return prev


def take_owned_processes() -> dict[int, OwnedOfficeProcess]:
    """Legacy alias returning an owned-process snapshot without clearing records."""
    with _owned_lock:
        return dict(_owned_pids)


def begin_owned_cleanup(exe: str | None = None) -> dict[int, OwnedOfficeProcess]:
    """Mark owned Word/Excel records pending and return an immutable snapshot."""
    global _shutdown_generation
    with _owned_lock:
        _shutdown_generation += 1
        cleanup_generation = _shutdown_generation
        snapshot: dict[int, OwnedOfficeProcess] = {}
        changed = False
        for pid, record in tuple(_owned_pids.items()):
            if record.exe in {"WINWORD.EXE", "EXCEL.EXE"} and (exe is None or record.exe == exe.upper()):
                pending = record if record.cleanup_pending else replace(
                    record, cleanup_pending=True, cleanup_generation=cleanup_generation,
                )
                _owned_pids[pid] = pending
                changed = changed or pending is not record
                snapshot[pid] = pending
        if changed:
            _state_changed_unlocked()
        return snapshot


def _record_matches(pid: int, record: OwnedOfficeProcess) -> bool:
    """Check that a cleanup snapshot still refers to the current registry record."""
    with _owned_lock:
        current = _owned_pids.get(pid)
        return (
            current is not None and current.cleanup_pending
            and current.exe == record.exe
            and current.creation_identity == record.creation_identity
            and current.terminable == record.terminable
            and current.generation == record.generation
            and current.cleanup_generation == record.cleanup_generation
        )


def _remove_owned_if_matches(pid: int, record: OwnedOfficeProcess) -> bool:
    """Remove a record only if PID, identity, and registration generation still match."""
    with _owned_lock:
        current = _owned_pids.get(pid)
        if current is None or not _record_matches_unlocked(current, record):
            return False
        _owned_pids.pop(pid, None)
        _state_changed_unlocked()
        return True


def _record_matches_unlocked(current: OwnedOfficeProcess, record: OwnedOfficeProcess) -> bool:
    """Compare record identity, including the non-comparing generation field."""
    return (
        current.exe == record.exe
        and current.creation_identity == record.creation_identity
        and current.terminable == record.terminable
        and current.generation == record.generation
        and current.cleanup_generation == record.cleanup_generation
        and current.cleanup_pending == record.cleanup_pending
    )


def finish_owned_cleanup(snapshot: dict[int, OwnedOfficeProcess], unresolved: set[int]) -> None:
    """Drop snapshot records confirmed gone by the identity-aware grace wait."""
    for pid, record in snapshot.items():
        if pid not in unresolved:
            _remove_owned_if_matches(pid, record)


def prune_released_nonterminable_processes() -> None:
    """Remove vanished/reused PowerPoint records without quitting or killing Office."""
    with _owned_lock:
        snapshot = {pid: rec for pid, rec in _owned_pids.items() if not rec.terminable}
    if not snapshot:
        return
    procs = _enumerate_processes()
    if procs is None:
        return
    names = {pid: name for name, pid in procs}
    for pid, record in snapshot.items():
        identity = _process_creation_identity(pid) if names.get(pid) == record.exe else None
        if names.get(pid) != record.exe or (
            identity is not None and identity != record.creation_identity
        ):
            _remove_owned_if_matches(pid, record)


def ensure_office_available(exe: str, retry_cooldown_sec: float = 30.0) -> None:
    """Retry pending cleanup at most once per cooldown, then gate COM if unresolved."""
    up = exe.upper()
    ensure_application_open(up)
    if up == "POWERPNT.EXE":
        from knowmate.secure.ppt_shutdown import ensure_admitted
        ensure_admitted()
    with _owned_lock:
        tokens = set(_unverified_cleanup.get(up, ()))
    if tokens:
        if office_processes_absent(up):
            for token in tokens:
                finish_unverified_cleanup(up, token)
        with _owned_lock:
            unresolved_ownership = bool(_unverified_cleanup.get(up))
        if unresolved_ownership:
            raise OfficeCleanupPendingError(up, f"{up} 소유권 미확인 문서 정리 대기 중")
    with _owned_lock:
        inflight = _cleanup_inflight.get(up, 0) > 0
    if inflight:
        raise OfficeCleanupPendingError(up, f"{up} 종료 정리 진행 중")
    pending = {pid: rec for pid, rec in _owned_for_exe(up).items() if rec.cleanup_pending}
    if not pending:
        return
    now = time.monotonic()
    with _owned_lock:
        retry_at = _cleanup_retry_at.get(up, 0.0)
        if now >= retry_at:
            _cleanup_retry_at[up] = now + retry_cooldown_sec
            retry = True
        else:
            retry = False
    if retry:
        try:
            result = cleanup_owned_processes(pending, timeout_sec=1.0)
            _log_cleanup_result(up, result, "재시도")
        except Exception as exc:
            logger.error("Office 종료 재시도 예외: exe=%s error_type=%s", up, type(exc).__name__)
    with _owned_lock:
        unresolved = {pid for pid, rec in _owned_pids.items() if rec.exe == up and rec.cleanup_pending}
        inflight = _cleanup_inflight.get(up, 0) > 0
    if unresolved or inflight:
        raise OfficeCleanupPendingError(up, f"{up} 종료 확인 대기 중: PID {sorted(unresolved)}")


def begin_shutdown_cleanup(exe: str) -> None:
    """Gate new Office activation while one timeout cleanup is in flight."""
    with _owned_lock:
        up = exe.upper()
        _cleanup_inflight[up] = _cleanup_inflight.get(up, 0) + 1
        _state_changed_unlocked()


def finish_shutdown_cleanup(exe: str) -> None:
    """Release one shutdown cleanup gate after its OS work has completed."""
    with _owned_lock:
        up = exe.upper()
        count = _cleanup_inflight.get(up, 0)
        if count <= 1:
            _cleanup_inflight.pop(up, None)
        else:
            _cleanup_inflight[up] = count - 1
        if count:
            _state_changed_unlocked()


def _log_cleanup_result(exe: str, result: OfficeCleanupResult, phase: str, *, attempt_id: int | None = None) -> None:
    """Log cleanup status without including document content or paths."""
    from knowmate.secure.com_stage import get_attempt_context
    context = get_attempt_context()
    if attempt_id is None and context is not None:
        attempt_id = context.attempt_id
    logger.info(
        "[com] Office 정리 결과: attempt_id=%s exe=%s phase=%s epoch=%d actual_exit=%s remaining=%s identity_unknown=%s failed=%s",
        attempt_id, exe, phase, guard_epoch(), sorted(result.confirmed_exited), sorted(result.remaining),
        sorted(result.identity_unknown), sorted(result.failed),
    )


def inspect_office_for_ext(ext: str) -> OfficeGuardDecision:
    """Return available, busy or deferred after a final state recheck."""
    exe = process_for_ext(ext)
    if exe is None:
        with _owned_lock:
            return OfficeGuardDecision("available", None, "unsupported_extension", _guard_epoch)
    for attempt in range(2):
        if attempt:
            logger.debug("[com] Office guard 재조회: exe=%s reason=state_changed retry=%d", exe, attempt)
        with _owned_lock:
            reason = _gate_reason_unlocked(exe)
            if reason:
                return OfficeGuardDecision("deferred", exe, reason, _guard_epoch, retries=attempt)
        snapshot = _cached_process_snapshot()
        if snapshot is None:
            logger.debug("[com] process snapshot 폐기: exe=%s reason=state_changed", exe)
            continue
        running = _pids_for(exe, snapshot.procs)
        with _owned_lock:
            if snapshot.epoch != _guard_epoch:
                continue
            records = {pid: record for pid, record in _owned_pids.items() if pid in running}
        unknown = set()
        if sys.platform != "win32":
            proven = set(records)
        else:
            proven = set()
            for pid, record in records.items():
                if record.exe != exe or record.creation_identity is None:
                    continue
                identity = _process_creation_identity(pid)
                if identity is None:
                    unknown.add(pid)
                elif identity == record.creation_identity:
                    proven.add(pid)
        external = running - proven
        with _owned_lock:
            reason = _gate_reason_unlocked(exe)
            if reason:
                return OfficeGuardDecision("deferred", exe, reason, _guard_epoch, retries=attempt)
            if snapshot.epoch != _guard_epoch:
                continue
            status = "busy" if external else "available"
            reason = "external_process" if external else (
                "enumeration_unavailable" if snapshot.procs is None else "no_external_process")
            return OfficeGuardDecision(status, exe, reason, _guard_epoch, snapshot.source,
                                       max(0.0, time.monotonic() - snapshot.enumerated_at),
                                       tuple(sorted(running)), tuple(sorted(external)),
                                       tuple(sorted(unknown)), attempt)
    with _owned_lock:
        return OfficeGuardDecision("deferred", exe, "guard_state_changed", _guard_epoch, retries=1)


def is_office_busy_for_ext(ext: str) -> bool:
    """Compatibility busy query; deferred must never authorize admission."""
    return inspect_office_for_ext(ext).status == "busy"


def begin_com_op(exe: str) -> None:
    """COM 파싱 작업 시작을 기록한다(워치독의 Dispatch-hang 대비).

    시작 전 해당 exe PID를 진단용 baseline으로 저장한다. 소유 등록 전 PID는
    사용자 동시 실행과 구별할 수 없으므로 baseline 차집합만으로 종료하지 않는다.
    """
    if not exe:
        return
    baseline = office_pids_live(exe)
    with _op_lock:
        _op["exe"] = exe.upper()
        _op["baseline"] = frozenset(baseline)


def end_com_op() -> None:
    """COM 파싱 작업 종료를 기록한다(워치독 컨텍스트 해제)."""
    with _op_lock:
        _op["exe"] = None
        _op["baseline"] = frozenset()


def _verified_owned_processes(
    exe: str | None = None,
    procs=None,
    owned: dict[int, OwnedOfficeProcess] | None = None,
) -> dict[int, OwnedOfficeProcess]:
    """현재 EXE와 생성 identity까지 일치하는 프로세스만 강제종료 후보로 반환한다."""
    if procs is None:
        procs = _enumerate_processes()
    if procs is None:
        return {}
    if owned is None:
        with _owned_lock:
            owned = dict(_owned_pids)
    names = {pid: name for name, pid in procs}
    target_exe = exe.upper() if exe else None
    verified: dict[int, OwnedOfficeProcess] = {}
    for pid, record in owned.items():
        if record.creation_identity is None:
            continue
        if target_exe is not None and record.exe != target_exe:
            continue
        if names.get(pid) != record.exe:
            continue
        if _process_creation_identity(pid) != record.creation_identity:
            continue
        verified[pid] = record
    return verified


def terminate_stuck_office(exe: str) -> int:
    """워치독 발동 — 블로킹된 COM 호출을 풀기 위해 해당 exe의 '우리' 프로세스를 종료.

    현재 exe·PID 생성 identity·terminable 플래그가 모두 일치하는 검증 소유
    Word/Excel만 종료한다. Dispatch-hang의 baseline 차집합은 사용자 동시 실행과
    구별할 수 없어 종료하지 않는다. 반환: 종료 시도한 프로세스 수.
    """
    if not exe or sys.platform != "win32":
        return 0
    up = exe.upper()
    # Dispatch 반환 전에는 HWND·생성 identity로 소유권을 검증할 수 없다. baseline
    # 차집합은 사용자가 같은 순간 연 Office일 수 있으므로 절대 강제 종료하지 않는다.
    owned = begin_owned_cleanup(up)
    targets = {pid: rec for pid, rec in owned.items() if rec.terminable}
    if not targets:
        return 0
    result = cleanup_owned_processes(targets)
    _log_cleanup_result(up, result, "read timeout")
    if result.confirmed_exited:
        logger.warning(
            "COM 행오버 추정 — %s 프로세스 실제 종료 확인 %d개: %s",
            up, len(result.confirmed_exited), sorted(result.confirmed_exited),
        )
    # Resiliency 표식은 cleanup_owned_processes에서 강제종료 확인 후에만 정리한다.
    return len(result.forced_exited)


def recover_poisoned_office(exe: str, timeout_sec: float = 2.0) -> bool:
    """RPC poison 직후 해당 EXE의 검증된 소유 PID만 종료·확인·등록 해제한다."""
    if not exe or sys.platform != "win32":
        return False
    up = exe.upper()
    registered = _owned_for_exe(up)
    if not registered:
        return False
    # PowerPoint MultiUse 세션은 절대 종료하지 않는다.
    if any(not record.terminable for record in registered.values()):
        logger.warning("COM poison 복구 보류 — %s MultiUse 세션은 종료하지 않음", up)
        return False
    before_epoch = guard_epoch()
    snapshot = begin_owned_cleanup(up)
    if not snapshot:
        return False
    result = cleanup_owned_processes(snapshot, timeout_sec=timeout_sec)
    from knowmate.secure.com_stage import get_attempt_context
    context = get_attempt_context()
    logger.info("[com] COM poison 복구 상태: attempt_id=%s exe=%s epoch_before=%d epoch_after=%d cleanup_generations=%s",
                context.attempt_id if context is not None else None, up, before_epoch, guard_epoch(),
                sorted({record.cleanup_generation for record in snapshot.values()}))
    _log_cleanup_result(up, result, "poison recovery")
    if result.successful:
        logger.warning("COM poison 즉시 복구 완료: %s actual_exit=%s", up, sorted(result.confirmed_exited))
        return True
    return False


_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000


def wait_for_owned_exit(
    owned: dict[int, OwnedOfficeProcess] | set[int], timeout_sec: float,
) -> tuple[set[int], float]:
    """Wait for natural exit within one deadline; retain unqueryable processes.

    For verified records, creation identity and exit are checked on the same
    handle. A reused PID belongs to someone else: the old process is already
    gone, so neither waiting on nor terminating its replacement is appropriate.
    """
    if not owned or sys.platform != "win32":
        return set(owned), 0.0

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    t0 = time.monotonic()
    deadline = t0 + timeout_sec
    unresolved: set[int] = set()
    no_handle: set[int] = set()
    records = owned if isinstance(owned, dict) else {}
    for pid in owned:
        record = records.get(pid)
        handle = kernel32.OpenProcess(_SYNCHRONIZE | 0x1000, False, pid)
        if not handle:
            no_handle.add(pid)
            continue
        try:
            if record is not None:
                identity = _process_creation_identity_from_handle(kernel32, handle)
                if identity is None or record.creation_identity is None:
                    unresolved.add(pid)
                    logger.warning(
                        "Office 종료 유예 identity 확인 실패: PID=%d exe=%s error=%d",
                        pid, record.exe, ctypes.get_last_error(),
                    )
                    continue
                if identity != record.creation_identity:
                    continue
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            result = kernel32.WaitForSingleObject(handle, remaining_ms)
            if result != _WAIT_OBJECT_0:
                unresolved.add(pid)
                if result == 0xFFFFFFFF:
                    logger.warning("Office 종료 유예 대기 실패: PID=%d error=%d", pid, ctypes.get_last_error())
        except Exception as exc:
            unresolved.add(pid)
            logger.warning("Office 종료 유예 확인 실패: PID=%d error_type=%s", pid, type(exc).__name__)
        finally:
            kernel32.CloseHandle(handle)

    if no_handle:
        # OpenProcess 실패만으로 종료를 단정하지 않는다. 열거 불가이면 보존한다.
        procs = _enumerate_processes()
        if procs is None:
            unresolved.update(no_handle)
        else:
            names = {pid: name for name, pid in procs}
            for pid in no_handle:
                record = records.get(pid)
                if record is not None:
                    if names.get(pid) == record.exe:
                        unresolved.add(pid)
                elif names.get(pid) in _OFFICE_EXES:
                    unresolved.add(pid)
    return unresolved, time.monotonic() - t0


def cleanup_owned_processes(
    owned: dict[int, OwnedOfficeProcess], timeout_sec: float = 2.0,
    *, deadline: float | None = None,
) -> OfficeCleanupResult:
    """Run verified process cleanup behind EXE gates until all OS work finishes."""
    exes = {record.exe for record in owned.values() if record.exe in {"WINWORD.EXE", "EXCEL.EXE"}}
    for exe in exes:
        begin_shutdown_cleanup(exe)
    try:
        return _cleanup_owned_processes_impl(owned, timeout_sec, deadline)
    finally:
        for exe in exes:
            finish_shutdown_cleanup(exe)


def _cleanup_owned_processes_impl(
    owned: dict[int, OwnedOfficeProcess], timeout_sec: float, deadline: float | None = None,
) -> OfficeCleanupResult:
    """Terminate identity-matched owned Office processes and confirm actual exit."""
    if not owned:
        return OfficeCleanupResult()
    if sys.platform != "win32":
        return OfficeCleanupResult(remaining=frozenset(owned))
    procs = _enumerate_processes()
    if procs is None:
        return OfficeCleanupResult(identity_unknown=frozenset(owned))
    names = {pid: name for name, pid in procs}
    exited: set[int] = set()
    remaining: set[int] = set()
    unknown: set[int] = set()
    failed: set[int] = set()
    forced_exited: set[int] = set()
    for pid, record in owned.items():
        if deadline is not None and time.monotonic() >= deadline:
            remaining.add(pid)
            continue
        if not _record_matches(pid, record):
            failed.add(pid)
            continue
        if names.get(pid) != record.exe:
            if _remove_owned_if_matches(pid, record):
                exited.add(pid)
            continue
        if not record.terminable:
            remaining.add(pid)
            continue
        wait_sec = timeout_sec if deadline is None else min(timeout_sec, max(0.0, deadline - time.monotonic()))
        state = _terminate_and_confirm(pid, record, wait_sec)
        if state in {"exited", "terminated", "identity_changed"}:
            if _remove_owned_if_matches(pid, record):
                exited.add(pid)
                if state == "terminated":
                    forced_exited.add(pid)
        elif state == "identity_unknown":
            unknown.add(pid)
        elif state == "remaining":
            remaining.add(pid)
        else:
            failed.add(pid)
    result = OfficeCleanupResult(
        frozenset(exited), frozenset(remaining), frozenset(unknown), frozenset(failed),
        frozenset(forced_exited),
    )
    if forced_exited:
        try:
            from knowmate.secure.office_resiliency import clear_resiliency_markers
            for exe in {owned[pid].exe for pid in forced_exited}:
                clear_resiliency_markers(exe)
        except Exception as exc:
            logger.warning("Office Resiliency 표식 정리 실패: error_type=%s HRESULT=%s", type(exc).__name__, outer_hresult(exc))
    return result


def terminate_owned_office_processes(owned) -> OfficeCleanupResult:
    """생성 identity까지 일치하는 검증 소유 Office만 강제 종료한다.

    quit_com_apps에서 Quit이 실패해 남은 좀비 프로세스를 정리한다. PID 재활용
    위험을 피하려 '지금 그 PID가 Office 실행 파일'인 경우에만 종료한다
    (다른 프로세스에 재할당된 PID를 실수로 죽이지 않도록)."""
    if not isinstance(owned, dict):
        # 구 API가 넘긴 단순 PID set은 creation identity가 없으므로 안전상 종료하지
        # 않는다. 현행 quit_com_apps는 begin_owned_cleanup() 사본을 사용한다.
        return OfficeCleanupResult(failed=frozenset(owned or ()))
    return cleanup_owned_processes(owned)


def _terminate_and_confirm(pid: int, record: OwnedOfficeProcess, timeout_sec: float) -> str:
    """Verify, terminate, and wait using one process handle; return a safe outcome."""
    if record.creation_identity is None:
        logger.warning("생성 identity 없는 Office 종료 거부: PID=%d exe=%s", pid, record.exe)
        return "identity_unknown"
    handle = None
    kernel32 = None
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_TERMINATE = 0x0001
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid,
        )
        if not handle:
            logger.warning(
                "Office 프로세스 핸들 열기 실패: PID=%d exe=%s error=%d",
                pid, record.exe, ctypes.get_last_error(),
            )
            return "identity_unknown"
        identity = _process_creation_identity_from_handle(kernel32, handle)
        if identity is None:
            logger.warning(
                "Office 생성 identity 확인 실패: PID=%d exe=%s error=%d",
                pid, record.exe, ctypes.get_last_error(),
            )
            return "identity_unknown"
        if identity != record.creation_identity:
            return "identity_changed"
        if not kernel32.TerminateProcess(handle, 1):
            error = ctypes.get_last_error()
            immediate = kernel32.WaitForSingleObject(handle, 0)
            if immediate == _WAIT_OBJECT_0:
                logger.info("Office 종료 요청 전 실제 종료 확인: PID=%d exe=%s", pid, record.exe)
                return "exited"
            if immediate == 0xFFFFFFFF:
                logger.warning(
                    "Office 종료 요청·상태 확인 실패: PID=%d exe=%s error=%d wait_error=%d",
                    pid, record.exe, error, ctypes.get_last_error(),
                )
                return "failed"
            logger.warning("Office 종료 요청 실패: PID=%d exe=%s error=%d", pid, record.exe, error)
            return "failed"
        wait_ms = max(1, int(max(0.0, timeout_sec) * 1000))
        wait_result = kernel32.WaitForSingleObject(handle, wait_ms)
        if wait_result == 0x00000102:  # WAIT_TIMEOUT
            logger.warning("Office 종료 확인 시간 초과: PID=%d exe=%s", pid, record.exe)
            return "remaining"
        if wait_result == 0xFFFFFFFF:  # WAIT_FAILED
            logger.warning(
                "Office 종료 대기 실패: PID=%d exe=%s error=%d",
                pid, record.exe, ctypes.get_last_error(),
            )
            return "failed"
        if wait_result != _WAIT_OBJECT_0:
            logger.warning("Office 종료 대기 예기치 않은 결과: PID=%d exe=%s result=%d", pid, record.exe, wait_result)
            return "failed"
        logger.info("Office 실제 종료 확인: PID=%d exe=%s", pid, record.exe)
        return "terminated"
    except Exception as exc:
        logger.warning("Office 종료 확인 실패: PID=%d exe=%s error_type=%s", pid, record.exe, type(exc).__name__)
        return "failed"
    finally:
        if handle and kernel32 is not None:
            kernel32.CloseHandle(handle)
