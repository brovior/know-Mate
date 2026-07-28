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

import hashlib
import json
import logging
import math
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
KIND_OPEN_ERROR = "OPEN_ERROR"                  # 6a: 워치독 미발화 COM 오류, Open 단계
KIND_READ_ERROR = "READ_ERROR"                  # 6a: 워치독 미발화 COM 오류, Read 단계
KIND_NEEDS_USER_ACTION = "NEEDS_USER_ACTION"    # 손상·암호·복구 등 사용자 조치 필요(DRM/판독불가)
KIND_FILE_CHANGED = "FILE_CHANGED"              # 처리 도중 파일이 변경됨(자체 해소)
KIND_UNKNOWN_TRANSIENT = "UNKNOWN_TRANSIENT"    # 원인 미확정 — 기본값

_ALL_KINDS = frozenset({
    KIND_TEMPORARY_BUSY, KIND_OPEN_TIMEOUT, KIND_READ_TIMEOUT,
    KIND_OPEN_ERROR, KIND_READ_ERROR,
    KIND_NEEDS_USER_ACTION, KIND_FILE_CHANGED, KIND_UNKNOWN_TRANSIENT,
})

# SOURCE_CHANGED(설계 초안 검토 중 잠깐 고려됐던 이름)로 수동 편집된 sidecar가
# 있어도 폐기하지 않고 FILE_CHANGED로 정규화한다(A-0003 §6 — 개명하지 않기로
# 확정했지만, 외부 편집 대비 하위호환만 유지).
_KIND_ALIASES = {"SOURCE_CHANGED": KIND_FILE_CHANGED}

# 워치독 발화 단계 → 타임아웃/오류 세부 분류. dispatch/open은 파일을 열기도 전에
# 멈춘 것(연결·오픈 문제), sheets/cell_read/read는 열린 뒤 내용을 읽다 멈춘 것.
_OPEN_STAGES = frozenset({"dispatch", "open"})
_READ_STAGES = frozenset({"sheets", "cell_read", "read"})


def _normalize_stage(stage: str | None) -> str | None:
    """원시 단계 이름을 "open"/"read"로 정규화한다(연속성 판정·A-0003 §4 참고).

    OPEN_ERROR·OPEN_TIMEOUT은 항상 _OPEN_STAGES에서만, READ_ERROR·READ_TIMEOUT은
    항상 _READ_STAGES에서만 나오므로 현재는 kind만으로도 이미 같은 구분이 되지만,
    stage가 kind와 독립적으로 저장되는 미래 확장에도 안전하도록 명시적으로 둔다.
    """
    if stage in _OPEN_STAGES:
        return "open"
    if stage in _READ_STAGES:
        return "read"
    return stage

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
    force_retry: bool = False
    """수동 재인덱싱이 "백오프 이번 한 번만 무시하고 다시 시도"를 요청했는지
    (초기화 조건 4 수정판 — 예전엔 기록 전체를 비웠지만, 그러면 만성 실패 파일의
    연속 실패 횟수·이력까지 함께 사라져 재인덱싱 버튼 한 번으로 30분 백오프로
    되돌아가는 문제가 있었다). should_defer()가 이 값을 보고 대기를 건너뛰게 하며,
    그 시도 결과(note_success/note_failure)가 기록을 새로 만들면서 자연히
    False로 소비된다 — 별도로 끄는 코드가 필요 없다."""


_DEFAULT_TEMPORARY_BUSY_SEC = 300.0                        # 5분 base(+지터로 5~10분)
_DEFAULT_TIMEOUT_LADDER_SEC = (1800.0, 21600.0)             # 30분 → 6시간 (3회차부터는 승격 상한)
_DEFAULT_UNKNOWN_LADDER_SEC = (1800.0, 21600.0)             # 위와 동일 사다리(별도 조정 가능)
_DEFAULT_FILE_CHANGED_SEC = 0.0                             # 다음 사이클 즉시
_DEFAULT_NEEDS_USER_ACTION_MAX_SEC = 7 * 24 * 3600.0        # 7일 안전밸브

