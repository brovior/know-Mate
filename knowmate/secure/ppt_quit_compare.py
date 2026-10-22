"""Bounded synthetic PowerPoint comparison for explicitly isolated Windows sessions."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

ARMS = ("external_quit", "internal_quit", "reference_drop")
EVENTS = {"native_scope_acquired", "host_bound", "synthetic_read", "document_closed",
          "quit_requested", "quit_returned", "references_dropped", "native_pair_completed",
          "caller_complete", "operation_failed", "launch_created", "handle_transferred",
          "dispatch_entered", "dispatch_returned"}
STATUSES = {"returned", "quit_requested", "protected", "not_supported", "failed", "unknown"}
_MAX_EVIDENCE = 65536


def caller_command() -> list[str]:
    """Reenter the same source or frozen executable before application initialization."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--ppt-quit-compare", "--caller"]
    return [sys.executable, "-m", "knowmate.secure.ppt_quit_compare", "--caller"]


def validate_binding(config: dict[str, Any]) -> None:
    """Reject malformed helper bindings before opening native processes or COM."""
    if (type(config) is not dict or set(config) != {
            "arm", "pid", "creation", "run_id", "token", "parent_pid", "parent_creation",
            "fixture_path", "fixture_sha256", "timeout", "evidence", "allowed_pids"}
            or config["arm"] not in ARMS):
        raise ValueError("caller_config")
    for name, limit in (("pid", 0xffffffff), ("creation", 0xffffffffffffffff),
                        ("parent_pid", 0xffffffff), ("parent_creation", 0xffffffffffffffff)):
        if type(config[name]) is not int or not 0 < config[name] <= limit:
            raise ValueError("caller_identity")
    for name, length in (("run_id", 32), ("token", 64), ("fixture_sha256", 64)):
        if type(config[name]) is not str or not re.fullmatch("[0-9a-f]{" + str(length) + "}", config[name]):
            raise ValueError("caller_nonce")
    if type(config["timeout"]) not in {int, float} or not math.isfinite(config["timeout"]) or not 5 <= config["timeout"] <= 300:
        raise ValueError("caller_timeout")
    if (type(config["allowed_pids"]) is not list or len(config["allowed_pids"]) > 2
            or any(type(pid) is not int or not 0 < pid <= 0xffffffff for pid in config["allowed_pids"])):
        raise ValueError("caller_allowed_pids")
    if type(config["fixture_path"]) is not str or type(config["evidence"]) is not str:
        raise ValueError("caller_paths")
    fixture, evidence = Path(config["fixture_path"]), Path(config["evidence"])
    if (not fixture.is_absolute() or not evidence.is_absolute() or fixture.name != "synthetic.pptx"
            or evidence.name != "caller.jsonl" or fixture.parent.parent != evidence.parent):
        raise ValueError("caller_paths")


