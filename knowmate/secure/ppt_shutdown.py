"""Process-wide scalar PPT provenance and graceful shutdown admission.

COM references belong exclusively to the creating worker's TLS. This module never
stores them, kills Office, or recreates a COM connection to finish cleanup.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)
_EXE = "POWERPNT.EXE"
_lock = threading.RLock()
_sequence = 0


@dataclass(frozen=True)
class LaunchTicket:
    """Immutable process origin; pending tickets survive worker replacement."""
    generation: int
    owner_thread: int
    state: str = "launching"
    pid: int | None = None
    identity: int | None = None
    proof_generation: int | None = None


_ticket: LaunchTicket | None = None


@dataclass
class _ObservationTarget:
    observer: Any
    record: Any
    yielded: bool = False


_observations: dict[int, _ObservationTarget] = {}


def _yield_observation(ticket: LaunchTicket) -> None:
    with _lock:
        target = _observations.get(ticket.generation)
        if target is not None:
            target.yielded = True


def close_observations() -> None:
    """Release retained OS handles on application exit without claiming process exit."""
    with _lock:
        targets = tuple(_observations.items())
        _observations.clear()
    for generation, target in targets:
        result = target.observer.poll()
        target.observer.close()
        logger.info("[ppt] observation released: generation=%s pid=%s status=%s yielded=%s",
                    generation, target.observer.pid, result.status, target.yielded)


def _finish_observation(generation: int, target: _ObservationTarget) -> None:
    from knowmate.secure import office_guard as guard
    global _ticket
    with _lock:
        if _observations.get(generation) is not target:
            return
        _observations.pop(generation)
        if _ticket is not None and _ticket.generation == generation:
            _ticket = None
    if not target.yielded:
        with guard._owned_lock:
            current = guard._owned_pids.get(target.observer.pid)
        if (current is not None and current.generation == target.record.generation
                and current.creation_identity == target.record.creation_identity):
            guard._remove_owned_if_matches(target.observer.pid, current)


def admission_reason() -> str | None:
    """Block new dispatch while launch or a single quit outcome is unresolved."""
    with _lock:
        current = _ticket
        if current is None or current.state == "ready":
            return None
        if current.state == "launching" and current.owner_thread == threading.get_ident():
            return None  # the one exact ticket holder may perform the initial bind
        return "ppt_launch_pending" if current.state == "launching" else "ppt_quit_pending"


def _set_ticket(expected: LaunchTicket, **changes: Any) -> LaunchTicket | None:
    global _ticket
    with _lock:
        if _ticket is not expected:
            return None
        _ticket = replace(expected, **changes)
        return _ticket


def reset_for_tests() -> None:
    """Reset admission and release retained handles without touching Office."""
    global _ticket
    with _lock:
        _ticket = None
        targets = tuple(_observations.values())
        _observations.clear()
    for target in targets:
        target.observer.close()


def observe_pending() -> None:
    """Resolve unknown outcomes using fresh OS identity, without any COM RPC."""
    from knowmate.secure import office_guard as guard
    global _ticket
    with _lock:
        current = _ticket
    if current is None or current.state in {"ready", "launching"}:
        return  # an in-flight CreateProcess is never resolved by a vacant OS list
    with _lock:
        target = _observations.get(current.generation)
    if target is not None:
        result = target.observer.poll()
        if result.status == "exited":
            _finish_observation(current.generation, target)
            return
        if result.status == "timeout":
            return
    processes = guard._enumerate_processes()
    if processes is None:
        return
    pids = guard._pids_for(_EXE, processes)
    gone = not pids if current.pid is None else current.pid not in pids
    if not gone and current.identity is not None:
        identity = guard._process_creation_identity(current.pid)
        gone = identity is not None and identity != current.identity
    if gone:
        with _lock:
            if _ticket is current:
                _ticket = None
                discarded = _observations.pop(current.generation, None)
            else:
                discarded = None
        if discarded is not None:
            discarded.observer.close()
        guard.prune_released_nonterminable_processes()


def ensure_admitted() -> None:
    """Require previous launch/quit observations to resolve before new COM."""
    from knowmate.secure.office_guard import OfficeCleanupPendingError
    observe_pending()
    reason = admission_reason()
    if reason:
        raise OfficeCleanupPendingError(_EXE, reason)


def launch_and_bind(win32com: Any, bitness: int, *, registration_timeout_sec: float | None = None) -> tuple[Any, tuple[int, Any], LaunchTicket] | None:
    """Launch once under the caller's machine lock, then verify exact COM host."""
    from knowmate.secure import office_guard as guard
    from knowmate.secure.ppt_launch import (
        LaunchedPowerPoint,
        PptLaunchNotStarted,
        trusted_powerpoint_path,
    )
    global _ticket, _sequence
    guard.ensure_application_open(_EXE)
    ensure_admitted()
    try:
        path = trusted_powerpoint_path(bitness)
    except (OSError, ValueError, ImportError) as exc:
        # Only fixed validation codes and scalar OS errors belong in this log.
        # Exception text can contain environment paths or other private data.
        detail = "validation_failed"
        error_type = "ValueError"
        if isinstance(exc, OSError):
            detail, error_type = "os_error", "OSError"
        elif isinstance(exc, ImportError):
            detail, error_type = "dependency_unavailable", "ImportError"
        elif len(exc.args) == 1 and isinstance(exc.args[0], str) and exc.args[0] in {
                "office_bitness", "office_path", "office_path_link", "office_image",
                "office_path_expansion"}:
            detail = exc.args[0]
        winerror = getattr(exc, "winerror", None)
        logger.info("[ppt] launch decision: path=dispatch reason=untrusted_path "
                    "detail=%s error_type=%s winerror=%s", detail, error_type,
                    winerror if type(winerror) is int else None)
        return None  # no process call was started; ordinary read-only dispatch remains valid
    baseline, _ = guard.prepare_powerpoint_dispatch()
    if baseline:
        logger.info("[ppt] launch decision: path=dispatch reason=existing_host")
        return None  # an existing read proof never acquires direct-launch authority
    with _lock:
        if _ticket is not None:
            raise guard.OfficeCleanupPendingError(_EXE, "ppt_launch_pending")
        _sequence += 1
        ticket = _ticket = LaunchTicket(_sequence, threading.get_ident())
    launched = None
    observer = None
    observer_attached = False
    started_call = False
    try:
        guard.ensure_application_open(_EXE)
        # CreateProcess failure is known not to create a child. Once it returns,
        # any bootstrap exit or mismatched COM host remains an unresolved ticket.
        try:
            started_call = True
            launched = LaunchedPowerPoint(path)
        except PptLaunchNotStarted:
            logger.info("[ppt] launch decision: path=dispatch reason=create_not_started")
            _set_ticket(ticket, state="launch_failed")
            with _lock:
                if _ticket is not None and _ticket.generation == ticket.generation:
                    _ticket = None
            return None
        ticket = _set_ticket(ticket, pid=launched.pid, identity=launched.identity)
        if ticket is None or ticket.identity is None:
            raise guard.OfficeCleanupPendingError(_EXE, "launch_identity_unknown")
        observer = launched.detach_observation()
        from knowmate.config import ppt_registration_timeout_seconds
        from knowmate.secure.ppt_active import wait_for_active_app

        def check_target() -> None:
            guard.ensure_application_open(_EXE)
            with _lock:
                if _ticket is not ticket:
                    raise guard.OfficeCleanupPendingError(_EXE, "launch_ticket_changed")
            if observer.poll().status != "timeout":
                raise guard.OfficeCleanupPendingError(_EXE, "launch_original_exited")
            processes = guard._enumerate_processes()
            if processes is None or guard._pids_for(_EXE, processes) != {ticket.pid}:
                raise guard.OfficeCleanupPendingError(_EXE, "launch_host_ambiguous")
            if guard._process_creation_identity(ticket.pid) != ticket.identity:
                raise guard.OfficeCleanupPendingError(_EXE, "launch_identity_changed")

        timeout = ppt_registration_timeout_seconds(
            {} if registration_timeout_sec is None else {"ppt_registration_timeout_sec": registration_timeout_sec}
        )
        app = wait_for_active_app(win32com, check_target, timeout)
        guard.ensure_application_open(_EXE)
        app.DisplayAlerts = 2
        if app.DisplayAlerts != 2:
            raise guard.OfficeCleanupPendingError(_EXE, "ppt_alerts_unconfirmed")
        # Reuse the existing bounded HWND probe, but permit only our exact child.
        with guard._ownership_window(_EXE, app) as read_hwnd:
            def exact_hwnd() -> int | None:
                hwnd = read_hwnd()
                bound_pid = guard._pid_from_hwnd(hwnd)
                if bound_pid != ticket.pid:
                    logger.info("[ppt] launch ownership: reason=%s expected_pid=%s observed_pid=%s",
                                "hwnd_missing" if hwnd is None else "pid_mismatch", ticket.pid, bound_pid)
                    return None
                procs = guard._enumerate_processes()
                if procs is None:
                    logger.info("[ppt] launch ownership: reason=process_enumeration_unavailable expected_pid=%s", ticket.pid)
                    return None
                live_pids = guard._pids_for(_EXE, procs)
                if live_pids != {ticket.pid}:
                    logger.info("[ppt] launch ownership: reason=process_set_mismatch expected_pid=%s expected_present=%s",
                                ticket.pid, ticket.pid in live_pids)
                    return None
                identity = guard._process_creation_identity(ticket.pid)
                if identity != ticket.identity:
                    logger.info("[ppt] launch ownership: reason=identity_mismatch expected_pid=%s identity_available=%s",
                                ticket.pid, identity is not None)
                    return None
                return hwnd
            proof = guard._register_owned_window(_EXE, set(), exact_hwnd, {})
        if proof is None or proof[0] != ticket.pid or proof[1].creation_identity != ticket.identity:
            raise guard.OfficeCleanupPendingError(_EXE, "launch_host_mismatch")
        guard.ensure_application_open(_EXE)
        ready = _set_ticket(ticket, state="ready", proof_generation=proof[1].generation)
        if ready is None:
            raise guard.OfficeCleanupPendingError(_EXE, "launch_ticket_changed")
        with _lock:
            attached = _ticket is ready
            if attached:
                _observations[ready.generation] = _ObservationTarget(observer, proof[1])
                observer_attached = True
        if not attached:
            observer.close()
            raise guard.OfficeCleanupPendingError(_EXE, "launch_ticket_changed")
        logger.info("[ppt] launch decision: path=direct reason=ready pid=%d generation=%d owner_thread=%d",
                    ready.pid, ready.generation, ready.owner_thread)
        return app, proof, ready
    except BaseException:
        with _lock:
            current = _ticket
        if current is not None and current.generation == (ticket.generation if ticket else -1):
            _set_ticket(current, state="pending")
        if not started_call:
            # application_closing before CreateProcess has no child to observe.
            with _lock:
                if _ticket is current:
                    _ticket = None
        raise
    finally:
        if observer is not None and not observer_attached:
            observer.close()
        if launched is not None:
            launched.close()


