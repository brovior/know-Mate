"""LanceDB 작은 fragment를 제한하는 비파괴적 주기 유지보수."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

DEFAULT_OPTIMIZE_EVERY_MUTATIONS = 100
DEFAULT_STARTUP_SMALL_FRAGMENTS = 100
DEFAULT_CYCLE_END_MIN_MUTATIONS = 20
DEFAULT_FAILURE_COOLDOWN_SEC = 300.0


@dataclass(frozen=True)
class LanceMaintenanceConfig:
    """검증을 마친 LanceDB 유지보수 설정."""

    enabled: bool = True
    optimize_every_mutations: int = DEFAULT_OPTIMIZE_EVERY_MUTATIONS
    startup_optimize_when_small_fragments_reach: int = DEFAULT_STARTUP_SMALL_FRAGMENTS
    cycle_end_min_mutations: int = DEFAULT_CYCLE_END_MIN_MUTATIONS
    failure_cooldown_sec: float = DEFAULT_FAILURE_COOLDOWN_SEC

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "LanceMaintenanceConfig":
        """사용자 YAML 값을 안전한 기본값과 함께 읽는다."""
        values = raw if isinstance(raw, Mapping) else {}
        enabled = values.get("enabled", True)
        if not isinstance(enabled, bool):
            logger.warning("[lance_maintenance] enabled 값이 bool이 아니어서 true 적용: %r", enabled)
            enabled = True
        return cls(
            enabled=enabled,
            optimize_every_mutations=_positive_int(
                values.get("optimize_every_mutations"),
                DEFAULT_OPTIMIZE_EVERY_MUTATIONS,
                "optimize_every_mutations",
            ),
            startup_optimize_when_small_fragments_reach=_positive_int(
                values.get("startup_optimize_when_small_fragments_reach"),
                DEFAULT_STARTUP_SMALL_FRAGMENTS,
                "startup_optimize_when_small_fragments_reach",
            ),
            cycle_end_min_mutations=_nonnegative_int(
                values.get("cycle_end_min_mutations"),
                DEFAULT_CYCLE_END_MIN_MUTATIONS,
                "cycle_end_min_mutations",
            ),
            failure_cooldown_sec=_positive_number(
                values.get("failure_cooldown_sec"),
                DEFAULT_FAILURE_COOLDOWN_SEC,
                "failure_cooldown_sec",
            ),
        )


def _positive_int(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 1:
        logger.warning("[lance_maintenance] %s 값이 잘못되어 기본값 %d 적용: %r", name, default, value)
        return default
    return value


def _nonnegative_int(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 0:
        logger.warning("[lance_maintenance] %s 값이 잘못되어 기본값 %d 적용: %r", name, default, value)
        return default
    return value


def _positive_number(value: Any, default: float, name: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        logger.warning("[lance_maintenance] %s 값이 잘못되어 기본값 %.0f 적용: %r", name, default, value)
        return default
    return float(value)


class LanceTableMaintenance:
    """테이블별 mutation 수와 optimize 재시도 상태를 관리한다.

    실제 optimize는 호출자가 상태 파일을 먼저 저장한 안전 체크포인트에서만
    ``run_*`` 메서드로 시작한다. DB 쓰기 메서드는 ``record_mutation``만 호출한다.
    """

    def __init__(
        self,
        table: Any,
        table_name: str,
        config: Mapping[str, Any] | LanceMaintenanceConfig | None,
        *,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._table = table
        self.table_name = table_name
        self.config = (
            config if isinstance(config, LanceMaintenanceConfig)
            else LanceMaintenanceConfig.from_mapping(config)
        )
        self._now = now_fn
        self.mutations_since_optimize = 0
        self.startup_checked = False
        self.last_failure_at: float | None = None
        self.in_progress = False

    def record_mutation(self, count: int = 1) -> None:
        """성공한 Lance 쓰기 API 호출 수를 누적한다."""
        if self.config.enabled and count > 0:
            self.mutations_since_optimize += count

    def note_external_optimize_success(self) -> None:
        """하위호환 공개 optimize 경로가 실행됐을 때 중복 최적화를 막는다."""
        self.mutations_since_optimize = 0
        self.last_failure_at = None
        self.startup_checked = True

    def periodic_due(self) -> bool:
        return (
            self.config.enabled
            and self.mutations_since_optimize >= self.config.optimize_every_mutations
            and self._cooldown_elapsed()
        )

    def cycle_end_due(self) -> bool:
        threshold = self.config.cycle_end_min_mutations
        return (
            self.config.enabled
            and threshold > 0
            and self.mutations_since_optimize >= threshold
            and self._cooldown_elapsed()
        )

    def run_startup_check(
        self,
        *,
        cancelled: Callable[[], bool] | None = None,
        memory_log: Callable[[str], None] | None = None,
    ) -> bool:
        """프로세스 수명 중 최초 사이클에서 작은 fragment 임계값을 한 번 확인한다."""
        if not self.config.enabled or self.startup_checked or not self._cooldown_elapsed():
            return False
        if cancelled and cancelled():
            return False
        try:
            before = self._fragment_stats()
        except Exception as exc:
            self.last_failure_at = self._now()
            logger.warning("[lance_maintenance] table=%s 시작 fragment 조회 실패: %s", self.table_name, exc)
            return False
        small = before.get("num_small_fragments")
        if not isinstance(small, int) or small < self.config.startup_optimize_when_small_fragments_reach:
            self.startup_checked = True
            return False
        # 성공했지만 줄지 않은 경우도 완료로 보되, optimize 자체가 실패한 경우에는
        # cooldown 뒤 다음 사이클에서 다시 시도할 수 있어야 한다.
        succeeded = self._run_optimize("startup_small_fragments", before, cancelled, memory_log)
        self.startup_checked = succeeded
        return succeeded

    def run_periodic(
        self,
        *,
        cancelled: Callable[[], bool] | None = None,
        memory_log: Callable[[str], None] | None = None,
    ) -> bool:
        if not self.periodic_due() or (cancelled and cancelled()):
            return False
        return self._run_optimize("mutation_limit", None, cancelled, memory_log)

    def run_cycle_end(
        self,
        *,
        cancelled: Callable[[], bool] | None = None,
        memory_log: Callable[[str], None] | None = None,
    ) -> bool:
        if not self.cycle_end_due() or (cancelled and cancelled()):
            return False
        return self._run_optimize("cycle_end", None, cancelled, memory_log)

    def _run_optimize(
        self,
        trigger: str,
        before: dict[str, int] | None,
        cancelled: Callable[[], bool] | None,
        memory_log: Callable[[str], None] | None,
    ) -> bool:
        if self.in_progress or not self._cooldown_elapsed() or (cancelled and cancelled()):
            return False
        # 종료 스레드가 이 상태를 관찰하면 60초 graceful wait를 사용한다. 통계 조회
        # 도중 취소가 들어와도 optimize 직전 재확인에서 중단하며, 그 사이 상태는 True라
        # cancel 확인과 native 호출 사이의 TOCTOU로 8초 terminate가 선택되지 않는다.
        self.in_progress = True
        try:
            if before is None:
                try:
                    before = self._fragment_stats()
                except Exception as exc:
                    logger.warning(
                        "[lance_maintenance] table=%s optimize 전 통계 조회 실패(계속 진행): %s",
                        self.table_name, exc,
                    )
                    before = {}
            mutations = self.mutations_since_optimize
            logger.info(
                "[lance_maintenance] table=%s trigger=%s mutations=%d "
                "before_fragments=%s before_small=%s",
                self.table_name, trigger, mutations,
                before.get("num_fragments", "n/a"), before.get("num_small_fragments", "n/a"),
            )
            if memory_log:
                memory_log(f"before_optimize_{self.table_name}")
            if cancelled and cancelled():
                return False
            started = self._now()
            self._table.optimize()
            elapsed = self._now() - started
            self.mutations_since_optimize = 0
            self.last_failure_at = None
            try:
                after = self._fragment_stats()
            except Exception as exc:
                logger.warning(
                    "[lance_maintenance] table=%s optimize 후 통계 조회 실패: %s",
                    self.table_name, exc,
                )
                after = {}
            if memory_log:
                memory_log(f"after_optimize_{self.table_name}")
            logger.info(
                "[lance_maintenance] table=%s optimize 완료 trigger=%s elapsed=%.2fs "
                "after_fragments=%s after_small=%s",
                self.table_name, trigger, elapsed,
                after.get("num_fragments", "n/a"), after.get("num_small_fragments", "n/a"),
            )
            return True
        except Exception as exc:
            self.last_failure_at = self._now()
            logger.warning(
                "[lance_maintenance] table=%s optimize 실패 trigger=%s — 인덱싱은 유지하고 재시도 연기: %s",
                self.table_name, trigger, exc,
            )
            return False
        finally:
            self.in_progress = False

    def _cooldown_elapsed(self) -> bool:
        if self.last_failure_at is None:
            return True
        return self._now() - self.last_failure_at >= self.config.failure_cooldown_sec

    def _fragment_stats(self) -> dict[str, int]:
        """LanceDB 0.34.0의 실제 dict stats 반환값을 방어적으로 읽는다."""
        stats = self._table.stats()
        if not isinstance(stats, dict):
            raise TypeError("table.stats() did not return dict")
        fragments = stats.get("fragment_stats")
        if not isinstance(fragments, dict):
            raise TypeError("fragment_stats missing")
        result: dict[str, int] = {}
        for key in ("num_fragments", "num_small_fragments"):
            value = fragments.get(key)
            if type(value) is int and value >= 0:
                result[key] = value
        if "num_small_fragments" not in result:
            raise TypeError("num_small_fragments missing")
        return result