def validate_row(row: dict[str, Any], config: dict[str, Any]) -> None:
    """Accept only fixed metadata; arbitrary text and document fields are forbidden."""
    if (type(row) is not dict or not {"event", "qpc_ns", "run_id", "pid", "creation"} <= set(row)
            or not set(row) <= {"event", "qpc_ns", "run_id", "pid", "creation", "status", "stage", "pythoncom_preimported", "error_type", "hresult", "winerror", "reason", "bind_step", "cause_error_type", "cause_hresult"}
            or type(row["event"]) is not str or row["event"] not in EVENTS or type(row["qpc_ns"]) is not int or row["qpc_ns"] <= 0
            or type(row["run_id"]) is not str or row["run_id"] != config["run_id"]):
        raise ValueError("evidence_record")
    identity = (row["pid"], row["creation"])
    if identity == (None, None):
        if config.get("kind") != "ppt_quit_compare_bootstrap" or row["event"] not in {"native_scope_acquired", "operation_failed", "references_dropped",
                                "native_pair_completed", "caller_complete"}:
            raise ValueError("evidence_record")
    elif (type(row["pid"]) is not int or not 0 < row["pid"] <= 0xffffffff
          or type(row["creation"]) is not int or not 0 < row["creation"] <= 0xffffffffffffffff):
        raise ValueError("evidence_record")
    elif config.get("pid") is not None and identity != (config["pid"], config["creation"]):
        raise ValueError("evidence_record")
    if "status" in row and (row["event"] != "quit_returned" or type(row["status"]) is not str or row["status"] not in STATUSES):
        raise ValueError("evidence_status")
    if "stage" in row and (row["event"] != "operation_failed" or type(row["stage"]) is not str or row["stage"] not in {
            "bind", "read", "close", "quit", "cleanup"}):
        raise ValueError("evidence_stage")
    if "pythoncom_preimported" in row and (row["event"] != "native_scope_acquired"
                                           or type(row["pythoncom_preimported"]) is not bool):
        raise ValueError("evidence_import_state")
    if "bind_step" in row and (row["event"] != "operation_failed" or row.get("stage") != "bind"
                               or type(row["bind_step"]) is not str or row["bind_step"] not in {
                                   "target_check", "client_import", "dispatch", "app_hwnd", "probe_precheck",
                                   "launch", "handoff",
                                   "caption_probe", "probe_owner", "probe_cleanup", "hwnd_owner", "idle_check",
                                   "service_select", "service_idle_check"}):
        raise ValueError("evidence_stage")
    errors = {"error_type", "hresult", "winerror", "reason"} & set(row)
    if errors:
        if row["event"] != "operation_failed" or errors != {"error_type", "hresult", "winerror", "reason"}:
            raise ValueError("evidence_record")
        # Match the encoder's fixed vocabulary without trusting arbitrary strings.
        from .ppt_quit_compare_com import ERROR_TYPES, error_fields
        allowed = error_fields(RuntimeError(row["reason"]))["reason"]
        if allowed != row["reason"] or type(row["error_type"]) is not str or row["error_type"] not in ERROR_TYPES | {"unknown"}:
            raise ValueError("evidence_record")
        if any(row[key] is not None and type(row[key]) is not int for key in ("hresult", "winerror")):
            raise ValueError("evidence_record")
    if "cause_error_type" in row or "cause_hresult" in row:
        if not errors or not {"cause_error_type", "cause_hresult"} <= set(row):
            raise ValueError("evidence_record")
        from .ppt_quit_compare_com import ERROR_TYPES
        allowed_types = ERROR_TYPES | {"unknown", None}
        if (row["cause_error_type"] is not None and type(row["cause_error_type"]) is not str
                or row["cause_error_type"] not in allowed_types
                or row["cause_hresult"] is not None and type(row["cause_hresult"]) is not int):
            raise ValueError("evidence_record")


