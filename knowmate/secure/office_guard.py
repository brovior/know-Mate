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
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

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
_cache: dict[str, object] = {"ts": 0.0, "procs": None}  # procs: list[(name, pid)] | None

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
_ownership_generation = 0
_shutdown_generation = 0
_cleanup_retry_at: dict[str, float] = {}
_cleanup_inflight: dict[str, int] = {}


@dataclass(frozen=True)
class OfficeCleanupResult:
    """Outcome of a single verified process cleanup attempt."""

    confirmed_exited: frozenset[int] = frozenset()
    remaining: frozenset[int] = frozenset()
    identity_unknown: frozenset[int] = frozenset()
    failed: frozenset[int] = frozenset()
    forced_exited: frozenset[int] = frozenset()

    @property
    def successful(self) -> bool:
        """Whether all target processes are confirmed gone or safely reused."""
        return not (self.remaining or self.identity_unknown or self.failed)


class OfficeCleanupPendingError(RuntimeError):
    """An owned Office process is still shutting down, so COM must be deferred."""

    def __init__(self, exe: str, detail: str) -> None:
        self.exe = exe.upper()
        super().__init__(detail)

# 현재 진행 중인 COM 파싱 작업의 컨텍스트. Dispatch-hang 시에도 baseline 차집합만
# 으로는 사용자 동시 실행과 구별할 수 없으므로, 이 정보는 진단용으로만 보존하며
# 소유권이 검증되지 않은 PID를 종료하지 않는다.
_op_lock = threading.Lock()
_op: dict = {"exe": None, "baseline": frozenset()}


class OfficeBusyError(RuntimeError):
    """대상 Office 앱이 사용자에 의해 실행 중이라 COM 점유를 피해 건너뛸 때 발생한다."""


class OfficeOwnershipProbeError(RuntimeError):
    """HWND 소유권 확인 중 COM 오류가 나 원본 예외를 호출자에게 전달할 때 발생한다."""


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
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return None
        out: list = []
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return out
            while True:
                name = entry.szExeFile.decode("ascii", "ignore").upper()
                out.append((name, int(entry.th32ProcessID)))
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        return out
    except Exception as exc:  # ctypes/권한 등 예외 시 "판단 불가"
        logger.debug("프로세스 열거 실패(무시): %s", exc)
        return None


def _cached_processes():
    """TTL 캐시를 적용해 (name, pid) 목록을 반환한다(가드의 빈번한 호출용)."""
    now = time.monotonic()
    procs = _cache["procs"]
    if procs is not None and (now - float(_cache["ts"])) < _CACHE_TTL_SEC:
        return procs
    procs = _enumerate_processes()
    _cache["procs"] = procs
    _cache["ts"] = now
    return procs


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


def register_owned_pids(pids: set[int]) -> None:
    """우리(COM 자동화)가 띄운 Office 프로세스 PID를 소유로 등록한다."""
    if not pids:
        return
    # 이전 공개 API 호환용. 생성 identity가 없으므로 이 경로의 PID는 절대
    # 강제 종료 대상이 되지 않는다. 새 생성 경로는 register_owned_app만 쓴다.
    with _owned_lock:
        for pid in pids:
            _owned_pids[pid] = OwnedOfficeProcess("", None)
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


def register_owned_app(exe: str, baseline: set[int], app: Any) -> bool:
    """새 COM 앱의 PID/HWND/exe가 모두 확인됐을 때만 소유 등록한다.

    비Windows 또는 프로세스 열거 불가 fake 경로에서는 기존 테스트 호환을 위해
    등록 없이 성공 처리한다. Windows에서 검증할 수 없으면 사용자 앱일 수 있어
    False를 반환하며 호출자는 자동화를 연기해야 한다.
    """
    if sys.platform != "win32":
        return True
    current_procs = _enumerate_processes()
    if current_procs is None:
        return False
    hwnd = None
    hwnd_attr = None
    probe_error: Exception | None = None
    for attr in ("Hwnd", "HWND"):
        try:
            hwnd = getattr(app, attr)
            hwnd_attr = attr
            break
        except AttributeError:
            continue
        except Exception as exc:
            probe_error = exc
            break
    if probe_error is not None:
        raise OfficeOwnershipProbeError("Office HWND 소유권 확인 실패") from probe_error
    if hwnd is None:
        return False
    pid = _pid_from_hwnd(hwnd)
    up = exe.upper()
    current = _pids_for(up, current_procs)
    creation_identity = _process_creation_identity(pid) if pid is not None else None
    if pid is None or pid in baseline or pid not in current or creation_identity is None:
        return False

    # HWND→PID 확인과 생성 identity 조회 사이 원래 프로세스가 끝나 PID가 사용자
    # Office에 재사용될 수 있다. identity 획득 뒤 COM 객체의 HWND, PID, EXE,
    # identity를 한 번 더 확인해 연결이 바뀐 경우 소유 등록을 거부한다. 이후 종료
    # 시점에도 같은 identity를 동일 프로세스 핸들에서 다시 검증한다.
    try:
        confirmed_hwnd = getattr(app, hwnd_attr) if hwnd_attr is not None else None
    except Exception as exc:
        raise OfficeOwnershipProbeError("Office HWND 소유권 재확인 실패") from exc
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
        return False
    global _ownership_generation
    with _owned_lock:
        _ownership_generation += 1
        # PowerPoint MultiUse Dispatch는 동시 사용자 실행과 완전한 소유 증명이
        # 불가능하다. 세션 추적은 해 가드의 자기 감지만 피하되, 어떤 강제 종료
        # 경로에도 넣지 않는다.
        _owned_pids[pid] = OwnedOfficeProcess(
            up, creation_identity, up != "POWERPNT.EXE", _ownership_generation,
        )
    logger.debug("우리 소유 Office PID 등록: %s=%d", up, pid)
    return True


