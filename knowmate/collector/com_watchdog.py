"""COM 추출 행오버 방지 워치독 (PyQt6 비의존 — 사외 단위 테스트 가능).

동기 COM 호출(Excel/Word 열기·셀 순회)이 멈추면 그 호출을 한 스레드가 COM 안에
갇힌다 — 같은 스레드에서 타임아웃을 걸 수 없고, 유일한 해제 방법은 그 호출을
서비스하는 Office 프로세스를 종료하는 것이다(프로세스가 죽으면 호출이 RPC 오류로
반환되며 스레드가 풀린다).

`arm(exe, timeout)`으로 무장하면 별도 daemon 타이머가 timeout 후 `terminate_fn(exe)`
로 해당 Office 프로세스를 강제 종료한다. `disarm()`으로 정상 완료 시 해제한다.

경합 방지(핵심):
- **세대 토큰**: 파일마다 세대를 올려, 이미 끝난 파일의 타이머가 뒤늦게 발화해
  *다음 파일이 쓰는* Office를 죽이는 오사살을 막는다.
- **락 + active 플래그**: disarm과 타이머 발화가 겹쳐도 한쪽만 유효하게 한다.
- **daemon 타이머**: 비데몬 스레드가 살아 프로세스 종료(트레이 [종료])를 막지 않게 한다.
"""
import logging
import threading

logger = logging.getLogger(__name__)


class ComWatchdog:
    """COM 추출 호출을 감싸 행오버 시 대상 Office 프로세스를 종료하는 워치독."""

    def __init__(self, terminate_fn, timer_factory=None):
        """워치독을 초기화한다.

        terminate_fn: (exe: str) -> int, 해당 exe의 우리 Office 프로세스를 종료하고
            종료 수를 반환하는 콜백(기본 사용 시 office_guard.terminate_stuck_office).
        timer_factory: (interval_sec, callback) -> timer, 테스트 주입용.
            기본은 daemon threading.Timer.
        """
        self._terminate_fn = terminate_fn
        self._timer_factory = timer_factory or self._default_timer
        self._lock = threading.Lock()
        self._gen = 0
        self._active = False
        self._timer = None
        self.timeout_count = 0  # 워치독이 실제로 프로세스를 종료한 횟수

    @staticmethod
    def _default_timer(interval, callback):
        t = threading.Timer(interval, callback)
        t.daemon = True  # 프로세스 종료를 막지 않도록 필수
        return t

    def arm(self, exe: str, timeout_sec: float) -> None:
        """exe 대상으로 timeout_sec 후 발화하도록 무장한다(이전 무장은 대체)."""
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._active = True
            timer = self._timer_factory(timeout_sec, lambda: self._fire(exe, gen))
            self._timer = timer
        timer.start()

    def _fire(self, exe: str, gen: int) -> int:
        """타이머 발화 콜백 — 현재 세대·활성 상태일 때만 종료를 수행한다."""
        with self._lock:
            if not self._active or gen != self._gen:
                return 0  # 이미 해제됐거나 다른(과거) 세대 → 무시
            killed = self._terminate_fn(exe)
            if killed:
                self.timeout_count += 1
                logger.warning("COM 추출 타임아웃 — %s 강제 해제(%d개 종료)", exe, killed)
            # 종료 후에는 비활성화(같은 파일에 중복 발화 방지)
            self._active = False
            return killed

    def disarm(self) -> None:
        """정상 완료 시 워치독을 해제한다."""
        with self._lock:
            self._active = False
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()
