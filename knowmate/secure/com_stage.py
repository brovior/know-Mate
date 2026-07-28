"""COM 추출 진행 단계 계측 — "어느 단계에서 멈췄는지"를 로그 한 줄로 알 수 있게 한다.

COM(Excel/Word/PowerPoint) 파싱이 멈추면(hang) 워치독이 프로세스를 강제 종료해야만
풀리는데, 지금까지는 그게 `Workbooks.Open()`에서 멈춘 건지 셀 순회에서 멈춘 건지
구분할 방법이 없었다. 원인에 따라 대응이 완전히 달라지므로(파일 사전 판별 vs 벌크
읽기 교체) 이 구분이 다음 결정의 전제다.

**스레드 경합 주의**: 워커 스레드가 단계를 갱신하는 동안, 워치독의 daemon 타이머
스레드(별도 스레드)가 `snapshot()`/`describe()`로 "지금 어느 단계에 몇 초째 멈춰
있는지"를 읽어 타임아웃 로그에 남긴다. 그래서 모듈 레벨 슬롯을 `threading.Lock`으로
보호한다 — `secure/office_guard.py`의 `_op`(프로세스 소유권 추적, 책임이 다름)와는
별개의 상태다.

COM/win32를 import하지 않는 순수 파이썬이라 `collector/com_watchdog.py`가 직접
import해도 CLAUDE.md 원칙3(보안·Office 의존 코드는 secure/ 안에 격리)을 어기지 않는다
(이 모듈 자체가 COM에 의존하지 않으므로).

로그에는 **경로·단계명·소요시간·건수만** 남긴다 — 셀 값·문서 본문은 절대 남기지 않는다
(CLAUDE.md 원칙7).
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 단계 이름 — 파일 종류에 따라 일부만 쓰인다(Excel: SHEETS/CELL_READ, Word/PPT: READ).
STAGE_DISPATCH = "dispatch"    # Excel/Word/PPT 실행(COM 서버 기동, 이미 떠 있으면 그 인스턴스에 붙음)
STAGE_OPEN = "open"            # 파일 오픈 시작~완료
STAGE_SHEETS = "sheets"        # (Excel) 시트 목록 조회
STAGE_CELL_READ = "cell_read"  # (Excel) 셀 데이터 읽기
STAGE_READ = "read"            # (Word/PPT) 본문 읽기 — 시트/셀 구분이 없는 포맷
STAGE_CLOSE = "close"          # 문서 닫기

_lock = threading.Lock()
# 현재 진행 중인 단계(워커 스레드가 쓰고, 워치독 타이머 스레드가 읽는다).
_current: dict = {"stage": None, "path": None, "started_at": None}
# 가장 최근 파싱에서 예외가 난 단계 이름 — 실패 분류(failure_state.classify)가
# "어느 단계에서 실패했는지"를 알아야 하는데, StageTimer는 파싱 1건마다 새로
# 만들어져 호출부(스케줄러)가 직접 참조할 수 없다. 여기 게시해 두고
# take_last_failed_stage()로 읽고 비운다(다음 파일로 값이 새는 것 방지).
_last_failed_stage: str | None = None


def begin(stage: str, path: str) -> None:
    """새 단계 진입을 기록한다. 이전 단계 정보는 덮어쓴다(단계는 순차 진행이므로)."""
    with _lock:
        _current["stage"] = stage
        _current["path"] = path
        _current["started_at"] = time.monotonic()


def clear() -> None:
    """파싱 1건이 끝나면(성공이든 실패든) 반드시 호출해 상태를 비운다.

    비우지 않으면 다음 파일 처리 전까지 "이전 파일이 아직 멈춰 있다"는 오정보가
    워치독 로그에 남을 수 있다.
    """
    with _lock:
        _current["stage"] = None
        _current["path"] = None
        _current["started_at"] = None


def snapshot() -> tuple[str | None, str | None, float]:
    """(현재 단계, 경로, 그 단계에 머문 시간(초))를 반환한다. 진행 중이 아니면 (None, None, 0.0)."""
    with _lock:
        stage = _current["stage"]
        path = _current["path"]
        started_at = _current["started_at"]
    if stage is None or started_at is None:
        return None, None, 0.0
    return stage, path, time.monotonic() - started_at


def take_last_failed_stage() -> str | None:
    """직전 파싱에서 예외가 난 단계 이름을 반환하고 슬롯을 비운다.

    실패가 없었으면(정상 완료 또는 아직 아무것도 안 했으면) None. 스케줄러가
    파일 1건의 예외 핸들러에서 호출해 실패 분류(failure_state.classify)에
    넘긴다 — 읽고 나면 즉시 비워야 다음 파일의 실패로 착각하지 않는다.
    """
    global _last_failed_stage
    with _lock:
        stage = _last_failed_stage
        _last_failed_stage = None
    return stage


def current_stage_name() -> str:
    """현재 단계 **이름만** 반환한다("open" 등). 진행 중이 아니면 "unknown".

    사이클 요약이 "타임아웃이 어느 단계에서 몇 건 났는지" 집계(`Counter`)할 때
    쓴다 — `describe()`는 경로·시간이 섞여 있어 매번 다른 문자열이라 집계에
    부적합하다.
    """
    stage, _path, _elapsed = snapshot()
    return stage or "unknown"


def describe() -> str:
    """워치독 타임아웃 로그에 그대로 붙일 수 있는 한 줄 요약을 반환한다.

    예: "open(74.0s) C:/docs/x.xlsx" — 경로·단계명·소요시간만 포함, 셀 값 없음.
    진행 중인 단계가 없으면(예: 이미 clear()된 뒤) "단계 정보 없음"을 반환한다.
    """
    stage, path, elapsed = snapshot()
    if stage is None:
        return "단계 정보 없음"
    return f"{stage}({elapsed:.1f}s) {path}"


class StageTimer:
    """파일 1건 파싱 동안의 단계별 소요시간을 기록한다(파싱 1회당 1개 생성)."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._durations: dict[str, float] = {}
        self.failed_stage: str | None = None

    def stage(self, name: str):
        """단계 진입~이탈을 계측하는 컨텍스트 매니저.

        진입 시 모듈 레벨 슬롯에 (단계, 경로)를 게시해 워치독이 읽을 수 있게 하고,
        이탈 시 소요시간을 기록한다. 블록 안에서 예외가 나면 `failed_stage`에 이
        단계 이름을 남기고(이미 기록돼 있으면 덮어쓰지 않음 — 최초 실패 단계만
        의미 있음) 예외를 그대로 전파한다.
        """
        return _StageContext(self, name)

    def _record(self, name: str, elapsed: float, exc: BaseException | None) -> None:
        self._durations[name] = elapsed
        if exc is not None and self.failed_stage is None:
            self.failed_stage = name
            global _last_failed_stage
            with _lock:
                _last_failed_stage = name

    def summary(self) -> str:
        """단계별 소요시간 + 실패 단계를 로그 한 줄로 요약한다. 셀 값·본문 없음."""
        parts = " ".join(f"{name}={dur:.2f}s" for name, dur in self._durations.items())
        if self.failed_stage:
            return f"{parts} (실패단계={self.failed_stage})"
        return parts

    def log_summary(self, level: int = logging.DEBUG) -> None:
        logger.log(level, "[com] 단계별 소요: %s <%s>", self.summary(), self._path)


class _StageContext:
    """`StageTimer.stage()`가 반환하는 컨텍스트 매니저 본체."""

    def __init__(self, timer: StageTimer, name: str) -> None:
        self._timer = timer
        self._name = name
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.monotonic()
        begin(self._name, self._timer._path)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = time.monotonic() - self._t0
        self._timer._record(self._name, elapsed, exc)
        return False  # 예외는 항상 그대로 전파
