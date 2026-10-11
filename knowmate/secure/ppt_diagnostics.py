"""PowerPoint 읽기 위치·개수·시간만 기록하며 COM 객체는 보관하지 않는다."""
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypeVar

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_PROGRESS_INTERVAL_SEC = 10.0
_SLOW_SHAPE_SEC = 1.0


@dataclass
class _SlideStats:
    started: float
    shapes: int = 0
    top_shapes: int = 0
    groups: int = 0
    tables: int = 0
    cells: int = 0
    text_frames: int = 0
    max_depth: int = 0
    errors: int = 0
    calls: Counter[str] = field(default_factory=Counter)
    seconds: Counter[str] = field(default_factory=Counter)


class PptReadDiagnostics:
    """생성 워커에서 계측하고 별도 스레드에는 수치 위치 정보만 전달한다."""

    def __init__(self, path: str) -> None:
        """파일 한 건의 읽기 계측을 준비한다."""
        self._path = path
        self._stats: dict[int, _SlideStats] = {}
        self._lock = threading.Lock()
        self._current: tuple | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._monitor, daemon=True, name="ppt-read-progress")

    def __enter__(self) -> "PptReadDiagnostics":
        """COM을 호출하지 않는 진행 로그 스레드를 시작한다."""
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """정상·오류 종료 모두 진행 로그를 중지한다."""
        self._stop.set()
        self._thread.join(timeout=0.2)
        with self._lock:
            self._current = None
        calls: Counter[str] = Counter()
        seconds: Counter[str] = Counter()
        for stats in self._stats.values():
            calls.update(stats.calls)
            seconds.update(stats.seconds)
        operations = " ".join(f"{name}:{calls[name]}/{seconds[name]:.2f}s" for name in sorted(calls))
        logger.info("[ppt] 읽기 구간 합계: operations=[%s] <%s>", operations, self._path)

    def _slide(self, slide: int) -> _SlideStats:
        if slide not in self._stats:
            self._stats[slide] = _SlideStats(time.monotonic())
        return self._stats[slide]

    def read(
        self, operation: str, action: Callable[[], _T], slide: int,
        shape: tuple[int, ...] = (), row: int = 0, column: int = 0,
    ) -> _T:
        """기존 읽기 한 구간의 실행 위치·시간과 오류 건수를 기록한다."""
        stats = self._slide(slide)
        started = time.monotonic()
        progress = (slide, shape, operation, row, column, started)
        with self._lock:
            self._current = progress
        try:
            return action()
        except StopIteration:
            raise
        except Exception as exc:
            stats.errors += 1
            logger.warning(
                "[ppt] 읽기 오류: slide=%d shape=%s operation=%s row=%d column=%d error_type=%s <%s>",
                slide, self._position(shape), operation, row, column, type(exc).__name__, self._path,
            )
            raise
        finally:
            stats.calls[operation] += 1
            stats.seconds[operation] += time.monotonic() - started
            with self._lock:
                self._current = None

    def items(
        self, collection: Any, kind: str, slide: int, shape: tuple[int, ...] = (),
    ) -> Iterator[tuple[int, Any]]:
        """순회에 필요한 기존 iterator·next 호출도 계측한다."""
        iterator = self.read(kind + "_iter", lambda: iter(collection), slide, shape)
        index = 1
        while True:
            next_slide = index if kind == "slides" else slide
            next_shape = () if kind == "slides" else shape + (index,)
            try:
                item = self.read(kind + "_next", lambda: next(iterator), next_slide, next_shape)
            except StopIteration:
                return
            yield index, item
            index += 1

    def shape_started(self, slide: int, shape: tuple[int, ...]) -> None:
        """실제로 방문한 도형과 그룹 깊이를 센다."""
        stats = self._slide(slide)
        stats.shapes += 1
        stats.top_shapes += int(len(shape) == 1)
        stats.max_depth = max(stats.max_depth, len(shape) - 1)

    def count(self, slide: int, kind: str, amount: int = 1) -> None:
        """이미 확인한 그룹·표·셀·텍스트프레임 개수를 추가한다."""
        stats = self._slide(slide)
        setattr(stats, kind, getattr(stats, kind) + amount)

    def shape_finished(
        self, slide: int, shape: tuple[int, ...], kind: str, started: float,
        rows: int = 0, columns: int = 0, shape_type: int = -1,
    ) -> None:
        """느린 도형은 INFO, 나머지는 DEBUG에 수치만 남긴다."""
        elapsed = time.monotonic() - started
        logger.log(
            logging.INFO if elapsed >= _SLOW_SHAPE_SEC else logging.DEBUG,
            "[ppt] 도형 읽기: slide=%d shape=%s kind=%s type=%d depth=%d rows=%d columns=%d seconds=%.2f <%s>",
            slide, self._position(shape), kind, shape_type, len(shape) - 1, rows, columns, elapsed, self._path,
        )

    def slide_finished(self, slide: int) -> float:
        """슬라이드별 건수·구간 합계를 기록하고 읽기 경과시간을 반환한다."""
        stats = self._slide(slide)
        elapsed = time.monotonic() - stats.started
        operations = " ".join(
            f"{name}:{stats.calls[name]}/{stats.seconds[name]:.2f}s" for name in sorted(stats.calls)
        )
        logger.info(
            "[ppt] 슬라이드 읽기: slide=%d shapes=%d top_shapes=%d groups=%d tables=%d cells=%d "
            "text_frames=%d max_group_depth=%d errors=%d seconds=%.2f operations=[%s] <%s>",
            slide, stats.shapes, stats.top_shapes, stats.groups, stats.tables, stats.cells,
            stats.text_frames, stats.max_depth, stats.errors, elapsed, operations, self._path,
        )
        return elapsed

    @staticmethod
    def _position(shape: tuple[int, ...]) -> str:
        return ".".join(map(str, shape)) or "0"

    def _monitor(self) -> None:
        while not self._stop.wait(_PROGRESS_INTERVAL_SEC):
            with self._lock:
                progress = self._current
            if progress is None:
                continue
            slide, shape, operation, row, column, started = progress
            elapsed = time.monotonic() - started
            if elapsed >= _PROGRESS_INTERVAL_SEC:
                logger.info(
                    "[ppt] 읽기 진행: slide=%d shape=%s operation=%s row=%d column=%d elapsed=%.2f <%s>",
                    slide, self._position(shape), operation, row, column, elapsed, self._path,
                )