def _exact_origin(ticket: LaunchTicket | None, proof: Any) -> bool:
    if ticket is None or proof is None:
        return False
    pid, record = proof
    with _lock:
        return (_ticket is ticket and ticket.state == "ready"
                and ticket.owner_thread == threading.get_ident() and ticket.pid == pid
                and ticket.identity == record.creation_identity
                and ticket.proof_generation == record.generation)


def abandon(ticket: LaunchTicket | None) -> None:
    """Poisoned or interrupted hosts permit only later OS observation."""
    if ticket is not None:
        _set_ticket(ticket, state="pending")


def request_quit(app: Any, proof: Any, ticket: LaunchTicket | None, cache: Any,
                 poisoned: bool = False) -> tuple[str, dict[int, Any]]:
    """Request one host-guarded Quit; return an OS-only observation snapshot."""
    global _ticket
    from knowmate.secure import office_guard as guard
    from knowmate.secure.ppt_addin_reader import PptAddinUnavailable, select_service
    from knowmate.secure.ppt_installation import MachineReadGuard
    from knowmate.secure.ppt_policy import auto_shutdown_enabled
    if not _exact_origin(ticket, proof):
        logger.info(
            "[ppt] quit decision: reason=origin_mismatch ticket_present=%s proof_present=%s",
            ticket is not None, proof is not None,
        )
        return "not_supported", {}
    pid, record = proof
    if poisoned:
        abandon(ticket)
        return "pending", {pid: record}
    if not auto_shutdown_enabled():
        logger.info("[ppt] quit decision: reason=policy_disabled")
        abandon(ticket)
        return "not_supported", {}
    processes = guard._enumerate_processes()
    if (guard._process_creation_identity(pid) != record.creation_identity
            or processes is None or pid not in guard._pids_for(_EXE, processes)):
        abandon(ticket)
        return "identity_unknown", {pid: record}
    try:
        # App exit forbids new activation, while this same-worker exact target
        # may still finish graceful cleanup. Never clear the global closing gate.
        with guard._owned_lock:
            current = guard._owned_pids.get(pid)
            if (current is None or not guard._record_matches_unlocked(current, record)
                    or current.cleanup_pending or guard._unverified_cleanup.get(_EXE)
                    or guard._cleanup_inflight.get(_EXE)):
                raise guard.OfficeCleanupPendingError(_EXE, "cleanup_target_changed")
        with MachineReadGuard():
            try:
                service = select_service(app, cache)
            except PptAddinUnavailable:
                logger.info("[ppt] quit decision: reason=service_unavailable")
                guard._remove_owned_if_matches(pid, record)
                _yield_observation(ticket)
                with _lock:
                    if _ticket is ticket:
                        _ticket = None
                return "not_supported", {}
            # Service compatibility and the exact read proof are separate from
            # launch authority. Publish pending before making a potentially hung RPC.
            pending = _set_ticket(ticket, state="pending")
            if pending is None:
                return "pending", {pid: record}
            snapshot_record = guard._mark_owned_cleanup_pending(pid, record)
            if snapshot_record is None:
                return "pending", {pid: record}
            snapshot = {pid: snapshot_record}
            try:
                logger.info("[ppt] quit decision: reason=service_call pid=%d generation=%d", pid, ticket.generation)
                from knowmate.secure.ppt_caller_quit import request_guarded_quit

                def check_quit_target() -> None:
                    with _lock:
                        target = _observations.get(pending.generation)
                        if _ticket is not pending or target is None:
                            raise guard.OfficeCleanupPendingError(_EXE, "quit_target_changed")
                        alive = target.observer.poll().status == "timeout"
                    with guard._owned_lock:
                        same_record = guard._owned_pids.get(pid) is snapshot_record
                    processes = guard._enumerate_processes()
                    if (not alive or not same_record or processes is None
                            or guard._pids_for(_EXE, processes) != {pid}
                            or guard._process_creation_identity(pid) != record.creation_identity):
                        raise guard.OfficeCleanupPendingError(_EXE, "quit_target_changed")

                status = request_guarded_quit(service, app, check_quit_target)
            except Exception as exc:  # noqa: BLE001 -- opaque COM/OS failures remain pending
                logger.warning("[ppt] 정상 종료 RPC 결과 불명: error_type=%s", type(exc).__name__)
                return "pending", snapshot
            logger.info("[ppt] quit decision: reason=service_return status=%s",
                        status if isinstance(status, str) and status in {
                            "protected", "not_supported", "failed", "quit_requested"
                        } else "unknown")
            if status in {"protected", "not_supported", "failed"}:
                # Protected includes a safely cancelled Quit. Yield this host to
                # the user; the OS busy guard protects it thereafter.
                guard._remove_owned_if_matches(pid, snapshot_record)
                _yield_observation(ticket)
                with _lock:
                    if _ticket is pending:
                        _ticket = None
                return status, {}
            return ("quit_requested" if status == "quit_requested" else "pending"), snapshot
    except Exception as exc:  # noqa: BLE001 -- opaque COM/OS failures remain pending
        abandon(ticket)
        logger.warning("[ppt] 정상 종료 준비 보류: error_type=%s", type(exc).__name__)
        return "pending", {pid: record}


