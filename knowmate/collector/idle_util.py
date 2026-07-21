"""OS 유휴 시간 조회 (읽기 전용) — 시스템 전역 마지막 입력 이후 경과 시간.

트레이 상주 앱은 창이 비활성·최소화된 상태에서도 유휴를 감지해야 하므로,
Qt 이벤트 필터(창에 포커스가 있어야 동작)가 아니라 Windows의 시스템 전역
API(GetLastInputInfo)를 사용한다.

주의: 이 모듈은 유휴 시간을 **읽기만** 한다. 절대 마우스·키보드 입력을
발생시키거나 시스템 유휴 타이머를 리셋하지 않는다 — 사내 DRM 등이 사용하는
것과 동일한 신호를 관찰만 할 뿐, 그 보안 타이머를 우회·연장하는 목적으로
쓰지 않는다.
"""
import sys


def _query_last_input_tick() -> int | None:
    """GetLastInputInfo()의 dwTime(마지막 입력 시각, ms 단위 tick)을 반환한다.

    실패 시(비Windows·API 오류 등) None을 반환한다.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return info.dwTime
    except Exception:
        return None


def _tick_count() -> int | None:
    """GetTickCount()(부팅 이후 경과 ms)를 반환한다. 실패 시 None."""
    try:
        import ctypes
        return ctypes.windll.kernel32.GetTickCount()
    except Exception:
        return None


def get_idle_seconds() -> float:
    """마지막 사용자 입력(마우스/키보드) 이후 경과 시간(초)을 반환한다.

    Windows 전용. 비Windows·조회 실패 시 0.0을 반환한다 — "방금 활동함"으로
    간주해 유휴 트리거가 오발동하지 않게 하는 안전한 기본값이다.
    """
    if sys.platform != "win32":
        return 0.0

    last_input_tick = _query_last_input_tick()
    now_tick = _tick_count()
    if last_input_tick is None or now_tick is None:
        return 0.0

    elapsed_ms = now_tick - last_input_tick
    if elapsed_ms < 0:
        # GetTickCount는 DWORD라 약 49.7일마다 0으로 랩어라운드한다.
        # 랩어라운드 직후 순간엔 음수가 나올 수 있으므로 "방금 활동함"으로 간주.
        return 0.0
    return elapsed_ms / 1000.0
