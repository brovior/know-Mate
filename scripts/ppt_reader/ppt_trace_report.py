"""Read bounded external diagnostic snapshots without contacting Office.

Missing output is evidence loss, never evidence that a guard stage succeeded.
Controller and OS observer records should carry the same run UUID, PID creation,
and perf_counter_ns timestamp (Windows QPC, normalized to nanoseconds).
"""
from pathlib import Path

STAGES = ("Bind Prepare Admission Identity IdentityRelease Guard Resolve Sink Advise "
          "AlertsSet AlertsRead Presentations ProtectedViews SlideShows Poll Commit Ack "
          "Completion Cancel Disconnect Unadvise Release Capabilities CapabilityClose "
          "TimerCleanup ProtocolCleanup PrimaryError CleanupError FatalError Terminal "
          "Generation Reading Owner DeferredDepth Quit").split()


def read_terminal_trace(directory: Path, run_id: str, pid: int, creation: int) -> dict:
    """Validate immutable frames and explicitly report missing or partial evidence."""
    frames = []
    issues = []
    for slot in range(2):
        path = directory / f"host-{pid}-external-{slot}.log"
        if not path.exists():
            continue
        try:
            if path.stat().st_size > 16384:
                raise ValueError("oversize")
            lines = path.read_text(encoding="ascii").splitlines()
            header = dict(part.split("=", 1) for part in lines[0].split())
            if (header.get("schema") != "2" or header.get("run") != run_id
                    or int(header["pid"]) != pid or int(header["creation"]) != creation
                    or int(header["snapshot"]) != slot or int(header["frequency"]) <= 0):
                raise ValueError("identity_or_schema")
            records = lines[1:-1]
            if lines[-1] != f"END count={len(records)}":
                raise ValueError("incomplete_frame")
            parsed = []
            for index, line in enumerate(records):
                seq, ticks, stage, outcome, a, b, c = line.split()
                if int(seq) != index:
                    raise ValueError("sequence")
                parsed.append(dict(stage=stage, outcome=outcome,
                                   qpc_ns=int(ticks)*1_000_000_000//int(header["frequency"]),
                                   values=[int(a), int(b), int(c)]))
            frames.append(dict(slot=slot, incomplete=header["incomplete"] != "0",
                               dropped=int(header["dropped"]), records=parsed,
                               first_stage=int(header["first_stage"]),
                               first_reason=int(header["first_reason"]),
                               first_flags=int(header.get("first_flags", "0")),
                               first_detail=int(header.get("first_detail", "0"))))
        except (ValueError, KeyError, IndexError, OSError, UnicodeError):
            issues.append(dict(slot=slot, reason="invalid_or_partial_frame"))
    complete = bool(frames) and not frames[-1]["incomplete"]
    intact = complete and not issues and not any(f["dropped"] for f in frames)
    records = frames[-1]["records"] if frames else []
    stage_status = {
        stage: {"reached": "yes" if any(r["stage"] == stage for r in records)
                else "not_reached" if intact else "unknown",
                "observations": [r for r in records if r["stage"] == stage]}
        for stage in STAGES
    }
    return dict(run_id=run_id, pid=pid, creation=creation, frames=frames, issues=issues,
                host_trace="recovered" if complete else "unrecovered",
                evidence_complete=intact, stages=stage_status,
                missing_reason=None if complete else "no_terminal_frame_write_failure_or_process_loss",
                absent_stage_interpretation="not_reached_only_if_complete_untruncated_else_unknown")
