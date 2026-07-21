"""사용자가 직접 열어둔 Office 인스턴스 감지 (COM 점유 충돌 예방).

COM 자동화는 대상 Office 프로세스가 이미 떠 있으면 그 인스턴스에 붙는다.
Office는 사용자당 하나의 인스턴스만 실행되는 구조라, 백그라운드 인덱싱이
`Dispatch("Word.Application")`을 호출하면 사용자가 열어둔 창을 그대로 점유한다.
이 상태에서 저장 확인 등 모달 대기가 걸리면 Office가 응답 없음이 될 수 있다.

**우리 자신 vs 사용자 구분(핵심)**: 인덱싱이 DRM/구형 문서를 읽으려고 COM으로
직접 띄운 Office도 같은 실행 파일(WINWORD.EXE 등)이라, 단순히 "프로세스가
있나?"로 판정하면 *우리가 띄운 인스턴스를 우리가 다시 점유로 오판*해 뒷부분
문서를 전부 스킵하는 자기 감지 버그가 생긴다. 이를 막기 위해:
  - `com_reader`가 COM으로 Office를 띄울 때 그 PID를 `register_owned_pids`로
    "우리 소유"로 등록한다(스레드별).
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

# 우리 자동화(COM Dispatch)가 띄운 Office 프로세스 PID (스레드별).
# COM은 스레드별로 쓰이므로(_tls) 소유 PID도 스레드별로 관리한다.
_tls = threading.local()


class OfficeBusyError(RuntimeError):
    """대상 Office 앱이 사용자에 의해 실행 중이라 COM 점유를 피해 건너뛸 때 발생한다."""


def process_for_ext(ext: str) -> str | None:
    """확장자를 서비스하는 Office 실행 파일명을 반환한다. 대상 아니면 None."""
    return _EXT_TO_PROCESS.get(ext.lower())


def _owned() -> set:
    """현재 스레드의 '우리 소유' Office PID 집합을 반환한다(없으면 생성)."""
    s = getattr(_tls, "owned", None)
    if s is None:
        s = set()
        _tls.owned = s
    return s


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
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.windll.kernel32
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


def register_owned_pids(pids: set) -> None:
    """우리(COM 자동화)가 띄운 Office 프로세스 PID를 소유로 등록한다."""
    if not pids:
        return
    _owned().update(pids)
    logger.debug("우리 소유 Office PID 등록: %s", sorted(pids))


def clear_owned_pids() -> set:
    """현재 스레드의 소유 PID 집합을 반환하고 비운다(사이클 종료 정리용)."""
    owned = _owned()
    prev = set(owned)
    owned.clear()
    return prev


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
    external = running - _owned()  # 우리가 띄운 인스턴스 제외
    return bool(external)


def terminate_owned_office_processes(owned: set) -> None:
    """owned PID 중 아직 살아있고 여전히 Office 실행 파일인 것만 강제 종료한다.

    quit_com_apps에서 Quit이 실패해 남은 좀비 프로세스를 정리한다. PID 재활용
    위험을 피하려 '지금 그 PID가 Office 실행 파일'인 경우에만 종료한다
    (다른 프로세스에 재할당된 PID를 실수로 죽이지 않도록)."""
    if not owned or sys.platform != "win32":
        return
    procs = _enumerate_processes()
    if procs is None:
        return
    alive_office = {pid for (name, pid) in procs if pid in owned and name in _OFFICE_EXES}
    for pid in alive_office:
        _terminate_pid(pid)


def _terminate_pid(pid: int) -> None:
    """PID를 강제 종료한다(TerminateProcess). 실패는 무시."""
    try:
        import ctypes

        PROCESS_TERMINATE = 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return
        try:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            logger.info("잔존 Office 프로세스 강제 종료(좀비 정리): PID=%d", pid)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception as exc:
        logger.debug("프로세스 종료 실패(무시) PID=%d: %s", pid, exc)