def observe_exit(snapshot: dict[int, Any], timeout: float, wait_fn: Any = None) -> Any:
    """Confirm actual exit after worker references are released; never kill."""
    from knowmate.secure import office_guard as guard
    deadline = time.monotonic() + max(0.0, timeout)
    with _lock:
        targets = tuple(_observations.items())
    confirmed: set[int] = set()
    remaining: set[int] = set()
    failed: set[int] = set()
    unknown: set[int] = set()
    covered: set[int] = set()
    for generation, target in targets:
        pid = target.observer.pid
        covered.add(pid)
        result = target.observer.poll() if target.yielded else target.observer.wait_until(deadline)
        logger.info("[ppt] process observation: generation=%s pid=%s status=%s error_code=%s yielded=%s",
                    generation, pid, result.status, result.error_code, target.yielded)
        if result.status == "exited":
            _finish_observation(generation, target)
            if not target.yielded:
                confirmed.add(pid)
        elif not target.yielded:
            if result.status == "timeout":
                remaining.add(pid)
            elif result.status == "target_unconfirmed":
                unknown.add(pid)
            else:
                failed.add(pid)
    legacy = {pid: record for pid, record in snapshot.items() if pid not in covered}
    try:
        if legacy:
            unresolved, _ = (wait_fn or guard.wait_for_owned_exit)(legacy, max(0.0, deadline - time.monotonic()))
            guard.finish_owned_cleanup(legacy, set(unresolved))
            confirmed.update(set(legacy) - set(unresolved))
            remaining.update(unresolved)
        observe_pending()
        return guard.OfficeCleanupResult(confirmed_exited=frozenset(confirmed),
                                        remaining=frozenset(remaining), failed=frozenset(failed),
                                        identity_unknown=frozenset(unknown))
    except Exception as exc:  # noqa: BLE001 -- opaque COM/OS failures remain pending
        logger.warning("[ppt] 실제 종료 관찰 실패: error_type=%s", type(exc).__name__)
        return guard.OfficeCleanupResult(confirmed_exited=frozenset(confirmed),
                                        remaining=frozenset(remaining), failed=frozenset(failed),
                                        identity_unknown=frozenset(set(legacy) | unknown))


