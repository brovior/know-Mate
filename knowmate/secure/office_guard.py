"""사용자가 직접 열어둔 Office 인스턴스 감지 (COM 점유 충돌 예방).

COM 자동화는 대상 Office 프로세스가 이미 떠 있으면 그 인스턴스에 붙는다.
Office는 사용자당 하나의 인스턴스만 실행되는 구조라, 백그라운드 인덱싱이
`Dispatch("Word.Application")`을 호출하면 사용자가 열어둔 창을 그대로 점유한다.
이 상태에서 저장 확인 등 모달 대기가 걸리면 Office가 응답 없음이 될 수 있다.

이 모듈은 **프로세스 열거만** 수행하고 COM 객체는 생성·연결하지 않는다
(사용자 창에 전혀 손대지 않는다). 대상 앱이 실행 중이면 그 확장자를 이번
인덱싱 사이클에서 건너뛰고(OfficeBusyError), 다음 사이클에서 재시도한다.

Windows 전용. 비Windows(사외 테스트)에서는 항상 "실행 중 아님"으로 판단해
COM 라우팅 로직에 영향을 주지 않는다.

CLAUDE.md 원칙3(보안·Office 의존 코드는 secure/ 안에 격리) 준수.
"""
import logging
import sys
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

# 프로세스 목록 캐시 (한 사이클에서 수천 건을 열거하지 않도록 짧은 TTL)
_CACHE_TTL_SEC = 2.0
_cache: dict[str, object] = {"ts": 0.0, "names": None}


class OfficeBusyError(RuntimeError):
    """대상 Office 앱이 사용자에 의해 실행 중이라 COM 점유를 피해 건너뛸 때 발생한다."""


def process_for_ext(ext: str) -> str | None:
    """확장자를 서비스하는 Office 실행 파일명을 반환한다. 대상 아니면 None."""
    return _EXT_TO_PROCESS.get(ext.lower())


def _snapshot_process_names() -> set[str] | None:
    """현재 실행 중인 프로세스 실행 파일명 집합을 반환한다(대문자).

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
        names: set[str] = set()
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return names
            while True:
                names.add(entry.szExeFile.decode("ascii", "ignore").upper())
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        return names
    except Exception as exc:  # ctypes/권한 등 예외 시 "판단 불가"
        logger.debug("프로세스 열거 실패(무시): %s", exc)
        return None


def _running_process_names() -> set[str] | None:
    """TTL 캐시를 적용해 실행 중 프로세스명 집합을 반환한다."""
    now = time.monotonic()
    if _cache["names"] is not None and (now - float(_cache["ts"])) < _CACHE_TTL_SEC:
        return _cache["names"]  # type: ignore[return-value]
    names = _snapshot_process_names()
    _cache["names"] = names
    _cache["ts"] = now
    return names


def is_office_busy_for_ext(ext: str) -> bool:
    """확장자를 서비스하는 Office 앱이 현재 실행 중이면 True.

    비Windows·열거 실패·대상 외 확장자·미실행이면 False(= 차단하지 않음).
    """
    proc = process_for_ext(ext)
    if proc is None:
        return False
    names = _running_process_names()
    if names is None:  # 판단 불가 → 기존 동작 유지(차단하지 않음)
        return False
    return proc in names