def clear_owned_pids() -> set[int]:
    """테스트/명시적 초기화용으로 소유 기록과 재시도 시각을 비운다."""
    with _owned_lock:
        prev = set(_owned_pids)
        _owned_pids.clear()
        _cleanup_retry_at.clear()
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
        for pid, record in tuple(_owned_pids.items()):
            if record.exe in {"WINWORD.EXE", "EXCEL.EXE"} and (exe is None or record.exe == exe.upper()):
                pending = OwnedOfficeProcess(
                    record.exe, record.creation_identity, record.terminable,
                    record.generation, True, cleanup_generation,
                )
                _owned_pids[pid] = pending
                snapshot[pid] = pending
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


def finish_shutdown_cleanup(exe: str) -> None:
    """Release one shutdown cleanup gate after its OS work has completed."""
    with _owned_lock:
        up = exe.upper()
        count = _cleanup_inflight.get(up, 0)
        if count <= 1:
            _cleanup_inflight.pop(up, None)
        else:
            _cleanup_inflight[up] = count - 1


def _log_cleanup_result(exe: str, result: OfficeCleanupResult, phase: str) -> None:
    """Log cleanup status without including document content or paths."""
    logger.info(
        "[com] Office 정리 결과: exe=%s phase=%s actual_exit=%s remaining=%s identity_unknown=%s failed=%s",
        exe, phase, sorted(result.confirmed_exited), sorted(result.remaining),
        sorted(result.identity_unknown), sorted(result.failed),
    )


def is_office_busy_for_ext(ext: str) -> bool:
    """확장자를 서비스하는 Office 앱이 **사용자에 의해** 실행 중이면 True.

    우리 자동화가 띄운 소유 PID는 제외한다 — 자기 감지로 인한 스킵 방지.
    비Windows·열거 실패·대상 외 확장자·(우리 소유 외) 미실행이면 False.
    """
    proc = process_for_ext(ext)
    if proc is None:
        return False
    procs = _cached_processes()
    if procs is None:  # 판단 불가 → 기존 동작 유지(차단하지 않음)
        return False
    running = _pids_for(proc, procs)
    if sys.platform != "win32":
        return bool(running - _owned_snapshot())
    with _owned_lock:
        records = {pid: record for pid, record in _owned_pids.items() if pid in running}
    proven_owned = {
        pid for pid, record in records.items()
        if record.exe == proc and record.creation_identity is not None
        and _process_creation_identity(pid) == record.creation_identity
    }
    external = running - proven_owned
    return bool(external)


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
    snapshot = begin_owned_cleanup(up)
    if not snapshot:
        return False
    result = cleanup_owned_processes(snapshot, timeout_sec=timeout_sec)
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


def cleanup_owned_processes(owned: dict[int, OwnedOfficeProcess], timeout_sec: float = 2.0) -> OfficeCleanupResult:
    """Run verified process cleanup behind EXE gates until all OS work finishes."""
    exes = {record.exe for record in owned.values() if record.exe in {"WINWORD.EXE", "EXCEL.EXE"}}
    for exe in exes:
        begin_shutdown_cleanup(exe)
    try:
        return _cleanup_owned_processes_impl(owned, timeout_sec)
    finally:
        for exe in exes:
            finish_shutdown_cleanup(exe)


def _cleanup_owned_processes_impl(
    owned: dict[int, OwnedOfficeProcess], timeout_sec: float,
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
        state = _terminate_and_confirm(pid, record, timeout_sec)
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
            logger.warning("Office Resiliency 표식 정리 실패: %s", exc)
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
