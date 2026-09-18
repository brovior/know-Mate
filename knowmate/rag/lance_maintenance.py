"""Durable, fragment-backlog based LanceDB maintenance."""
from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

DEFAULT_OPTIMIZE_SMALL = 100
DEFAULT_HARD_LIMIT_SMALL = 1000
DEFAULT_BACKLOG_FINALIZE_SMALL = 300
DEFAULT_MIN_INTERVAL_SEC = 86400.0
DEFAULT_FAILURE_COOLDOWN_SEC = 300.0
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LanceMaintenanceConfig:
    """Validated configuration for one LanceDB table."""

    enabled: bool = True
    optimize_when_small_fragments_reach: int = DEFAULT_OPTIMIZE_SMALL
    backlog_hard_limit_small_fragments: int = DEFAULT_HARD_LIMIT_SMALL
    backlog_finalize_small_fragments_reach: int = DEFAULT_BACKLOG_FINALIZE_SMALL
    min_optimize_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC
    failure_cooldown_sec: float = DEFAULT_FAILURE_COOLDOWN_SEC

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "LanceMaintenanceConfig":
        """Read current keys only; obsolete mutation keys intentionally do nothing."""
        values = raw if isinstance(raw, Mapping) else {}
        enabled = values.get("enabled", True)
        if not isinstance(enabled, bool):
            logger.warning("[lance_maintenance] enabled must be bool; using true")
            enabled = True
        for old in ("optimize_every_mutations", "cycle_end_min_mutations"):
            if old in values:
                logger.warning("[lance_maintenance] obsolete %s is ignored", old)
        return cls(
            enabled=enabled,
            optimize_when_small_fragments_reach=_positive_int(values.get("optimize_when_small_fragments_reach"), DEFAULT_OPTIMIZE_SMALL, "optimize_when_small_fragments_reach"),
            backlog_hard_limit_small_fragments=_positive_int(values.get("backlog_hard_limit_small_fragments"), DEFAULT_HARD_LIMIT_SMALL, "backlog_hard_limit_small_fragments"),
            backlog_finalize_small_fragments_reach=_positive_int(values.get("backlog_finalize_small_fragments_reach"), DEFAULT_BACKLOG_FINALIZE_SMALL, "backlog_finalize_small_fragments_reach"),
            min_optimize_interval_sec=_positive_number(values.get("min_optimize_interval_sec"), DEFAULT_MIN_INTERVAL_SEC, "min_optimize_interval_sec"),
            failure_cooldown_sec=_positive_number(values.get("failure_cooldown_sec"), DEFAULT_FAILURE_COOLDOWN_SEC, "failure_cooldown_sec"),
        )


