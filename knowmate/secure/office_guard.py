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

# 우리 자동화(COM Dispatch)가 띄운 Office 프로세스 PID.
# 워치독(별도 스레드)이 행오버 시 이 목록을 읽어 강제 종료해야 하므로 모듈
# 레벨 + 락으로 관리한다(COM 자체는 단일 워커 스레드에서만 쓰임).
_owned_lock = threading.Lock()
_owned_pids: set = set()

# 현재 진행 중인 COM 파싱 작업의 컨텍스트(워치독의 Dispatch-hang 대비).
# begin_com_op이 대상 exe와 '시작 전 존재하던 그 exe PID'를 기록해, 소유로
# 등록되기 전(Dispatch가 반환 전) 멈춘 경우에도 '작업 시작 후 새로 뜬' 프로세스를
# 우리 것으로 보고 종료할 수 있게 한다.
_op_lock = threading.Lock()
_op: dict = {"exe": None, "baseline": frozenset()}


class OfficeBusyError(RuntimeError):
    """대상 Office 앱이 사용자에 의해 실행 중이라 COM 점유를 피해 건너뛸 때 발생한다."""


def process_for_ext(ext: str) -> str | None:
    """확장자를 서비스하는 Office 실행 파일명을 반환한다. 대상 아니면 None."""
    return _EXT_TO_PROCESS.get(ext.lower())


def _owned_snapshot() -> set:
    """소유 PID 집합의 사본을 반환한다(락 보호)."""
    with _owned_lock:
        return set(_owned_pids)


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
    with _owned_lock:
        _owned_pids.update(pids)
    logger.debug("우리 소유 Office PID 등록: %s", sorted(pids))


def clear_owned_pids() -> set:
    """소유 PID 집합을 반환하고 비운다(사이클 종료 정리용)."""
    with _owned_lock:
        prev = set(_owned_pids)
        _owned_pids.clear()
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
    external = running - _owned_snapshot()  # 우리가 띄운 인스턴스 제외
    return bool(external)


def begin_com_op(exe: str) -> None:
    """COM 파싱 작업 시작을 기록한다(워치독의 Dispatch-hang 대비).

    시작 전 존재하던 해당 exe PID를 baseline으로 저장 — 이후 새로 뜬(=우리)
    프로세스를 소유 등록 전이라도 워치독이 종료할 수 있게 한다.
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


def terminate_stuck_office(exe: str) -> int:
    """워치독 발동 — 블로킹된 COM 호출을 풀기 위해 해당 exe의 '우리' 프로세스를 종료.

    우선 우리 소유 PID를 대상으로 하고, 없으면(Dispatch가 반환 전 멈춰 소유 등록이
    안 된 경우) begin_com_op 이후 새로 뜬 PID를 대상으로 한다. PID 재활용을 피하려
    '지금도 그 exe인' 프로세스만 종료한다. 반환: 종료 시도한 프로세스 수.
    """
    if not exe or sys.platform != "win32":
        return 0
    procs = _enumerate_processes()
    if procs is None:
        return 0
    up = exe.upper()
    current = {pid for (name, pid) in procs if name == up}
    targets = _owned_snapshot() & current
    if not targets:
        # 소유 등록 전 Dispatch-hang 의심 → 작업 시작 후 새로 뜬 그 exe만 종료
        with _op_lock:
            baseline = _op["baseline"] if _op["exe"] == up else frozenset(current)
        targets = current - baseline
    for pid in targets:
        _terminate_pid(pid)
    if targets:
        logger.warning(
            "COM 행오버 추정 — %s 프로세스 %d개 강제 종료(블로킹 해제): %s",
            up, len(targets), sorted(targets),
        )
        # 강제 종료는 Office에 "비정상 종료" 표식을 남기고, 그 표식은 다음 기동 때
        # 세이프모드 프롬프트 → 또 행오버 → 또 강제 종료의 루프를 만든다. 방금
        # 우리가 만든 표식이므로 여기서 바로 지운다(다음 Dispatch 직전에도 한 번 더
        # 지우지만, 그 사이 사용자가 Office를 열면 프롬프트를 보게 되므로 즉시 정리).
        # 이 함수는 워치독 daemon 타이머에서 호출되므로 어떤 예외도 밖으로 내보내지
        # 않는다 — 여기서 터지면 타이머 스레드가 조용히 죽는다.
        try:
            from knowmate.secure.office_resiliency import clear_resiliency_markers
            clear_resiliency_markers(up)
        except Exception as exc:
            logger.debug("Resiliency 표식 정리 실패(무시): %s", exc)
    return len(targets)


_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000


def wait_for_owned_exit(owned: set, timeout_sec: float) -> tuple[set, float]:
    """owned PID들이 스스로 종료하기를 최대 timeout_sec초 기다린다.

    `Quit()`은 종료를 "요청"할 뿐 즉시 반환하므로, 반환 직후 프로세스 목록을
    조회하면 아직 정리 중인(임시파일·애드인 정리 등) Office가 거의 항상 살아있는
    것으로 잡혀 불필요하게 강제 종료된다(레이스). `OpenProcess`+`WaitForSingleObject`로
    커널이 실제 종료를 알려줄 때까지 대기해, 스스로 꺼지면 유예 시간을 다 쓰지
    않고 즉시 반환한다.

    한 PID라도 이 시점에 `OpenProcess`가 실패하면(권한 문제 등 드문 경우 제외,
    보통은 **이미 종료됨을 의미**) 폴링으로 5초를 기다리지 않는다 — 대신 프로세스
    열거를 1회만 호출해 정말 살아있는지 확인한다(열거 자체가 실패하면 판단 불가로
    보수적으로 "살아있음" 취급 — 기존 즉시 강제종료 동작과 동일하게 안전한 방향).

    반환: (유예 종료 후에도 남아있는 PID 집합, 실제 대기한 시간(초)) — 후자는
    로그로 남겨 "레이스였는지(빠르게 종료) vs 다른 원인인지(매번 유예 소진)"를
    운영 중 구분할 수 있게 한다.
    """
    if not owned or sys.platform != "win32":
        return set(owned), 0.0

    import ctypes

    kernel32 = ctypes.windll.kernel32
    t0 = time.monotonic()

    handles: dict[int, int] = {}
    no_handle: set = set()
    for pid in owned:
        handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
        if handle:
            handles[pid] = handle
        else:
            no_handle.add(pid)

    deadline = t0 + timeout_sec
    unresolved: set = set()
    for pid, handle in handles.items():
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        try:
            result = kernel32.WaitForSingleObject(handle, remaining_ms)
            if result != _WAIT_OBJECT_0:
                unresolved.add(pid)
        finally:
            kernel32.CloseHandle(handle)

    still_alive = set(unresolved)
    if no_handle:
        procs = _enumerate_processes()
        if procs is None:
            # 판단 불가 → 기존 즉시 강제종료 동작과 동일하게 보수적으로 취급
            still_alive |= no_handle
        else:
            still_alive |= {pid for (name, pid) in procs if pid in no_handle and name in _OFFICE_EXES}

    return still_alive, time.monotonic() - t0


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
