"""Finite primitive supervision that never waits for a COM caller to return."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from scripts.diagnostics.ppt_capture.bundle import Bundle
from .evidence import ARMS, summarize, validate_binding, validate_record


class Backend(Protocol):
    """Native resources remain inside a secure backend, outside this supervisor."""

    def start(self, bundle: Bundle) -> dict[str, Any]:
        """Start one fresh target/caller and return a scalar target binding."""

    def controller(self) -> list[dict[str, Any]]:
        """Read only complete available records without waiting for EOF."""

    def observe(self) -> dict[str, Any]:
        """Poll the retained original handle without COM."""

    def close(self) -> dict[str, Any]:
        """Close observations without killing or joining caller or target."""


def capture_arm(output: Path, arm: str, backend: Backend, *, mode: str = "live",
                now: Callable[[], int] = time.perf_counter_ns,
                pause: Callable[[float], None] = time.sleep, duration_ms: int = 180_000) -> dict[str, Any]:
    """Bound collection of one target independently of Quit and caller completion."""
    if arm not in ARMS or mode not in {"live", "mock"} or not 0 < duration_ms <= 180_000:
        raise ValueError("lifetime_capture_parameters")
    bundle = Bundle(output)
    control = bundle.stream("controller.jsonl")
    os_stream = bundle.stream("os.jsonl")
    controller: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    liveness: dict[str, Any] = {}
    incomplete = False
    manifest: dict[str, Any] = {}
    try:
        binding = backend.start(bundle)
        validate_binding(binding)
        if binding.get("arm") != arm:
            raise ValueError("lifetime_backend_arm")
        manifest = dict(schema=1, variant="lifetime_evidence", mode=mode, binding=binding)
        bundle.write("manifest.json", manifest)
        deadline = now() + duration_ms * 1_000_000
        exit_deadline: int | None = None
        while True:
            current = now()
            for row in backend.controller():
                validate_record(row, binding)
                controller.append(row)
                bundle.emit(control, row)
            try:
                row = backend.observe()
                validate_record(row, binding)
            except Exception as error:
                incomplete = True
                row = dict(binding, event="observer_failed", qpc_ns=current,
                           error_type=type(error).__name__ if type(error).__name__ in {"OSError", "ValueError", "TypeError", "RuntimeError"} else "unknown")
            observations.append(row)
            bundle.emit(os_stream, row)
            for stream in (control, os_stream):
                stream.flush()
                os.fsync(stream.fileno())
            if row.get("status") == "exited" and exit_deadline is None:
                exit_deadline = min(deadline, current + 1_500_000_000)
            if current >= deadline or (exit_deadline is not None and
                                       (current >= exit_deadline or
                                        (any(r["event"] == "caller_complete" for r in controller)
                                         and getattr(backend, "caller_finished", lambda: False)()))):
                break
            pause(0.05)
    except Exception as error:
        incomplete = True
        if manifest:
            row = dict(manifest["binding"], event="observer_failed", qpc_ns=now(),
                       error_type=type(error).__name__ if type(error).__name__ in {"OSError", "ValueError", "TypeError", "RuntimeError"} else "unknown")
            observations.append(row)
            bundle.emit(os_stream, row)
        else:
            raise
    finally:
        try:
            liveness = backend.close()
        except Exception:
            incomplete = True
        finally:
            # A start failure still leaves a bounded, flushed partial bundle.
            if not manifest:
                bundle.write("summary.json", dict(schema=1, status="target_unconfirmed", live_approved=False))
                bundle.finish()
    try:
        result = summarize(manifest, controller, observations, liveness.get("caller_alive"),
                           incomplete or bool(liveness.get("incomplete_transport")))
    except (ValueError, TypeError):
        result = dict(schema=1, binding=manifest["binding"], mode=mode, os_exit="observation_failed",
                      actual_os_exit_verified=False, caller_alive=liveness.get("caller_alive"),
                      evidence_incomplete=True, live_approved=False)
    bundle.write("summary.json", result)
    bundle.finish()
    return result


def compare(output: Path, factory: Callable[[str], Backend], *, mode: str = "live",
            duration_ms: int = 180_000) -> dict[str, Any]:
    """Never start the second arm while the preceding target/caller remains unresolved."""
    from scripts.diagnostics.ppt_capture.bundle import reject_links
    reject_links(output)
    output.mkdir(mode=0o700, exist_ok=False)
    results: dict[str, Any] = {}
    for arm in ARMS:
        results[arm] = capture_arm(output / arm, arm, factory(arm), mode=mode, duration_ms=duration_ms)
        if results[arm]["os_exit"] != "exited" or results[arm]["caller_alive"] is not False:
            break
    comparison = dict(schema=1, mode=mode, arms=results,
                      unexecuted=[arm for arm in ARMS if arm not in results],
                      addin_unloaded_controls="UNEXECUTED", live_approved=False)
    summary_bundle = Bundle(output / "comparison")
    summary_bundle.write("summary.json", comparison)
    summary_bundle.finish()
    return comparison