# TEMPORARY_BUSY 지터 폭 — base(300초) + 0~이 값 만큼 가산해 5~10분 범위를 만든다.
_TEMPORARY_BUSY_JITTER_SEC = 300.0

# 승격 상태(6a·A-0003 §5) — UI 표시("반복 중"/"조치 필요")와 백오프 사다리 길이
# 초과 시 상한(needs_user_action_max_sec)을 이 임계로 판정한다. 사용자 결정(안 B):
# "3회 이상 → 사용자 조치 필요"는 대기 시간까지 의미하므로, 사다리는 2단(30분·6시간)
# 까지만 두고 3회차부터 escalation_state가 NEEDS_ACTION으로 승격시켜
# needs_user_action_max_sec(기본 7일)을 적용한다.
_DEFAULT_ESCALATION_REPEAT_AT = 2
_DEFAULT_ESCALATION_ACTION_AT = 3

ESCALATION_NORMAL = "NORMAL"
ESCALATION_REPEATED = "REPEATED"
ESCALATION_NEEDS_ACTION = "NEEDS_ACTION"

# 백오프에서 원인별 정책이 항상 우선하는 kind — 승격 사다리(3회=7일)를 타지 않는다
# (3차 리뷰 M-2: TEMPORARY_BUSY가 사다리를 타면 파일을 3번 열어둔 사용자가 7일
# 대기로 넘어가는 회귀가 생긴다. UI 상태는 여전히 공통 적용된다 — escalation_state
# 참고).
_FIXED_BACKOFF_KINDS = frozenset({KIND_TEMPORARY_BUSY, KIND_FILE_CHANGED, KIND_NEEDS_USER_ACTION})


@dataclass(frozen=True)
class BackoffPolicy:
    """4차: 실패 유형별 재시도 대기 정책.

    "영구적인 무시가 아니라 재시도 시점만 조정"이 완료 기준이라, 모든 분류에
    상한이 있어야 한다(NEEDS_USER_ACTION도 needs_user_action_max_sec으로 상한을
    둔다 — 시간 기반 재시도 없음 정책이지만 7일마다 재확인해, 우리가 만든 일시적
    상태 때문에 멀쩡한 파일이 영구히 안 읽히는 사고(세이프모드 루프 사건)를
    반복하지 않는다).
    """

    enabled: bool = True
    temporary_busy_sec: float = _DEFAULT_TEMPORARY_BUSY_SEC
    timeout_ladder_sec: tuple[float, ...] = _DEFAULT_TIMEOUT_LADDER_SEC
    unknown_ladder_sec: tuple[float, ...] = _DEFAULT_UNKNOWN_LADDER_SEC
    file_changed_sec: float = _DEFAULT_FILE_CHANGED_SEC
    needs_user_action_max_sec: float = _DEFAULT_NEEDS_USER_ACTION_MAX_SEC
    escalation_repeat_at: int = _DEFAULT_ESCALATION_REPEAT_AT
    escalation_action_at: int = _DEFAULT_ESCALATION_ACTION_AT

    @classmethod
    def from_config(cls, collector_cfg: dict) -> "BackoffPolicy":
        """`collector.failure_backoff`에서 정책을 만든다.

        무효 값(NaN·음수·타입 오류 등)은 조용히 기본값으로 폴백한다(fail-open)
        — 이 정책은 삭제 안전장치가 아니라 "언제 재시도할지"만 정하므로, 잘못된
        설정의 결과는 "예상보다 자주 재시도"(무해)여야지 "영구히 재시도 안 함"
        이면 안 된다.
        """
        raw = collector_cfg.get("failure_backoff", {})
        if not isinstance(raw, dict):
            return cls()

        def _sec(key: str, default: float) -> float:
            v = raw.get(key, default)
            return float(v) if _is_valid_nonneg_seconds(v) else default

        def _ladder(key: str, default: tuple[float, ...]) -> tuple[float, ...]:
            v = raw.get(key, default)
            if not isinstance(v, (list, tuple)) or not v:
                return default
            out = []
            for item in v:
                if not _is_valid_nonneg_seconds(item):
                    return default
                out.append(float(item))
            return tuple(out)

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = True

        repeat_at = raw.get("escalation_repeat_at", _DEFAULT_ESCALATION_REPEAT_AT)
        action_at = raw.get("escalation_action_at", _DEFAULT_ESCALATION_ACTION_AT)
        if not _is_valid_escalation_pair(repeat_at, action_at):
            repeat_at, action_at = _DEFAULT_ESCALATION_REPEAT_AT, _DEFAULT_ESCALATION_ACTION_AT

        return cls(
            enabled=enabled,
            temporary_busy_sec=_sec("temporary_busy_sec", _DEFAULT_TEMPORARY_BUSY_SEC),
            timeout_ladder_sec=_ladder("timeout_ladder_sec", _DEFAULT_TIMEOUT_LADDER_SEC),
            unknown_ladder_sec=_ladder("unknown_ladder_sec", _DEFAULT_UNKNOWN_LADDER_SEC),
            file_changed_sec=_sec("file_changed_sec", _DEFAULT_FILE_CHANGED_SEC),
            needs_user_action_max_sec=_sec(
                "needs_user_action_max_sec", _DEFAULT_NEEDS_USER_ACTION_MAX_SEC
            ),
            escalation_repeat_at=int(repeat_at),
            escalation_action_at=int(action_at),
        )


