"""purge 메타 상태 머신 — sidecar(index_state.meta.json) 기반 조건부 스킵/강제 reconciliation.

`_purge_removed_folders`가 매 유휴 사이클(기본 60초)마다 LanceDB 전체를 로드하던 문제를
해결하기 위해, "이번 사이클에 purge를 실제로 실행해야 하는가"를 이 모듈이 판정한다.
판정 순서는 **차단 → 백오프 → 성공 스킵 → 실행**(억제 판정이 성공 스킵보다 항상 먼저) —
실패 직후 reconciled_sig가 해제되지 않으면 백오프가 성공 스킵에 가려 무력화되는 결함이
있었다(설계 리뷰 3~4차에서 발견).

설계: docs/ai-workflow/architecture.md § A-0002, docs/ai-workflow/adr/ADR-0002-purge-projection-and-skip.md
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 강제 reconciliation 주기 — 스킵이 계속되더라도 이 시간(초)이 지나면 처리 0건이어도
# purge를 1회 실행해, 외부 요인으로 생긴 state-DB 불일치가 무기한 방치되지 않게 한다.
DEFAULT_FORCE_RECONCILE_SEC = 24 * 3600
# 일시적 실패 후 재시도까지 대기하는 시간(초) — 실패가 지속돼도 매 사이클(60초) 전건
# 재조회하지 않도록 한다.
DEFAULT_BACKOFF_SEC = 30 * 60


def is_valid_ratio(value) -> bool:
    """0~1 범위의 유한한 실수인지 확인한다(config.yaml은 사용자가 직접 편집 가능하므로
    `max_delete_ratio`처럼 삭제 안전장치에 쓰이는 값은 fail-closed로 검증해야 한다 —
    설계 리뷰 10차 B-1. YAML의 `.nan`이 그대로 들어오면 `ratio > max_delete_ratio`
    비교가 항상 거짓이 되어 대량삭제 차단기를 무력화할 수 있다)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def is_valid_positive_seconds(value) -> bool:
    """유한하고 0보다 큰 실수인지 확인한다(강제 reconciliation 주기·백오프 값 검증용).
    삭제 안전장치는 아니므로 무효 시 안전한 기본값으로 폴백(fail-open)하면 된다."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _normalize_path_str(s: str) -> str:
    """단일 경로 문자열에 정규화 규칙을 적용한다(절대경로화·normpath·normcase·구분자
    통일·후행 구분자 제거). 루트(watch_folders)와 비교 대상(DB의 file_path) 양쪽에
    동일하게 적용해야 소속 판정이 정확하다 — 이미 절대경로인 문자열에 abspath를
    다시 적용해도 결과는 normpath와 동일해 멱등하다."""
    return os.path.normcase(os.path.normpath(os.path.abspath(s))).replace("\\", "/").rstrip("/")


def normalize_folders(watch_folders: list[str]) -> list[str]:
    """watch_folders를 서명 계산·소속 판정 공용 규칙으로 정규화한다(중복 제거·정렬 포함).

    환경변수 확장은 하지 않는다(config는 리터럴 경로만 허용 — 기존 동작 유지).
    UNC·매핑 드라이브·junction은 문자열이 다르면 다른 실체로 취급한다(파일시스템
    조회로 해석하지 않음 — 오판의 결과는 최악의 경우 불필요한 purge 1회일 뿐이라
    무해하다).
    """
    seen: set[str] = set()
    out: list[str] = []
    for f in watch_folders:
        norm = _normalize_path_str(f)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return sorted(out)


def belongs_to_any(path_str: str, normalized_roots: list[str]) -> bool:
    """경로가 정규화된 루트 목록 중 하나에 속하는지 경계 인식으로 판정한다.

    비교 전 path_str에도 루트와 동일한 정규화를 적용한다(양쪽 정규화 불일치 방지).
    `p == root or p.startswith(root + "/")` — 단순 접두사 비교와 달리 구분자를
    붙여 비교하므로 `C:/watch`가 `C:/watch-old/...`를 자기 하위로 오판하지 않는다.
    """
    p = _normalize_path_str(path_str)
    return any(p == root or p.startswith(root + "/") for root in normalized_roots)


def compute_op_sig(normalized_folders: list[str], dry_run: bool, max_delete_ratio: float) -> str:
    """op_sig(canonical JSON의 SHA-256)를 계산한다.

    스키마 버전을 포함한 고정 필드 + sort_keys + 고정 separator + UTF-8 직렬화로,
    단순 문자열 결합의 필드 경계 모호성을 없애고 프로세스 재시작에도 안정적이다.
    `allow_nan=False`로 직렬화한다 — 호출부가 `max_delete_ratio`를 `is_valid_ratio`로
    미리 검증해야 하며(설계 리뷰 10차 B-1), 검증을 건너뛴 NaN/Infinity가 여기까지
    들어오면 (표준 JSON이 아닌 리터럴 NaN을 허용하는) 조용한 직렬화 대신 즉시
    예외로 드러낸다.
    """
    payload = {
        "v": SCHEMA_VERSION,
        "folders": list(normalized_folders),
        "dry_run": bool(dry_run),
        "max_delete_ratio": float(max_delete_ratio),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class PurgeMeta:
    """purge 판정에 쓰이는 상태. 필드 의미는 모듈 docstring·설계 문서 참조.

    blocked_reason: blocked_sig가 왜 설정됐는지 구분한다(설계 리뷰 12차 m-1) —
    "mass_delete"(대량삭제 차단 안전장치) | "unsupported"(projection API 비호환,
    영구 장애). 두 원인 모두 decide()의 판정 로직(동일 op_sig에서 자동 재시도
    안 함)은 동일해 별도 상태 필드를 추가하지 않고 재사용하되, 재시작 후 표시할
    알림 문구를 구분하는 데 쓴다.
    """

    reconciled_sig: str | None = None
    last_purge_ts: float | None = None
    failed_sig: str | None = None
    next_retry_ts: float | None = None
    blocked_sig: str | None = None
    blocked_reason: str | None = None


def _is_valid_number(v) -> bool:
    """JSON에서 읽은 값이 유효한 유한 실수(bool 제외)인지 확인한다."""
    if v is None:
        return True
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return v == v and v not in (float("inf"), float("-inf"))  # NaN·Inf 배제


def load_purge_meta(meta_file: Path) -> PurgeMeta:
    """sidecar 파일을 읽어 PurgeMeta를 반환한다.

    부재·JSON 손상·필드 타입/범위 이상은 모두 "메타 없음"과 동일하게 취급한다
    (보수적 — 스킵 불가, 그 사이클 purge 실행 후 재생성).
    """
    if not meta_file.exists():
        return PurgeMeta()
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[purge] 메타 파일 읽기 실패, 무시(스킵 불가로 처리): %s (%s)", meta_file, exc)
        return PurgeMeta()

    if not isinstance(data, dict):
        return PurgeMeta()

    reconciled_sig = data.get("reconciled_sig")
    failed_sig = data.get("failed_sig")
    blocked_sig = data.get("blocked_sig")
    blocked_reason = data.get("blocked_reason")
    last_purge_ts = data.get("last_purge_ts")
    next_retry_ts = data.get("next_retry_ts")

    for name, val in (
        ("reconciled_sig", reconciled_sig), ("failed_sig", failed_sig),
        ("blocked_sig", blocked_sig), ("blocked_reason", blocked_reason),
    ):
        if val is not None and not isinstance(val, str):
            logger.warning("[purge] 메타 필드 타입 이상(%s), 무시: %r", name, val)
            return PurgeMeta()

    for name, val in (("last_purge_ts", last_purge_ts), ("next_retry_ts", next_retry_ts)):
        if not _is_valid_number(val):
            logger.warning("[purge] 메타 필드 범위 이상(%s), 무시: %r", name, val)
            return PurgeMeta()

    return PurgeMeta(
        reconciled_sig=reconciled_sig,
        last_purge_ts=last_purge_ts,
        failed_sig=failed_sig,
        next_retry_ts=next_retry_ts,
        blocked_sig=blocked_sig,
        blocked_reason=blocked_reason,
    )


def save_purge_meta(meta_file: Path, meta: PurgeMeta) -> bool:
    """sidecar를 원자적으로 교체 저장한다(tmp→replace). 성공하면 True.

    저장 실패는 이 함수 안에서 삼키고 False를 반환한다 — 호출부는 이미 메모리
    상태를 최신으로 반영했으므로(성공 스킵/억제 모두 저장 여부와 무관하게 동작),
    저장 실패가 사이클을 막지 않는다. 실패는 ERROR로 로그해 관측 가능하게 한다.
    """
    try:
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = meta_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, **asdict(meta)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(meta_file)
        return True
    except OSError as exc:
        logger.error("[purge] 메타 저장 실패(다음 기회에 재시도): %s (%s)", meta_file, exc)
        return False


@dataclass
class PurgeDecision:
    """사이클 종료부의 purge 판정 결과."""

    should_run: bool
    reason: str


def decide(
    meta: PurgeMeta,
    op_sig: str,
    processed_count: int,
    now: float,
    force_reconcile_sec: float = DEFAULT_FORCE_RECONCILE_SEC,
    backoff_sec: float = DEFAULT_BACKOFF_SEC,
) -> PurgeDecision:
    """이번 사이클에 purge를 실행해야 하는지 판정한다.

    판정 순서(차단 → 백오프 → 성공 스킵 → 실행)를 항상 지킨다 — 이 순서가 아니면
    실패 직후 reconciled_sig 불일치로 백오프 조건에 도달하지 못해 매 사이클 전건
    재조회하는 결함이 생긴다.
    """
    if meta.blocked_sig == op_sig:
        return PurgeDecision(False, "blocked")

    if meta.failed_sig == op_sig and meta.next_retry_ts is not None:
        # next_retry_ts는 정의상 미래값이 정상이다(now < next_retry_ts <= now+backoff).
        # 그 범위를 벗어난 미래값만 손상으로 간주해 억제를 무시한다.
        if now < meta.next_retry_ts <= now + backoff_sec:
            return PurgeDecision(False, "backoff")
        # 유효 범위 밖(손상 또는 이미 만료) → 억제 해제, 아래 판정으로 진행

    if (
        op_sig == meta.reconciled_sig
        and processed_count == 0
        and meta.last_purge_ts is not None
        and 0 <= (now - meta.last_purge_ts) < force_reconcile_sec
    ):
        return PurgeDecision(False, "success_skip")

    return PurgeDecision(True, "run")


def on_success(meta: PurgeMeta, op_sig: str, now: float) -> PurgeMeta:
    """purge 성공 완료 후 메타 — 모든 억제 상태를 해제하고 성공 서명·시각을 기록한다."""
    return PurgeMeta(reconciled_sig=op_sig, last_purge_ts=now, failed_sig=None, next_retry_ts=None, blocked_sig=None)


def on_transient_failure(meta: PurgeMeta, op_sig: str, now: float, backoff_sec: float = DEFAULT_BACKOFF_SEC) -> PurgeMeta:
    """일시적 예외 후 메타 — 백오프를 걸고, 이전 성공 서명(reconciled_sig)과 차단 상태를
    함께 해제해 이후 판정이 예전 성공 메타에 가려 재시도가 막히지 않게 한다."""
    return PurgeMeta(
        reconciled_sig=None, last_purge_ts=meta.last_purge_ts,
        failed_sig=op_sig, next_retry_ts=now + backoff_sec, blocked_sig=None,
    )


def on_blocked(meta: PurgeMeta, op_sig: str, reason: str = "mass_delete") -> PurgeMeta:
    """차단 후 메타 — 동일 op_sig에서는 자동 재시도하지 않도록 차단 서명만 남기고,
    이전 성공·실패 상태는 함께 해제한다(오래된 억제가 다시 유효해지는 것 방지).

    reason: "mass_delete"(대량삭제 안전장치) | "unsupported"(projection API 비호환) —
    재시작 후 표시할 알림 문구를 구분하는 데만 쓰인다(설계 리뷰 12차 m-1)."""
    return PurgeMeta(
        reconciled_sig=None, last_purge_ts=meta.last_purge_ts, failed_sig=None,
        next_retry_ts=None, blocked_sig=op_sig, blocked_reason=reason,
    )
