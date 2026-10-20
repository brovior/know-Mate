"""Conservative capture coverage and exit reporting without any Office access."""
from __future__ import annotations

from typing import Any

from .layout import CAPACITY, STAGES


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def summarize(binding: dict[str, Any], header: dict[str, Any] | None,
              host: list[dict[str, Any]], controller: list[dict[str, Any]],
              os_events: list[dict[str, Any]], closure: dict[str, Any],
              persisted_seq: int, issues: list[str] | None = None) -> dict[str, Any]:
    """Keep request return, capture closure, OS exit, and evidence loss separate."""
    problems = list(issues or [])
    if header is None:
        problems.append("host_header_missing")
    else:
        if any(header.get(key) != binding.get(key) for key in ("run_id", "pid", "creation")):
            problems.append("host_binding")
        published = header.get("published_seq")
        if not _integer(published) or published > CAPACITY:
            problems.append("published_sequence")
        elif [r.get("seq") for r in host] != list(range(1, published + 1)):
            problems.append("missing_or_duplicate_host_sequence")
        if not _integer(persisted_seq) or persisted_seq != published:
            problems.append("persisted_tail_mismatch")
        for key in ("dropped", "offthread", "append_reentry"):
            if not _integer(header.get(key)) or header[key]:
                problems.append(key)
    for row in host + controller + os_events:
        if any(row.get(key) != binding.get(key) for key in ("run_id", "pid", "creation")):
            problems.append("record_binding")
            break
    if closure.get("reason") not in {"target_exit", "deadline", "cancelled", "blocked", "collector_failed", "caller_failed"}:
        problems.append("capture_end_reason")
    end = closure.get("qpc_ns")
    if not _integer(end):
        problems.append("capture_end_time")
    def valid(row: dict[str, Any]) -> bool:
        return (all(row.get(key) == binding.get(key) for key in ("run_id", "pid", "creation"))
                and _integer(row.get("qpc_ns")) and _integer(end) and row["qpc_ns"] <= end)
    valid_controller = [r for r in controller if valid(r)]
    valid_host = [r for r in host if valid(r)]
    valid_os = [r for r in os_events if valid(r)]
    if len(valid_controller) != len(controller) or len(valid_host) != len(host) or len(valid_os) != len(os_events):
        problems.append("foreign_or_out_of_capture_record")
    exact = [r for r in valid_os if r.get("event") == "target_state"
             and r.get("retained_handle") is True and r.get("identity_matches") is True
             and all(r.get(key) == binding.get(key) for key in ("run_id", "pid", "creation"))]
    exits = [r for r in exact if type(r.get("wait_result")) is int and r["wait_result"] == 0]
    os_exit = True if exits else False if exact and type(exact[-1].get("wait_result")) is int and exact[-1]["wait_result"] == 258 else None
    request_return = [r for r in valid_controller if r.get("event") == "request_return"]
    not_requested = any(r.get("event") == "request_not_run" for r in valid_controller)
    request_complete = True if request_return else False if not_requested else None
    drop = [r.get("qpc_ns") for r in valid_controller if r.get("event") == "controller_refs_drop_end"]
    uninit = [r.get("qpc_ns") for r in valid_controller if r.get("event") == "CoUninitialize_end"]
    post_ms = None
    post_reason = "cleanup_not_observed"
    if drop and uninit and exact:
        start = max(drop[-1], uninit[-1], min(r["qpc_ns"] for r in exact))
        stop = min([end, max(r["qpc_ns"] for r in exact)] + [r["qpc_ns"] for r in exits])
        if start <= stop:
            post_ms = (stop - start) / 1_000_000
            post_reason = "retained_handle_observed_interval"
        else:
            post_reason = "no_os_observation_after_cleanup"
    loss = bool(problems)
    by_stage = {
        stage: {"reached": "yes" if any(row.get("stage") == stage for row in valid_host) else "unknown",
                "observations": [row for row in valid_host if row.get("stage") == stage]}
        for stage in STAGES
    }
    # An explicit controller branch proves only its own not-run decision, not
    # the absence of later host/lifecycle callbacks.
    decisions = [r for r in valid_controller if r.get("event") == "request_not_run"]
    return dict(schema=3, binding=dict(binding), request_complete=request_complete,
                request_calls=sum(r.get("event") == "request_begin" for r in valid_controller),
                quit_calls=sum(r.get("stage") == "Quit" and r.get("outcome") == "Entered" for r in valid_host)
                + sum(r.get("event") == "quit_begin" for r in valid_controller),
                host_quit_calls_observed=sum(r.get("stage") == "Quit" and r.get("outcome") == "Entered" for r in valid_host),
                controller_quit_calls_observed=sum(r.get("event") == "quit_begin" for r in valid_controller),
                counts_are_observed_lower_bounds=True,
                capture_closed=dict(closure), os_exit=os_exit, evidence_loss=loss,
                evidence_issues=sorted(set(problems)), published_seq=None if header is None else header.get("published_seq"),
                persisted_seq=persisted_seq, counters={} if header is None else {
                    key: header.get(key) for key in ("dropped", "offthread", "append_reentry", "counters_are_lower_bounds")},
                post_cleanup_observed_ms=post_ms, post_cleanup_reason=post_reason,
                coverage={"scope": "captured_interval", "complete": not loss,
                          "host_capture_scope": None if header is None else header.get("capture_scope", "unknown"),
                          "host_capture_start_ticks": None if header is None else header.get("capture_start_ticks"),
                          "pre_attach_interpretation": "unknown",
                          "host_lifecycle_complete": bool(not loss and os_exit),
                          "absent_stage_interpretation": "unknown", "stages": by_stage,
                          "explicit_controller_decisions": decisions})
