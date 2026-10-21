"""Bounded OS-only cleanup before the application terminates its COM worker."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from knowmate.secure import office_guard

logger = logging.getLogger(__name__)
_EXIT_BUDGET_SEC = 13.0
_SWEEP_BUDGET_SEC = 5.0
_POLL_SEC = 0.05


def cleanup_before_worker_termination(worker: Any, *, budget_sec: float = _EXIT_BUDGET_SEC) -> bool:
    """Return True if unfinished cleanup/worker work requires a hard exit.

    Snapshot locks, native calls, registry cleanup and logging stay on a daemon.
    The controller never waits for their locks or calls COM. Its timeout is not
    proof that the daemon stopped: a live daemon always forces a hard exit.
    """
    started = time.monotonic()
    deadline = started + max(0.0, budget_sec)
    done = threading.Event()
    unsafe = [True]

    def run() -> None:
        attempted: set[tuple[int, int]] = set()
        targets: set[int] = set()
        confirmed: set[int] = set()
        forced: set[int] = set()
        unresolved: dict[int, str] = {}
        try:
            from knowmate.secure import ppt_shutdown
            stage = getattr(worker, "work_stage", "idle") if worker is not None else "idle"
            logger.info("[com] Office 정리 시작: phase=app_exit stage=%s budget=%.1f", stage, budget_sec)
            while time.monotonic() < deadline:
                ppt_result = ppt_shutdown.observe_exit({}, 0)
                confirmed.update(ppt_result.confirmed_exited)
                targets.update(ppt_result.confirmed_exited | ppt_result.remaining | ppt_result.failed | ppt_result.identity_unknown)
                for pid in ppt_result.confirmed_exited:
                    unresolved.pop(pid, None)
                for state in ("remaining", "identity_unknown", "failed"):
                    unresolved.update((pid, state) for pid in getattr(ppt_result, state))
                running = worker is not None and worker.isRunning()
                snapshot = office_guard.begin_owned_cleanup()
                # A Dispatch already in flight may register its verified PID late.
                # Do not repeatedly spend the budget on the same failed target.
                candidates = {
                    pid: record for pid, record in snapshot.items()
                    if not running or (pid, record.generation) not in attempted
                }
                if candidates:
                    targets.update(candidates)
                    attempted.update((pid, rec.generation) for pid, rec in candidates.items())
                    result = office_guard.cleanup_owned_processes(
                        candidates, timeout_sec=_SWEEP_BUDGET_SEC,
                        deadline=min(deadline, time.monotonic() + _SWEEP_BUDGET_SEC),
                    )
                    confirmed.update(result.confirmed_exited)
                    forced.update(result.forced_exited)
                    for pid in result.confirmed_exited:
                        unresolved.pop(pid, None)
                    for state in ("remaining", "identity_unknown", "failed"):
                        unresolved.update((pid, state) for pid in getattr(result, state))
                inflight = office_guard.shutdown_cleanup_inflight()
                running = worker is not None and worker.isRunning()
                if not running and not inflight:
                    # The worker has completed its finally block. Re-snapshot once
                    # to include registrations made during the preceding OS sweep.
                    remaining = office_guard.begin_owned_cleanup()
                    late = {pid: rec for pid, rec in remaining.items() if (pid, rec.generation) not in attempted}
                    if late:
                        continue
                    unsafe[0] = False
                    break
                done.wait(min(_POLL_SEC, max(0.0, deadline - time.monotonic())))
            remaining = office_guard.begin_owned_cleanup()
            for pid in remaining:
                unresolved.setdefault(pid, "remaining")
            logger.info(
                "[com] Office 정리 체크포인트: phase=app_exit targets=%s count=%d "
                "confirmed_exited=%s forced_exited=%s remaining=%s identity_unknown=%s failed=%s "
                "ownership_pending=%s inflight=%s timed_out=%s elapsed=%.3f",
                sorted(targets), len(targets), sorted(confirmed), sorted(forced),
                sorted(pid for pid, state in unresolved.items() if state == "remaining"),
                sorted(pid for pid, state in unresolved.items() if state == "identity_unknown"),
                sorted(pid for pid, state in unresolved.items() if state == "failed"),
                sorted(office_guard.unverified_cleanup_pending()),
                office_guard.shutdown_cleanup_inflight(), time.monotonic() >= deadline, time.monotonic() - started,
            )
        except Exception as exc:
            unsafe[0] = True
            logger.warning("[com] Office 앱 종료 정리 실패: phase=app_exit error_type=%s", type(exc).__name__)
        finally:
            try:
                from knowmate.secure.ppt_shutdown import close_observations
                close_observations()
            finally:
                done.set()

    helper = threading.Thread(target=run, name="office-app-exit", daemon=True)
    helper.start()
    done.wait(max(0.0, deadline - time.monotonic()))
    helper.join(max(0.0, deadline - time.monotonic()))
    # No logger call here: the daemon could be waiting on a logging handler lock.
    return helper.is_alive() or not done.is_set() or unsafe[0]
