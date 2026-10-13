"""PowerPoint 읽기 위치·개수·시간만 기록하며 COM 객체는 보관하지 않는다."""
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, TypeVar

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_PROGRESS_INTERVAL_SEC = 10.0
_SLOW_SHAPE_SEC = 1.0
_PROBE_KINDS = {"com", "binding", "argument", "null_reference", "invalid_operation", "other"}
_PROBE_STAGES = {
    "application", "application_get", "host_identity", "document_identity", "application_compare",
    "slide_count", "slide_get", "shapes", "shapes_get", "read_shapes", "shape_iter", "shape_read", "serialize",
    "shape_type", "group_items", "group_iter", "has_table", "table_get", "table_rows", "table_columns",
    "cell_get", "cell_shape", "cell_frame", "cell_range", "cell_text",
    "has_text_frame", "text_frame", "text_range", "text_read",
    "path_validate", "host_presentations", "host_document_count", "host_document_get", "host_document_path",
}


def probe_error_details(exc: BaseException) -> dict[str, Any]:
    """COM 내부 코드와 허용된 probe 표식만 반환하고 임의 예외 문구는 버린다."""
    info = getattr(exc, "excepinfo", None)
    if not isinstance(info, (tuple, list)) or len(info) < 6:
        return {}
    result: dict[str, Any] = {}
    if isinstance(info[5], int):
        result["inner_hresult"] = info[5] & 0xFFFFFFFF
    description = info[2]
    # Office 예외 문구에는 문서 내용이 포함될 수 있어 원문을 출력하지 않는다.
    allowed = {
        "probe_wrong_thread_or_disconnected", "probe_reentrant_request",
        "probe_extract_failed", "probe_wrong_application", "probe_invalid_slide",
        "probe_missing_presentation",
        "probe_disconnected", "probe_wrong_thread", "probe_requires_office_sta",
        "probe_invalid_path", "probe_missing_host_document", "probe_ambiguous_host_document",
        "probe_document_not_open_in_host",
    }
    if isinstance(description, str):
        if description in allowed:
            result["probe_error"] = description
        else:
            match = re.fullmatch(
                r"probe_extract_failed:([a-z_]+):(?:([a-z_]+):)?([0-9A-F]{8})",
                description,
            )
            if match and match[1] in _PROBE_STAGES and (match[2] is None or match[2] in _PROBE_KINDS):
                result.update(probe_error="probe_extract_failed", probe_stage=match[1],
                              probe_inner_hresult=int(match[3], 16))
                if match[2] is not None:
                    result["probe_error_kind"] = match[2]
    return result


def log_probe_read_errors(payload: dict[str, Any], slide: int) -> None:
    """추가 기능의 복구 가능 오류 중 검증된 위치·분류·코드만 제한해서 기록한다."""
    records = payload.get("read_errors", [])
    if not isinstance(records, list):
        return
    logged = 0
    for record in records[:16]:
        if not isinstance(record, dict):
            continue
        stage, kind, shape = record.get("stage"), record.get("kind"), record.get("shape")
        row, column, code = record.get("row"), record.get("column"), record.get("hresult")
        if not isinstance(stage, str) or stage not in _PROBE_STAGES:
            continue
        if not isinstance(kind, str) or kind not in _PROBE_KINDS:
            continue
        if not isinstance(shape, str) or len(shape) > 1024 or re.fullmatch(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*", shape) is None:
            continue
        if any(type(value) is not int or value < 0 for value in (row, column, code)) or code > 0xFFFFFFFF:
            continue
        logger.warning(
            "[ppt-probe] 내부 읽기 오류: slide=%d shape=%s stage=%s row=%d column=%d kind=%s hresult=0x%08X",
            slide, shape, stage, row, column, kind, code,
        )
        logged += 1
    total = payload.get("counts", {}).get("errors", 0)
    if type(total) is int and total > logged:
        logger.warning("[ppt-probe] 상세 미표시 오류: slide=%d count=%d", slide, total - logged)


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

    @property
    def error_count(self) -> int:
        """Return all swallowed read failures so partial text cannot be committed."""
        return sum(stats.errors for stats in self._stats.values())

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
