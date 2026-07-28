"""파일별 실패 이력 — `index_failure.json` 상태 머신 (3차: 실패 원인 분류 및 기록).

지금까지 수집기는 실패한 파일을 그 사이클 안에서만 목록에 담고 버렸다 — 다음
실행까지 남는 실패 상태가 없어 "원래 못 읽는 파일"과 "오늘 오전처럼 잠깐 문제가
생겨 안 읽힌 파일"을 구분할 방법이 없었다. 이 모듈은 그 구분에 필요한 최소한의
메타데이터만 기록한다.

**이번 범위는 기록뿐이다.** 여기 기록된 값으로 재시도를 늦추거나 파일을 건너뛰는
로직은 이 모듈에도, 호출부(scheduler.py)에도 없다 — 그건 4차(재시도 정책)의 일이다.

COM 오류 코드만으로 실제 원인을 100% 확정할 수 없다(사용자 지적) — 특히 암호
보호·손상 문서는 더미 암호를 넘겨 즉시 실패시키는 현재 구현상 일반 COM 오류와
코드로 구분되지 않는다. 그래서 확실한 몇 가지만 분류하고 나머지는 모두
UNKNOWN_TRANSIENT로 떨어뜨린다 — 실측 로그가 쌓이면 그걸 보고 분류를 넓히는 게
순서다. `last_error_code`(HRESULT)는 그대로 보존해 그 근거로 쓸 수 있게 한다.

`index_state.json`(성공 상태)과 완전히 분리된 별도 파일이다 — 스키마·소비자·수명
주기가 다르고, 이 파일이 손상돼도 성공 상태(재인덱싱 필요 여부 판정)에 영향을
주면 안 된다.

로그·저장 내용에는 경로·분류·단계·오류코드·건수·시각만 남긴다. 문서 내용·셀
값은 물론 **예외 메시지도 저장하지 않는다** — 시트명 등 내용 조각이 섞여 들어올
수 있다(CLAUDE.md 원칙7).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

READ_STRATEGY_VERSION = 1
"""추출 로직이 결과에 영향을 줄 수 있게 바뀌면 이 값을 올린다 — 로드 시 이 값과
다른 과거 실패 기록은 폐기되어 다음 사이클에 다시 시도된다(예: 셀 단위 → 범위
단위 일괄 읽기 전환처럼 이전엔 실패하던 파일이 새 로직으로는 성공할 수 있는
변경이 여기 해당한다)."""

# 실패 분류 — 재시도 정책(4차)이 필요로 하는 수준까지만 구분한다.
KIND_TEMPORARY_BUSY = "TEMPORARY_BUSY"          # Office를 사용자/다른 프로세스가 점유 중
KIND_OPEN_TIMEOUT = "OPEN_TIMEOUT"              # 파일 Open 단계에서 워치독 타임아웃
KIND_READ_TIMEOUT = "READ_TIMEOUT"              # 열렸지만 셀/본문 읽기 단계에서 타임아웃
KIND_NEEDS_USER_ACTION = "NEEDS_USER_ACTION"    # 손상·암호·복구 등 사용자 조치 필요(DRM/판독불가)
KIND_FILE_CHANGED = "FILE_CHANGED"              # 처리 도중 파일이 변경됨(자체 해소)
KIND_UNKNOWN_TRANSIENT = "UNKNOWN_TRANSIENT"    # 원인 미확정 — 기본값

_ALL_KINDS = frozenset({
    KIND_TEMPORARY_BUSY, KIND_OPEN_TIMEOUT, KIND_READ_TIMEOUT,
    KIND_NEEDS_USER_ACTION, KIND_FILE_CHANGED, KIND_UNKNOWN_TRANSIENT,
})

# 워치독 발화 단계 → 타임아웃 세부 분류. dispatch/open은 파일을 열기도 전에 멈춘
# 것(연결·오픈 문제), sheets/cell_read/read는 열린 뒤 내용을 읽다 멈춘 것.
_OPEN_STAGES = frozenset({"dispatch", "open"})
_READ_STAGES = frozenset({"sheets", "cell_read", "read"})

# COM 오류 중 "다른 프로세스가 지금 바쁘다"는 확실한 신호로만 좁게 분류한다.
# RPC_E_CALL_REJECTED(0x80010001) · RPC_E_SERVERCALL_RETRYLATER(0x8001010A)
_BUSY_HRESULTS = frozenset({-2147418111, -2147417846})


@dataclass
class FailureRecord:
    """파일 1개의 실패 이력. 문서 내용·예외 메시지는 담지 않는다."""

    mtime: float
    size: int
    kind: str
    stage: str | None
    consecutive_failures: int
    last_failed_ts: float
    last_error_code: str | None
    strategy_version: int = READ_STRATEGY_VERSION


def _hresult_of(exc: BaseException) -> int | None:
    """예외에서 HRESULT 정수를 뽑아낸다(있으면). win32/pywintypes를 import하지 않고
    duck-typing만 쓴다 — 이 모듈은 순수 파이썬이라 사외에서도 테스트 가능해야 한다.

    pywintypes.com_error는 `.hresult` 속성 또는 `args[0]`에 HRESULT를 담는다(버전에
    따라 다름) — 둘 다 확인한다.
    """
    hresult = getattr(exc, "hresult", None)
    if isinstance(hresult, int):
        return hresult
    args = getattr(exc, "args", None)
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _format_error_code(hresult: int | None) -> str | None:
    """HRESULT를 `0x8XXXXXXX` 형태의 문자열로 포맷한다(부호 있는 32비트 → unsigned)."""
    if hresult is None:
        return None
    return f"0x{hresult & 0xFFFFFFFF:08X}"


def classify(
    exc: BaseException,
    *,
    watchdog_stage: str | None = None,
    failed_stage: str | None = None,
) -> tuple[str, str | None]:
    """예외를 (분류, HRESULT 문자열)로 분류한다.

    watchdog_stage: 워치독이 이번 파일에서 실제로 발화했다면 그 단계 이름
        (ComWatchdog.disarm()의 반환값). 발화하지 않았으면 None.
    failed_stage: com_stage.take_last_failed_stage() — COM 파싱 중 예외가 난
        단계(워치독 타임아웃이 아닌 일반 COM 오류에도 쓰인다).

    우선순위: 워치독 타임아웃(단계로 OPEN/READ 구분) > OfficeBusyError류(이름으로
    판별, win32 없이도 동작) > COM busy HRESULT > UnreadableFormatError류 >
    나머지는 UNKNOWN_TRANSIENT.
    """
    if watchdog_stage is not None:
        if watchdog_stage in _OPEN_STAGES:
            return KIND_OPEN_TIMEOUT, _format_error_code(_hresult_of(exc))
        return KIND_READ_TIMEOUT, _format_error_code(_hresult_of(exc))

    exc_type_name = type(exc).__name__
    if exc_type_name == "OfficeBusyError":
        return KIND_TEMPORARY_BUSY, _format_error_code(_hresult_of(exc))

    hresult = _hresult_of(exc)
    if hresult in _BUSY_HRESULTS:
        return KIND_TEMPORARY_BUSY, _format_error_code(hresult)

    if exc_type_name == "UnreadableFormatError":
        return KIND_NEEDS_USER_ACTION, _format_error_code(hresult)

    return KIND_UNKNOWN_TRANSIENT, _format_error_code(hresult)


def load_failures(path: Path) -> dict[str, FailureRecord]:
    """sidecar 파일을 읽어 {경로: FailureRecord} dict를 반환한다.

    부재·JSON 손상·필드 타입 이상은 모두 "기록 없음"으로 취급한다(purge_meta.py의
    load_purge_meta와 동일 관례 — 보수적으로, 실패 기록이 사라지는 쪽이 잘못된
    기록으로 재시도가 막히는 쪽보다 안전하다). 개별 항목이 손상됐으면 그 항목만
    건너뛴다(파일 전체를 버리지 않는다).

    `strategy_version`이 현재 READ_STRATEGY_VERSION과 다른 항목은 폐기한다(초기화
    조건 3).
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[failure] 실패 기록 파일 읽기 실패, 초기화: %s (%s)", path, exc)
        return {}

    if not isinstance(data, dict):
        return {}
    files = data.get("files")
    if not isinstance(files, dict):
        return {}

    out: dict[str, FailureRecord] = {}
    for file_path, raw in files.items():
        if not isinstance(raw, dict):
            continue
        try:
            kind = raw.get("kind")
            if kind not in _ALL_KINDS:
                continue
            strategy_version = raw.get("strategy_version")
            if strategy_version != READ_STRATEGY_VERSION:
                continue
            mtime = raw["mtime"]
            size = raw["size"]
            consecutive_failures = raw["consecutive_failures"]
            last_failed_ts = raw["last_failed_ts"]
            if not isinstance(mtime, (int, float)) or isinstance(mtime, bool):
                continue
            if not isinstance(size, int) or isinstance(size, bool):
                continue
            if not isinstance(consecutive_failures, int) or isinstance(consecutive_failures, bool):
                continue
            if not isinstance(last_failed_ts, (int, float)) or isinstance(last_failed_ts, bool):
                continue
            stage = raw.get("stage")
            if stage is not None and not isinstance(stage, str):
                continue
            last_error_code = raw.get("last_error_code")
            if last_error_code is not None and not isinstance(last_error_code, str):
                continue
        except (KeyError, TypeError):
            continue
        out[file_path] = FailureRecord(
            mtime=float(mtime), size=int(size), kind=kind, stage=stage,
            consecutive_failures=int(consecutive_failures),
            last_failed_ts=float(last_failed_ts), last_error_code=last_error_code,
            strategy_version=strategy_version,
        )
    return out