def _positive_int(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    if type(value) is not int or value < 1:
        logger.warning("[lance_maintenance] invalid %s; using %s", name, default)
        return default
    return value


def _positive_number(value: Any, default: float, name: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        logger.warning("[lance_maintenance] invalid %s; using %s", name, default)
        return default
    return float(value)


class LanceTableMaintenance:
    """Coordinate safe optimize calls for one table after a durable checkpoint."""

    def __init__(
        self, table: Any, table_name: str, config: Mapping[str, Any] | LanceMaintenanceConfig | None,
        *, db_path: str | Path | None = None, recreated: bool = False, confirmed_empty: bool = False,
        state_dir: Path | None = None, now_fn: Callable[[], float] = time.monotonic,
        wall_time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._table = table
        self.table_name = table_name
        self.config = config if isinstance(config, LanceMaintenanceConfig) else LanceMaintenanceConfig.from_mapping(config)
        self._now, self._wall_now = now_fn, wall_time_fn
        self.in_progress = False
        self.mutations_since_optimize = 0  # telemetry only; never a trigger
        self._db_identity = self._canonical_db_identity(db_path, table)
        self._state_path = (state_dir or self._default_state_dir()) / f"{table_name}.json"
        self._state = self._empty_state()
        if not recreated and not confirmed_empty:
            self._load_state()
        else:
            self._persist_state("table reset")
        self._restore_deadlines()
        # 실제 fragment 수가 trigger이지만 매 write마다 table.stats()를 호출하면
        # 유지보수 판정 자체가 새 병목이 된다. 기본 100 write 간격으로만 통계를
        # 표본화하며, 재시작으로 열린 backlog를 복구한 경우 첫 checkpoint는 즉시 확인한다.
        self._fragment_check_interval = max(
            1,
            min(
                self.config.optimize_when_small_fragments_reach,
                self.config.backlog_hard_limit_small_fragments,
            ),
        )
        self._mutations_since_fragment_check = (
            self._fragment_check_interval if self.backlog_active else 0
        )

    @staticmethod
    def _canonical_db_identity(db_path: str | Path | None, table: Any) -> str:
        candidate = db_path or getattr(getattr(table, "_conn", None), "uri", None) or getattr(table, "uri", None)
        try:
            return str(Path(str(candidate or "unknown")).expanduser().resolve())
        except OSError:
            return str(candidate or "unknown")

    @staticmethod
    def _default_state_dir() -> Path:
        try:
            from knowmate.config import get_data_dir
            return get_data_dir() / "lancedb_maintenance"
        except Exception:
            return Path(os.environ.get("APPDATA", ".")) / "AegisDesk" / "lancedb_maintenance"

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION, "db_identity": self._db_identity,
            "table_name": self.table_name, "generation": str(uuid.uuid4()),
            "last_success_wall_time": None, "last_failure_wall_time": None,
            "last_result": None, "last_no_effect_small_fragments": None,
            "backlog_active": False, "steady_not_before": None,
        }

    def _load_state(self) -> None:
        try:
            with self._state_path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError("unsupported state")
            if raw.get("db_identity") != self._db_identity or raw.get("table_name") != self.table_name:
                raise ValueError("different table")
            if not isinstance(raw.get("generation"), str) or not raw["generation"]:
                raise ValueError("invalid generation")
            if not isinstance(raw.get("backlog_active"), bool):
                raise ValueError("invalid backlog_active")
            if raw.get("last_result") not in {
                None, "success", "success_stats_unknown", "failure", "no_effect",
            }:
                raise ValueError("invalid last_result")
            for key in ("last_success_wall_time", "last_failure_wall_time", "steady_not_before", "last_no_effect_small_fragments"):
                value = raw.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                    raise ValueError(f"invalid {key}")
            self._state.update({key: raw.get(key, value) for key, value in self._state.items()})
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("[lance_maintenance] table=%s sidecar ignored: %s", self.table_name, exc)

    def _persist_state(self, reason: str) -> bool:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self._state, handle, sort_keys=True, separators=(",", ":"))
            tmp.replace(self._state_path)
            return True
        except OSError as exc:
            logger.warning("[lance_maintenance] table=%s sidecar save failed (%s): %s", self.table_name, reason, exc)
            return False

    def _restore_deadlines(self) -> None:
        wall_now, changed = self._wall_now(), False
        for key in ("last_success_wall_time", "last_failure_wall_time"):
            value = self._state.get(key)
            if isinstance(value, (int, float)) and value > wall_now:
                self._state[key] = wall_now
                changed = True
        # steady_not_before는 정상적으로 미래를 가리킨다. 다만 시스템 시각 역행이나
        # 손상 상태가 영구 차단을 만들지 않도록 남은 대기는 최대 설정 간격으로 제한한다.
        steady = self._state.get("steady_not_before")
        if isinstance(steady, (int, float)) and steady > wall_now + self.config.min_optimize_interval_sec:
            steady = wall_now + self.config.min_optimize_interval_sec
            self._state["steady_not_before"] = steady
            changed = True
        if changed:
            self._persist_state("clamp future clock")
        failure = self._state.get("last_failure_wall_time")
        self._steady_deadline = self._now() + max(0.0, float(steady or 0) - wall_now)
        self._failure_deadline = self._now() + max(0.0, float(failure or 0) + self.config.failure_cooldown_sec - wall_now)

    @property
    def backlog_active(self) -> bool:
        """Return the in-memory durable backlog marker."""
        return bool(self._state["backlog_active"])

    def record_mutation(self, count: int = 1) -> None:
        """Retain write telemetry without using it to decide optimization."""
        if self.config.enabled and count > 0:
            self.mutations_since_optimize += count
            self._mutations_since_fragment_check += count

    def mark_backlog_active(self) -> bool:
        """Durably mark actionable work before the first mail/document write."""
        if not self.config.enabled or self.backlog_active:
            return True
        self._state["backlog_active"] = True
        # 새 backlog의 첫 write 뒤 기존 fragment 상태를 한 번 확인한다.
        self._mutations_since_fragment_check = self._fragment_check_interval
        return self._persist_state("open backlog")

    def checkpoint_hard_limit(self, *, cancelled: Callable[[], bool] | None = None, memory_log: Callable[[str], None] | None = None) -> bool:
        """Run active-backlog hard-limit maintenance after caller saved state."""
        if not self.config.enabled or not self.backlog_active or (cancelled and cancelled()):
            return False
        # 실패 cooldown 중에는 stats 표본 예산을 소비하지 않는다. 그래야 새 write가
        # 없어도 cooldown 종료 직후 이전 실패 작업을 한 번 복구할 수 있다.
        if self._now() < self._failure_deadline:
            return False
        if self._mutations_since_fragment_check < self._fragment_check_interval:
            return False
        self._mutations_since_fragment_check = 0
        try:
            small = self._fragment_stats()["num_small_fragments"]
        except Exception as exc:
            logger.warning("[lance_maintenance] table=%s hard-limit stats failed: %s", self.table_name, exc)
            return False
        if small < self.config.backlog_hard_limit_small_fragments or not self._allowed(small):
            return False
        return self._run_optimize("backlog_hard_limit", small, cancelled, memory_log)

    def finish_backlog(self, *, completion: str, checkpoint_succeeded: bool, cancelled: Callable[[], bool] | None = None, memory_log: Callable[[str], None] | None = None) -> bool:
        """Consume a real EXHAUSTED marker after the mail state checkpoint."""
        if completion != "EXHAUSTED" or not checkpoint_succeeded or (cancelled and cancelled()):
            return False
        was_active = self.backlog_active
        self._state["backlog_active"] = False
        if not self._persist_state("close backlog"):
            return False
        if not self.config.enabled:
            return False
        if not was_active:
            # A normal completed scan still gets the steady policy at its safe
            # checkpoint; it is never treated as a backlog finalization.
            return self.checkpoint_steady(cancelled=cancelled, memory_log=memory_log)
        try:
            small = self._fragment_stats()["num_small_fragments"]
        except Exception as exc:
            logger.warning("[lance_maintenance] table=%s final stats failed: %s", self.table_name, exc)
            return False
        if small < self.config.backlog_finalize_small_fragments_reach:
            self._set_steady_gate()
            return False
        if not self._allowed(small):
            return False
        return self._run_optimize("backlog_finalize", small, cancelled, memory_log)

    def checkpoint_steady(self, *, cancelled: Callable[[], bool] | None = None, memory_log: Callable[[str], None] | None = None) -> bool:
        """Evaluate idle/completed-table maintenance after a state checkpoint."""
        if not self.config.enabled or self.backlog_active or (cancelled and cancelled()):
            return False
        try:
            small = self._fragment_stats()["num_small_fragments"]
        except Exception as exc:
            logger.warning("[lance_maintenance] table=%s steady stats failed: %s", self.table_name, exc)
            return False
        if small < self.config.optimize_when_small_fragments_reach or self._now() < self._steady_deadline or not self._allowed(small):
            return False
        return self._run_optimize("steady_small_fragments", small, cancelled, memory_log)

    def _allowed(self, small: int) -> bool:
        if self._now() < self._failure_deadline:
            return False
        no_effect = self._state.get("last_no_effect_small_fragments")
        return not isinstance(no_effect, (int, float)) or small >= no_effect + self.config.backlog_finalize_small_fragments_reach

    def _set_steady_gate(self) -> None:
        now = self._wall_now()
        self._state["steady_not_before"] = now + self.config.min_optimize_interval_sec
        self._steady_deadline = self._now() + self.config.min_optimize_interval_sec
        self._persist_state("steady gate")

    def _run_optimize(self, trigger: str, before_small: int, cancelled: Callable[[], bool] | None, memory_log: Callable[[str], None] | None) -> bool:
        if self.in_progress or (cancelled and cancelled()) or not self._allowed(before_small):
            return False
        self.in_progress = True
        try:
            if memory_log:
                memory_log(f"before_optimize_{self.table_name}")
            if cancelled and cancelled():
                return False
            self._table.optimize()
            try:
                after = self._fragment_stats()["num_small_fragments"]
            except Exception as exc:
                # optimize는 이미 성공했다. 후속 통계 실패를 optimize 실패로 기록하면
                # cooldown 뒤 같은 전체 재작성을 다시 실행할 수 있으므로 성공-통계미상으로 남긴다.
                logger.warning(
                    "[lance_maintenance] table=%s optimize 후 fragment 통계 조회 실패: %s",
                    self.table_name, exc,
                )
                after = None
            now = self._wall_now()
            self.mutations_since_optimize = 0
            self._mutations_since_fragment_check = 0
            self._state["last_success_wall_time"] = now
            self._state["last_failure_wall_time"] = None
            self._state["last_result"] = (
                "success_stats_unknown" if after is None
                else "no_effect" if after >= before_small
                else "success"
            )
            self._state["last_no_effect_small_fragments"] = (
                after if after is not None and after >= before_small else None
            )
            self._state["steady_not_before"] = now + self.config.min_optimize_interval_sec
            self._steady_deadline = self._now() + self.config.min_optimize_interval_sec
            self._failure_deadline = self._now()
            self._persist_state(f"optimize {trigger}")
            if memory_log:
                memory_log(f"after_optimize_{self.table_name}")
            logger.info(
                "[lance_maintenance] table=%s trigger=%s before_small=%d after_small=%s",
                self.table_name, trigger, before_small,
                after if after is not None else "n/a",
            )
            return True
        except Exception as exc:
            now = self._wall_now()
            self._state["last_failure_wall_time"] = now
            self._state["last_result"] = "failure"
            self._failure_deadline = self._now() + self.config.failure_cooldown_sec
            self._persist_state(f"failed {trigger}")
            logger.warning("[lance_maintenance] table=%s optimize failed (%s): %s", self.table_name, trigger, exc)
            return False
        finally:
            self.in_progress = False

    def note_external_optimize_success(self) -> None:
        """Update the same sidecar when the public Indexer.optimize API is used."""
        self.mutations_since_optimize = 0
        self._state["last_success_wall_time"] = self._wall_now()
        self._state["last_failure_wall_time"] = None
        self._state["last_result"] = "success"
        self._state["last_no_effect_small_fragments"] = None
        self._set_steady_gate()

    # Legacy entrypoints are safe aliases; mutation counts do not trigger work.
    def periodic_due(self) -> bool:
        return False

    def cycle_end_due(self) -> bool:
        return False

    def run_startup_check(self, **kwargs: Any) -> bool:
        return self.checkpoint_steady(**kwargs)

    def run_periodic(self, **kwargs: Any) -> bool:
        return self.checkpoint_hard_limit(**kwargs)

    def run_cycle_end(self, **kwargs: Any) -> bool:
        return self.checkpoint_steady(**kwargs)

    def _fragment_stats(self) -> dict[str, int]:
        stats = self._table.stats()
        fragments = stats.get("fragment_stats") if isinstance(stats, dict) else None
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