class WindowsBackend:
    """Supervisor retains original process identity while the COM helper may hang."""

    def __init__(self, directory: Path, bitness: int, timeout: float) -> None:
        """Create a COM-free observer without activation or Office mutation."""
        from .ppt_capture_native import Native
        self.native = Native()
        try:
            self.native.mutex()
        except Exception:
            self.native.close()
            raise
        self.directory, self.bitness, self.timeout = directory, bitness, timeout
        self.target: Any = None
        self.caller: Any = None
        self.config: dict[str, Any] = {}

    def baseline(self) -> None:
        """Require no Office host or separate AegisDesk before each fresh experiment."""
        from .ppt_capture_observer import processes
        for row in processes(self.native):
            if row["name"] == "powerpnt.exe" or (row["name"] == "aegisdesk.exe" and row["pid"] != os.getpid()):
                raise RuntimeError("baseline_not_empty")

    def start(self, arm: str) -> dict[str, Any]:
        """Start the prepared caller before it creates its own PowerPoint process."""
        from .ppt_quit_compare_fixture import create_fixture
        from .ppt_capture_native import start_child
        from .ppt_quit_compare_handoff import KIND, VERSION, validate_bootstrap
        self.baseline()
        fixture = create_fixture(self.directory / "fixture")
        token = uuid4().hex + uuid4().hex
        self.config = dict(kind=KIND, version=VERSION, arm=arm,
                           run_id=uuid4().hex, token=token, parent_pid=os.getpid(),
                           parent_creation=self.native.creation(self.native.k.GetCurrentProcess()),
                           fixture_path=str(fixture.path), fixture_sha256=fixture.sha256,
                           evidence=str(self.directory / "caller.jsonl"), timeout=self.timeout,
                           allowed_pids=[os.getpid()], office_bitness=self.bitness,
                           handoff=str(self.directory / "launch-handoff.json"))
        validate_bootstrap(self.config)
        config_path = self.directory / "caller-config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        environment = dict(os.environ)
        for name in list(environment):
            if name.startswith("AEGIS_PPT_COMPARE_"):
                environment.pop(name)
        environment["AEGIS_PPT_COMPARE_CONFIG"] = str(config_path)
        environment["AEGIS_PPT_COMPARE_TOKEN"] = token
        self.caller = start_child(caller_command(), [], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
        self.binding = dict(**{key: self.config[key] for key in ("run_id", "arm")}, pid=None, creation=None,
                            caller_pid=self.caller.pid, supervisor_pid=os.getpid(), launch_handoff_required=True)
        return self.binding

    def poll(self) -> tuple[str, bool | None, list[dict[str, Any]]]:
        """Observe the exact original process and bounded complete helper records."""
        if self.target is None and self.caller is not None and "handoff" in self.config:
            from .ppt_quit_compare_handoff import receive_launch
            try:
                self.target = receive_launch(self.native, Path(self.config["handoff"]), self.config, self.caller.pid)
            except Exception as error:
                from .ppt_quit_compare_com import error_fields
                self.handoff_failure = error_fields(error)
            if self.target is not None:
                self.binding.update(pid=self.target.pid, creation=self.target.identity)
        observation = self.target.poll() if self.target is not None else None
        self.last_observation_error = None if observation is None else observation.error_code
        state = "target_unconfirmed" if observation is None else observation.status
        caller_alive = None if self.caller is None else self.caller.poll() is None
        rows: list[dict[str, Any]] = []
        path = self.directory / "caller.jsonl"
        if path.exists():
            from scripts.diagnostics.ppt_capture.bundle import reject_links
            reject_links(path)
            if path.stat().st_size > _MAX_EVIDENCE:
                raise ValueError("evidence_bound")
            content = path.read_bytes()
            lines = content.splitlines(keepends=True)
            if lines and not lines[-1].endswith(b"\n") and caller_alive is False:
                raise ValueError("evidence_partial")
            for line in lines:
                if not line.endswith(b"\n"):
                    continue
                row = json.loads(line)
                validate_row(row, dict(self.config, **getattr(self, "binding", {})))
                rows.append(row)
            if len(rows) > 64:
                raise ValueError("evidence_rows")
        return state, caller_alive, rows

    def close(self) -> None:
        """Release observations only; surviving callers and Office remain untouched."""
        if self.target is not None:
            self.target.close()
        self.native.close()


def summarize_arm(binding: dict[str, Any], state: str, alive: bool | None,
                  rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate OS exit, caller exit and completed experimental stages."""
    events = [row["event"] for row in rows]
    if state not in {"exited", "timeout", "target_unconfirmed", "observation_failed"} or (alive is not None and type(alive) is not bool):
        raise ValueError("evidence_record")
    bound = "host_bound" in events
    verified = state == "exited" and bound and type(binding.get("pid")) is int and type(binding.get("creation")) is int
    if len(events) != len(set(events)) or any(rows[i]["qpc_ns"] >= rows[i + 1]["qpc_ns"] for i in range(len(rows) - 1)):
        raise ValueError("evidence_order")
    failed = "operation_failed" in events
    completed = all(event in events for event in (
        "native_scope_acquired", "host_bound", "synthetic_read", "document_closed",
        "references_dropped", "native_pair_completed", "caller_complete")) and not failed
    expected = ["native_scope_acquired", "host_bound", "synthetic_read", "document_closed"]
    if binding.get("arm") != "reference_drop":
        expected += ["quit_requested", "quit_returned"]
    expected += ["references_dropped", "native_pair_completed", "caller_complete"]
    startup_events = {"launch_created", "handle_transferred", "dispatch_entered", "dispatch_returned"}
    startup = [event for event in events if event in startup_events]
    if startup or binding.get("launch_handoff_required"):
        expected = expected[:1] + ["launch_created", "handle_transferred", "dispatch_entered", "dispatch_returned"] + expected[1:]
    completed = completed and events == expected
    quit_status = next((row.get("status") for row in rows if row["event"] == "quit_returned"), None)
    if binding.get("arm") == "internal_quit" and quit_status != "quit_requested":
        completed = False
    if binding.get("arm") == "external_quit" and quit_status != "returned":
        completed = False
    timings = {row["event"]: round((row["qpc_ns"] - rows[0]["qpc_ns"]) / 1000000, 3) for row in rows} if rows else {}
    failures = [{key: row.get(key) for key in ("stage", "bind_step", "reason", "error_type", "hresult", "winerror",
                                              "cause_error_type", "cause_hresult")}
                for row in rows if row["event"] == "operation_failed"]
    return dict(binding, os_status=state, actual_os_exit_verified=verified,
                caller_exited=alive is False, experiment_complete=completed and alive is False,
                native_pair_scope="caller_owned_only", pythoncom_initialization="implicit_import_possible",
                handoff_verified=type(binding.get("pid")) is int and type(binding.get("creation")) is int,
                caller_reported_pid=next((row["pid"] for row in rows if type(row["pid"]) is int), None),
                caller_reported_creation=next((row["creation"] for row in rows if type(row["creation"]) is int), None),
                outcome="exited" if verified else "remaining" if state == "timeout" else "unknown",
                events=events, stage_elapsed_ms=timings, failures=failures,
                pythoncom_preimported=next((row.get("pythoncom_preimported") for row in rows
                                           if row["event"] == "native_scope_acquired"), None),
                quit_status=quit_status)


def run_compare(output: Path, bitness: int = 64, timeout: float = 30,
                confirm_isolated_session: bool = False, arm: str = "all",
                backend_factory: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Run independent experiments and stop on a surviving or unconfirmed host/caller."""
    if (confirm_isolated_session is not True or type(bitness) is not int or bitness not in {32, 64}
            or type(timeout) not in {int, float} or not math.isfinite(timeout)
            or not 5 <= timeout <= 300 or arm not in (*ARMS, "all")):
        raise ValueError("isolated_session_and_valid_options_required")
    from scripts.diagnostics.ppt_capture.bundle import reject_links
    output = output.absolute()
    reject_links(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    factory = backend_factory or WindowsBackend
    arms = ARMS if arm == "all" else (arm,)
    report: dict[str, Any] = dict(schema=1, isolated_session_required=True,
                                simulated=backend_factory is not None, arms={}, stopped=False)
    log = output / "summary.log"
    with log.open("x", encoding="utf-8") as stream:
        stream.write("PowerPoint synthetic quit comparison; no Office force termination\n")
        stream.flush()
        for current in arms:
            if report["stopped"]:
                report["arms"][current] = dict(outcome="UNEXECUTED", reason="previous_target_or_caller_unresolved")
                stream.write(current + " " + json.dumps(report["arms"][current]) + "\n")
                stream.flush()
                continue
            directory = output / current
            directory.mkdir(mode=0o700)
            backend = None
            result: dict[str, Any] = dict(outcome="unknown", experiment_complete=False,
                                          actual_os_exit_verified=False, caller_exited=False)
            try:
                backend = factory(directory, bitness, timeout)
                binding = backend.start(current)
                observation_started = time.perf_counter_ns()
                observed_times: dict[str, float] = {}
                deadline = time.monotonic() + timeout
                while True:
                    state, alive, rows = backend.poll()
                    result = summarize_arm(binding, state, alive, rows)
                    elapsed = round((time.perf_counter_ns() - observation_started) / 1000000, 3)
                    if state == "exited":
                        observed_times.setdefault("os_exit_observed_after_ms", elapsed)
                    if alive is False:
                        observed_times.setdefault("caller_exit_observed_after_ms", elapsed)
                    result.update(observed_times, observation_elapsed_ms=elapsed,
                                  observation_origin="caller_started")
                    result["os_error_code"] = getattr(backend, "last_observation_error", None)
                    if getattr(backend, "handoff_failure", None) is not None:
                        result["handoff_failure"] = backend.handoff_failure
                        result["experiment_complete"] = False
                    if getattr(backend, "caller", None) is not None:
                        code = backend.caller.poll()
                        result["caller_exit_code"] = code
                        if code != 0:
                            result["experiment_complete"] = False
                    if (state == "exited" and alive is False) or time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
                if backend_factory is not None:
                    result["actual_os_exit_verified"] = False
                if not (state == "exited" and alive is False and result["experiment_complete"]):
                    report["stopped"] = True
            except Exception as error:
                from .ppt_quit_compare_com import error_fields
                result.update(error_fields(error))
                result["experiment_complete"] = False
                if backend is not None and getattr(backend, "target", None) is not None:
                    result.update(pid=backend.target.pid, creation=backend.target.identity)
                if backend is not None and getattr(backend, "caller", None) is not None:
                    result.update(caller_pid=backend.caller.pid, supervisor_pid=os.getpid(),
                                  caller_exited=backend.caller.poll() is not None)
                report["stopped"] = True
            finally:
                if backend is not None:
                    try:
                        backend.close()
                    except Exception:
                        result["close_failed"] = True
                        report["stopped"] = True
            report["arms"][current] = result
            stream.write(current + " " + json.dumps(result, separators=(",", ":")) + "\n")
            stream.flush()
        stream.write("remaining arms are UNEXECUTED when a target/caller is unresolved\n")
    (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def caller_entry() -> int:
    """Validate a supervisor nonce, parent process and generated fixture before COM."""
    if os.name != "nt":
        return 2
    from scripts.diagnostics.ppt_capture.bundle import reject_links
    config_path = Path(os.environ.get("AEGIS_PPT_COMPARE_CONFIG", ""))
    reject_links(config_path)
    if not config_path.is_absolute() or config_path.name != "caller-config.json" or config_path.stat().st_size > 16384:
        return 2
    config = json.loads(config_path.read_bytes())
    from .ppt_quit_compare_handoff import validate_bootstrap
    validate_bootstrap(config)
    if (config["token"] != os.environ.pop("AEGIS_PPT_COMPARE_TOKEN", "")
            or config_path.parent != Path(config["evidence"]).parent):
        return 2
    from .ppt_capture_native import Native
    from .ppt_quit_compare_com import Session, run_caller
    native = Native()
    try:
        parent = native.keep(native.k.OpenProcess(0x100000 | 0x1000 | 0x40, False, config["parent_pid"]))
        if native.wait(parent) or native.creation(parent) != config["parent_creation"]:
            return 2
        config["allowed_pids"].append(os.getpid())
        sys.coinit_flags = 0
        evidence = Path(config["evidence"])
        reject_links(evidence)
        with evidence.open("xb", buffering=0) as stream:
            def emit(event: str, **fields: Any) -> None:
                row = dict(event=event, qpc_ns=time.perf_counter_ns(),
                           run_id=config["run_id"], pid=config.get("pid"), creation=config.get("creation"), **fields)
                validate_row(row, config)
                stream.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
            try:
                session = Session(config, native, parent)
            except Exception as error:
                from .ppt_quit_compare_com import error_fields
                emit("operation_failed", stage="bind", **error_fields(error))
                emit("caller_complete")
                return 2
            try:
                return run_caller(session, config["arm"], emit)
            finally:
                if session.observer is not None:
                    session.observer.close()
    finally:
        native.close()


def main(arguments: list[str] | None = None) -> int:
    """Expose an opt-in isolated-session comparison, never normal product cleanup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-isolated-session", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--office-bitness", choices=(32, 64), type=int, default=64)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--arm", choices=("all", *ARMS), default="all")
    parser.add_argument("--caller", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    try:
        if args.caller:
            return caller_entry()
        if not args.live or not args.confirm_isolated_session or args.output is None:
            parser.error("requires --live --confirm-isolated-session --output NEW_DIRECTORY")
        report = run_compare(args.output, args.office_bitness, args.timeout,
                             args.confirm_isolated_session, args.arm)
        if sys.stdout is not None:
            print("PPT comparison finished; see summary.log and result.json")
        return 2 if report["stopped"] else 0
    except Exception:
        if sys.stdout is not None:
            print("PPT comparison blocked; no arbitrary exception text recorded")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