def _is_valid_escalation_pair(repeat_at, action_at) -> bool:
    """승격 임계 두 값이 `0 < repeat_at < action_at`인 정수인지 확인한다
    (2차 리뷰 M-2 — 순서가 뒤바뀌면 REPEATED 상태가 아예 나타나지 않거나
    NEEDS_ACTION보다 늦게 반응하는 등 판정이 무의미해진다)."""
    for v in (repeat_at, action_at):
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            return False
    return repeat_at < action_at


def _is_valid_nonneg_seconds(value) -> bool:
    """유한하고 0 이상인 실수인지 확인한다(백오프 값은 0도 허용 — FILE_CHANGED는
    0초가 정상값이라 purge_meta.is_valid_positive_seconds(>0 only)는 재사용할 수
    없다)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _path_jitter_fraction(file_path: str) -> float:
    """경로에서 결정적인 0.0~1.0 지터를 뽑는다.

    여러 파일의 재시도 시각이 한 사이클에 몰리는 것을 흩어준다. 파이썬 내장
    hash()는 프로세스마다 랜덤 솔트가 달라(PYTHONHASHSEED) 재시작 시 값이
    바뀌므로 쓸 수 없다 — sha256으로 프로세스 재시작에도 안정적인 값을 얻는다.
    """
    digest = hashlib.sha256(file_path.encode("utf-8", errors="surrogateescape")).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF


def escalation_state(rec: FailureRecord, policy: BackoffPolicy) -> str:
    """연속 실패 횟수만으로 3상태를 판정하는 단일 진실원(A-0003 §5 — 2차 리뷰 M-2).

    UI(`bridge.py`)와 백오프(`backoff_seconds`)가 **모두 이 함수만** 호출한다.
    임계를 여러 호출부가 각자 해석하면 같은 레코드를 서로 다르게 판정할 위험이
    있다(2차 리뷰 M-2가 지적한 위험 그 자체 — kind와 무관하게 순수 카운트만 본다).
    """
    if rec.consecutive_failures >= policy.escalation_action_at:
        return ESCALATION_NEEDS_ACTION
    if rec.consecutive_failures >= policy.escalation_repeat_at:
        return ESCALATION_REPEATED
    return ESCALATION_NORMAL


def backoff_seconds(rec: FailureRecord, file_path: str, policy: BackoffPolicy) -> float:
    """이 기록에 다음 재시도까지 적용할 대기 시간(초)을 계산한다.

    **원인별 고정 정책이 승격 사다리보다 항상 우선한다**(3차 리뷰 M-2 — 사용자
    결정 안 B로 "3회 이상=7일 대기"가 확정되면서, 이 우선순위가 없으면 파일을
    3번 연속 열어둔 사용자(TEMPORARY_BUSY)의 재시도가 7일 뒤로 밀리는 회귀가
    생긴다). TEMPORARY_BUSY·FILE_CHANGED·NEEDS_USER_ACTION은 회차와 무관하게
    항상 같은 값이고, 그 외(OPEN_ERROR·READ_ERROR·OPEN_TIMEOUT·READ_TIMEOUT·
    UNKNOWN_TRANSIENT)만 승격 사다리(`escalation_state`)를 탄다.

    UI 상태(`escalation_state`)는 이 함수와 무관하게 모든 kind에 공통 적용된다
    — "표시"와 "대기"가 서로 다른 축이라는 것이 A-0003 §1·§5의 핵심이다.
    """
    if rec.kind == KIND_TEMPORARY_BUSY:
        jitter = _path_jitter_fraction(file_path) * _TEMPORARY_BUSY_JITTER_SEC
        return policy.temporary_busy_sec + jitter
    if rec.kind == KIND_FILE_CHANGED:
        return policy.file_changed_sec
    if rec.kind == KIND_NEEDS_USER_ACTION:
        return policy.needs_user_action_max_sec

    if escalation_state(rec, policy) == ESCALATION_NEEDS_ACTION:
        return policy.needs_user_action_max_sec  # 승격 상한 재사용(안전밸브 — FR-4)

    if rec.kind in (KIND_OPEN_TIMEOUT, KIND_READ_TIMEOUT, KIND_OPEN_ERROR, KIND_READ_ERROR):
        ladder = policy.timeout_ladder_sec
    else:  # KIND_UNKNOWN_TRANSIENT (및 알 수 없는 미래 분류에 대한 안전한 기본)
        ladder = policy.unknown_ladder_sec
    if not ladder:
        return 0.0
    idx = min(rec.consecutive_failures, len(ladder)) - 1
    idx = max(idx, 0)
    return ladder[idx]


def should_defer(
    rec: FailureRecord | None,
    file_path: str,
    mtime: float,
    size: int,
    now: float,
    policy: BackoffPolicy,
) -> bool:
    """이번 사이클에 이 파일을 건너뛰어야 하는지 판정한다.

    생산자(큐에 넣기 전)와 소비자(처리 직전) 양쪽이 이 함수 하나를 호출한다 —
    판정 로직이 두 곳에서 갈라지면 그 자체가 버그의 원천이라 반드시 공유한다.

    판정 순서는 "재시도하는 쪽"이 항상 먼저다(fail-open):
    기록 없음 → 처리 / 정책 비활성 → 처리 / 파일 변경(mtime·size 불일치, 완료기준
    "파일이 변경되면 즉시 다시 시도") → 처리 / force_retry(수동 재인덱싱 요청) →
    처리 / 그 외엔 마지막 실패 이후 경과 시간이 백오프보다 짧으면 건너뜀.
    """
    if rec is None:
        return False
    if not policy.enabled:
        return False
    if rec.mtime != mtime or rec.size != size:
        return False
    if rec.force_retry:
        return False
    wait = backoff_seconds(rec, file_path, policy)
    return now < rec.last_failed_ts + wait


def describe_wait(rec: FailureRecord, file_path: str, now: float, policy: BackoffPolicy) -> str:
    """로그에 붙일 "N시간 M분 후 재시도" 형태의 한 줄을 반환한다(경로·내용 없음)."""
    wait = backoff_seconds(rec, file_path, policy)
    remaining = max(rec.last_failed_ts + wait - now, 0.0)
    hours, rem = divmod(int(remaining), 3600)
    minutes = rem // 60
    if hours > 0:
        return f"{hours}시간 {minutes}분 후 재시도"
    return f"{minutes}분 후 재시도"


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
    failed_stage로 OPEN_ERROR/READ_ERROR 구분(6a — 이전엔 이 인자를 받고도 쓰지
    않아 전부 UNKNOWN_TRANSIENT로 떨어졌다) > 나머지는 UNKNOWN_TRANSIENT.
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

    if failed_stage in _OPEN_STAGES:
        return KIND_OPEN_ERROR, _format_error_code(hresult)
    if failed_stage in _READ_STAGES:
        return KIND_READ_ERROR, _format_error_code(hresult)

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
            kind = _KIND_ALIASES.get(kind, kind)
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
            force_retry = raw.get("force_retry", False)
            if not isinstance(force_retry, bool):
                force_retry = False
        except (KeyError, TypeError):
            continue
        out[file_path] = FailureRecord(
            mtime=float(mtime), size=int(size), kind=kind, stage=stage,
            consecutive_failures=int(consecutive_failures),
            last_failed_ts=float(last_failed_ts), last_error_code=last_error_code,
            strategy_version=strategy_version, force_retry=force_retry,
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

    같은 (mtime, size)로, **같은 (분류, 정규화 단계)**로 다시 실패하면(=같은 파일
    내용이 같은 이유·같은 단계에서 계속 실패) 연속 실패 횟수를 누적한다. mtime·size가
    바뀌었거나(초기화 조건 1) 분류·단계가 달라졌으면(4차 — 백오프 사다리는 분류별로
    독립적이어야 한다) 1로 리셋한다. 분류까지 같아야 누적하지 않으면, 예를 들어
    사용자가 파일을 하루 종일 열어둬 TEMPORARY_BUSY가 여러 번 쌓인 뒤 우연히
    시간초과가 1번 나면 consecutive가 크게 이어받아 곧장 긴 백오프로 튀는 오류가
    생긴다. 단계까지 비교하는 것은 A-0003 §4(6a) — 현재는 kind가 이미 단계를
    함의하지만(OPEN_ERROR는 항상 open류 단계), 미래에 kind와 단계가 분리될 경우를
    대비한 안전망이다.
    """
    prev = records.get(file_path)
    if (
        prev is not None and prev.mtime == mtime and prev.size == size
        and prev.kind == kind and _normalize_stage(prev.stage) == _normalize_stage(stage)
    ):
        consecutive = prev.consecutive_failures + 1
    else:
        consecutive = 1
    records[file_path] = FailureRecord(
        mtime=mtime, size=size, kind=kind, stage=stage,
        consecutive_failures=consecutive, last_failed_ts=now,
        last_error_code=error_code, strategy_version=READ_STRATEGY_VERSION,
    )


