"""Primitive-only bounded supervision independent of COM caller completion."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .bundle import Bundle, primitive
from .report import summarize
from .transport import CollectorRecords, JsonlTail


class Backend(Protocol):
    """Secure backend operations never expose COM or native handles to this module."""

    def start(self, bundle: Bundle) -> dict[str, Path] | None:
        """Prepare transport resources and start only the independent collector."""

    def tick(self, collector: CollectorRecords, controller: list[dict[str, Any]]) -> None:
        """Advance ready/ownership handshakes without blocking for child return."""

    def binding(self) -> dict[str, Any]:
        """Return the one exact target identity, or zero PID before launch."""

    def observe(self) -> list[dict[str, Any]]:
        """Poll retained handles and completed asynchronous OS probes only."""

    def health(self) -> str | None:
        """Report a definite collector/caller failure without waiting for either."""

    def stop(self, reason: str) -> None:
        """Signal cancellation and final drain without killing or waiting for children."""

    def close(self) -> dict[str, Any]:
        """Close owned observations and return primitive child liveness."""

    def release_output(self) -> None:
        """Release optional output leases after every final bundle write has finished."""


def capture(root: Path, manifest: dict[str, Any], backend: Backend,
            cancelled: Callable[[], bool] = lambda: False,
            now: Callable[[], int] = time.perf_counter_ns,
            pause: Callable[[float], None] = time.sleep,
            duration_ms: int = 180_000, drain_ms: int = 1500) -> dict[str, Any]:
    """Collect one target until exact exit/deadline, then bound the final drain."""
    if not 0 < duration_ms <= 180_000 or not 0 <= drain_ms <= 2000:
        raise ValueError("capture_duration")
    bundle = Bundle(root)
    bundle.write("manifest.json", primitive(manifest))
    bundle.stream("host.jsonl")
    bundle.stream("controller.jsonl")
    os_stream = bundle.stream("os.jsonl")
    host_tail, control_tail = JsonlTail(root / "host.jsonl"), JsonlTail(root / "controller.jsonl")
    records = CollectorRecords(manifest["run_id"])
    controller: list[dict[str, Any]] = []
    os_events: list[dict[str, Any]] = []
    issues: list[str] = []
    reason = "deadline"
    started = now()
    deadline = started + duration_ms * 1_000_000
    request_started: int | None = None
    stopped = False
    admission_open = True
    closure_time: int | None = None
    next_flush = started
    liveness: dict[str, Any] = {}
    def stop_safely(why: str) -> None:
        nonlocal admission_open
        admission_open = False
        try:
            backend.stop(why)
        except Exception as error:
            issues.append("stop_" + type(error).__name__)

    def bound(row: dict[str, Any], current: int) -> bool:
        identity = backend.binding()
        return (identity.get("pid", 0) > 0 and all(type(row.get(k)) is type(v) and row.get(k) == v for k, v in identity.items())
                and type(row.get("qpc_ns")) is int and started <= row["qpc_ns"] <= current)
    try:
        transport_paths = backend.start(bundle)
        if transport_paths is not None:
            host_tail = JsonlTail(transport_paths["host.jsonl"])
            control_tail = JsonlTail(transport_paths["controller.jsonl"])
        while True:
            current = now()
            if not stopped and (cancelled() or current >= deadline):
                stop_safely("admission_closed")
            try:
                host_rows = host_tail.poll()
                control_rows = control_tail.poll()
                if transport_paths is not None:
                    for name, rows in (("host.jsonl", host_rows), ("controller.jsonl", control_rows)):
                        output_stream = bundle._streams[name]
                        for row in rows:
                            bundle.emit(output_stream, row)
                        # Parent fsync completes before the request admission
                        # event. A late child can only alter its private spool.
                        if rows:
                            output_stream.flush()
                            os.fsync(output_stream.fileno())
                for row in host_rows:
                    records.accept(row)
                controller.extend(control_rows)
                if request_started is None:
                    begins = [r["qpc_ns"] for r in controller if r.get("event") == "request_begin" and bound(r, current)]
                    if begins:
                        request_started = begins[0]
                        # A request has its own maximum interval; pre-request hangs
                        # are bounded separately from initial startup.
                        deadline = min(current + duration_ms * 1_000_000, request_started + duration_ms * 1_000_000)
                failure = backend.health()
                if failure and failure not in issues:
                    issues.append(failure)
                    stop_safely(failure)
                if admission_open and not stopped:
                    backend.tick(records, controller)
            except Exception as error:
                issues.append("supervisor_" + type(error).__name__)
                stop_safely("collector_failed")
            try:
                for row in ([] if stopped else backend.observe()):
                    value = primitive(row)
                    os_events.append(value)
                    bundle.emit(os_stream, value)
                current = now()
                failure = backend.health()
                if failure and failure not in issues:
                    issues.append(failure)
                    # A failed collector forbids further COM while OS observation
                    # still lasts until the original target's exit/deadline.
                    stop_safely(failure)
                exited = any(r.get("event") == "target_state" and r.get("wait_result") == 0
                             and r.get("retained_handle") is True and r.get("identity_matches") is True
                             and bound(r, current)
                             for r in os_events[-10:])
            except Exception as error:
                issues.append("supervisor_" + type(error).__name__)
                stop_safely("collector_failed")
                exited = False
            if current >= next_flush:
                try:
                    os_stream.flush()
                    os.fsync(os_stream.fileno())
                except Exception as error:
                    issues.append("os_persist_" + type(error).__name__)
                    stop_safely("collector_failed")
                next_flush = current + 1_000_000_000
            if not stopped and (exited or cancelled() or current >= deadline):
                reason = "target_exit" if exited else "cancelled" if cancelled() else "deadline"
                closure_time = current
                stopped = True
                stop_safely(reason)
                deadline = current + drain_ms * 1_000_000
            if stopped and (records.ended or current >= deadline):
                break
            pause(0.02)
    except Exception as error:
        issues.append("startup_" + type(error).__name__)
        reason = "blocked"
        closure_time = now()
        stop_safely(reason)
    finally:
        try:
            liveness = backend.close()
        except Exception as error:
            issues.append("cleanup_" + type(error).__name__)
            liveness = dict(available=False, error_type=type(error).__name__)
    binding = backend.binding()
    if not records.ended:
        issues.append("collector_final_drain_unconfirmed")
    if host_tail.partial or control_tail.partial:
        issues.append("incomplete_transport_line")
    try:
        host = records.host(binding)
    except ValueError:
        host = []
        issues.append("collector_foreign_target")
    # The drain can recover events published just before capture closure, but
    # records after closure remain explicitly outside the observed interval.
    closure = dict(reason=reason, qpc_ns=closure_time if closure_time is not None else now(),
                   children=primitive(liveness), request_started_qpc_ns=request_started)
    result = summarize(binding, records.header, host, controller, os_events, closure,
                       records.persisted_seq, issues)
    try:
        bundle.write("summary.json", result)
        bundle.write("coverage.json", result["coverage"])
        bundle.finish()
    finally:
        release = getattr(backend, "release_output", None)
        if release is not None:
            release()
    return result