def pending_cleanup_result() -> Any:
    """Report scalar launch/quit uncertainty after all worker COM refs are gone."""
    from knowmate.secure import office_guard as guard
    observe_pending()
    with _lock:
        current = _ticket
    if current is None or current.state == "ready":
        return guard.OfficeCleanupResult()
    if current.pid is None or current.state == "launching":
        return guard.OfficeCleanupResult(ownership_pending=frozenset({_EXE}))
    with _lock:
        target = _observations.get(current.generation)
    if target is not None:
        result = target.observer.poll()
        if result.status == "exited":
            _finish_observation(current.generation, target)
            return guard.OfficeCleanupResult(confirmed_exited=frozenset({current.pid}))
        if result.status == "observation_failed":
            return guard.OfficeCleanupResult(failed=frozenset({current.pid}))
        if result.status == "target_unconfirmed":
            return guard.OfficeCleanupResult(identity_unknown=frozenset({current.pid}))
        return guard.OfficeCleanupResult(remaining=frozenset({current.pid}))
    processes = guard._enumerate_processes()
    identity = guard._process_creation_identity(current.pid)
    if processes is None or identity is None or current.identity is None:
        return guard.OfficeCleanupResult(identity_unknown=frozenset({current.pid}))
    # observe_pending performed the identity-aware resolution; a concurrent exit
    # can conservatively remain pending until the next OS-only observation.
    return guard.OfficeCleanupResult(remaining=frozenset({current.pid}))