def save_failures(path: Path, records: dict[str, FailureRecord]) -> bool:
    """실패 기록을 원자적으로 교체 저장한다(tmp 작성 후 replace). 성공하면 True.

    저장 실패는 여기서 삼키고 False를 반환한다 — 호출부(스케줄러)는 이미 이번
    프로세스의 인메모리 판단을 마쳤으므로 저장 실패가 사이클을 막지 않는다.
    실패는 ERROR로 로그해 관측 가능하게 한다(save_purge_meta와 동일 관례).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "files": {p: asdict(r) for p, r in records.items()},
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.error("[failure] 실패 기록 저장 실패(다음 기회에 재시도): %s (%s)", path, exc)
        return False


def note_failure(
    records: dict[str, FailureRecord],
    file_path: str,
    kind: str,
    stage: str | None,
    error_code: str | None,
    mtime: float,
    size: int,
    now: float,
) -> None:
    """파일 1건의 실패를 기록한다.

    같은 (mtime, size)로 다시 실패하면(=같은 파일 내용이 계속 실패) 연속 실패
    횟수를 누적한다. mtime·size가 이전 기록과 다르면(파일이 바뀜) 1로 리셋한다
    (초기화 조건 1 — 다른 내용의 파일에 과거 실패를 이어붙이지 않는다).
    """
    prev = records.get(file_path)
    if prev is not None and prev.mtime == mtime and prev.size == size:
        consecutive = prev.consecutive_failures + 1
    else:
        consecutive = 1
    records[file_path] = FailureRecord(
        mtime=mtime, size=size, kind=kind, stage=stage,
        consecutive_failures=consecutive, last_failed_ts=now,
        last_error_code=error_code, strategy_version=READ_STRATEGY_VERSION,
    )


def note_success(records: dict[str, FailureRecord], file_path: str) -> None:
    """추출에 성공했으면 그 파일의 실패 기록을 지운다(초기화 조건 2)."""
    records.pop(file_path, None)


def prune(records: dict[str, FailureRecord], exists_fn: Callable[[str], bool] | None = None) -> int:
    """더 이상 존재하지 않는 파일의 기록을 제거한다. 제거한 건수를 반환한다.

    "이번 스캔에서 못 봤음"이 아니라 **파일이 실제로 없을 때만** 지운다 — 네트워크
    드라이브 일시 단절이나 스캔 취소로 멀쩡한 기록이 날아가는 것을 막기 위해서다.
    """
    if exists_fn is None:
        exists_fn = lambda p: Path(p).exists()  # noqa: E731
    stale = [p for p in records if not exists_fn(p)]
    for p in stale:
        records.pop(p, None)
    return len(stale)


def summarize(records: dict[str, FailureRecord]) -> dict[str, int]:
    """분류별 건수 집계를 반환한다(사이클 요약 로그용)."""
    counts: dict[str, int] = {}
    for rec in records.values():
        counts[rec.kind] = counts.get(rec.kind, 0) + 1
    return counts