def request_retry_all(records: dict[str, FailureRecord]) -> int:
    """모든 기록에 force_retry를 세워, 이번 사이클엔 백오프를 무시하고 한 번씩
    다시 시도하게 한다(초기화 조건 4 수정판 — 수동 재인덱싱 트리거 전용).

    예전엔 `records`를 통째로 비웠지만, 그러면 만성 실패 파일의 연속 실패
    횟수·이력까지 함께 사라져 버튼 한 번으로 백오프가 처음 칸(30분)으로
    되돌아갔다(재인덱싱을 누른 사용자가 그 자리에서 DRM 문서 재시도 대기를
    떠안는 사고). 이력은 그대로 두고 "이번 한 번만" 봐주는 플래그만 세운다 —
    재시도 결과(성공/실패)가 반영되면 새 FailureRecord가 만들어지며 플래그는
    자연히 꺼진다. 반환값은 플래그를 세운 건수(로그용).
    """
    for file_path, rec in list(records.items()):
        if rec.force_retry:
            continue
        records[file_path] = FailureRecord(
            mtime=rec.mtime, size=rec.size, kind=rec.kind, stage=rec.stage,
            consecutive_failures=rec.consecutive_failures,
            last_failed_ts=rec.last_failed_ts, last_error_code=rec.last_error_code,
            strategy_version=rec.strategy_version, force_retry=True,
        )
    return len(records)


def request_retry_one(records: dict[str, FailureRecord], file_path: str) -> bool:
    """지정한 파일 1건에만 force_retry를 세운다(5차: [확인 필요한 문서] 화면의
    개별 「지금 다시 시도」 버튼 전용). request_retry_all과 동일하게 이력은
    보존한다. 기록이 없으면 아무 것도 하지 않고 False를 반환한다."""
    rec = records.get(file_path)
    if rec is None or rec.force_retry:
        return False
    records[file_path] = FailureRecord(
        mtime=rec.mtime, size=rec.size, kind=rec.kind, stage=rec.stage,
        consecutive_failures=rec.consecutive_failures,
        last_failed_ts=rec.last_failed_ts, last_error_code=rec.last_error_code,
        strategy_version=rec.strategy_version, force_retry=True,
    )
    return True


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
