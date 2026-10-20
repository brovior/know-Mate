"""Windows process/capability orchestration behind the unexecuted live review lock."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import shutil
import signal
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.diagnostics.ppt_capture.bundle import Bundle, primitive, reject_links
from scripts.diagnostics.ppt_capture.gate import digest, read_json, require_live_review, validate_package
from scripts.diagnostics.ppt_capture.supervisor import capture
from scripts.diagnostics.ppt_capture.transport import CollectorRecords

from .ppt_capture_native import Native, start_child
from .ppt_capture_observer import Probe, processes, snapshot, wait_chain
from .ppt_capture_preflight import environment, helper_check, lease_directories, registration_fingerprint


def _clean_environment() -> dict[str, str]:
    """Remove old diagnostic controls instead of inheriting an unrelated experiment."""
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("AEGIS_PPT_", "AEGIS_TEST_", "AEGIS_CAPTURE_"))}


def _validate_approval(config: dict[str, Any]) -> None:
    """Require native results and concrete package-bound approval after the source lock."""
    pins = config["pins"]
    for field in ("native_gate", "office_approval"):
        path = Path(pins[field + "_path"])
        if digest(path) != pins[field + "_sha256"]:
            raise ValueError("capture_approval_pin")
        report = read_json(path)
        if report.get("package_manifest_sha256") != pins["package_manifest_sha256"]:
            raise ValueError("capture_approval_package")
        if field == "native_gate":
            if report.get("x86") != "PASS" or report.get("x64") != "PASS":
                raise ValueError("capture_native_unexecuted")
        elif report.get("approved") is not True or report.get("path") != config["path"]:
            raise ValueError("capture_office_not_approved")


def _validate(config: dict[str, Any]) -> dict[str, Any]:
    """Repeat package/source/fixture approval checks in parent and private helper."""
    require_live_review()
    manifest = validate_package(Path(config["package"]), Path(config["source"]), config["pins"])
    _validate_approval(config)
    installed = Path(config["pins"]["installed_directory"])
    for name in ("AegisDesk.PptReader.dll", "AegisDesk.PptReader.Events.dll"):
        if digest(installed / name) != manifest["files"][name]:
            raise ValueError("capture_installed_pin")
    return manifest


class WindowsBackend:
    """Hold collector, caller and original target handles independently of COM progress."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize resources only after the locked CLI has admitted this package."""
        self.config, self.native = config, Native()
        self.output_native = Native()
        self.collector: subprocess.Popen | None = None
        self.caller: subprocess.Popen | None = None
        self.target: int | None = None
        self._binding = dict(run_id=config["run_id"], pid=0, creation=0)
        self.probes: list[Probe] = []
        self.request_ns: int | None = None
        self.snapshot_index = 0
        self.owner_thread: int | None = None
        self.stopping = False
        self.admitted = False
        self.output: Bundle | None = None
        self.snapshot_disabled = False
        self.spools: dict[str, Any] = {}
        self.pending_os: list[dict[str, Any]] = []

    def start(self, bundle: Bundle) -> dict[str, Path]:
        """Arm protected mapping/collector before ever starting a caller or PowerPoint."""
        _validate(self.config)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        if shell.IsUserAnAdmin():
            raise RuntimeError("admin_not_allowed")
        reject_links(bundle.root)
        lease_directories(self.output_native, bundle.root, bundle._streams)
        volume = ctypes.create_unicode_buffer(32)
        root = bundle.root.anchor
        if not self.native.k.GetVolumeInformationW(root, None, 0, None, None, None, volume, len(volume)) or volume.value != "NTFS":
            raise RuntimeError("local_ntfs_required")
        if self.native.k.GetDriveTypeW(root) != 3:
            raise RuntimeError("local_fixed_volume_required")
        if any(p["name"] in {"powerpnt.exe", "aegisdesk.exe"} for p in processes(self.native)):
            raise RuntimeError("baseline_not_empty")
        helper_check(Path(self.config["package"]), self.config["pins"])
        self.config["registration_sha256"] = registration_fingerprint()
        self.runtime_environment = environment(self.native, self.config["pins"])
        self.native.mutex()
        self.output = bundle
        self.spool_root = Path(tempfile.mkdtemp(prefix="aegis-ppt-capture-" + self.config["run_id"] + "-"))
        for name in ("host.jsonl", "controller.jsonl"):
            self.spools[name] = (self.spool_root / name).open("xb", buffering=0)
        self.mapping = self.native.mapping(self.config["run_id"])
        self.stop_event, self.cancel, self.arm, self.admit = [self.native.event() for _ in range(4)]
        parent = self.native.duplicate(self.native.k.GetCurrentProcess(), inherit=True, rights=0x100000)
        mapping = self.native.duplicate(self.mapping, inherit=True)
        stop = self.native.duplicate(self.stop_event, inherit=True)
        self.collector = start_child([str(Path(self.config["package"]) / "PptCaptureCollector.exe"),
                                      str(mapping), str(stop), str(parent), self.config["run_id"]],
                                     [mapping, stop, parent], stdout=self.spools["host.jsonl"],
                                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=_clean_environment())
        return {name: self.spool_root / name for name in self.spools}

    def tick(self, collector: CollectorRecords, controller: list[dict[str, Any]]) -> None:
        """Advance only when external collection and exact target ownership are proven."""
        if self.stopping:
            return
        if collector.ready and self.caller is None:
            assert self.output is not None
            parent = self.native.duplicate(self.native.k.GetCurrentProcess(), inherit=True, rights=0x100040)
            caps = {name: self.native.duplicate(handle, inherit=True) for name, handle in
                    (("mapping", self.mapping), ("cancel", self.cancel), ("arm", self.arm), ("admit", self.admit))}
            config = dict(self.config, **caps, parent=parent, parent_pid=os.getpid(),
                          parent_creation=self.native.creation(self.native.k.GetCurrentProcess()))
            env = _clean_environment()
            env["AEGIS_CAPTURE_CONFIG"] = json.dumps(primitive(config), separators=(",", ":"))
            self.caller = start_child([sys.executable, "-m", "scripts.diagnostics.ppt_capture", "--controller"],
                                      [parent, *caps.values()], cwd=self.config["source"], env=env,
                                      stdout=self.spools["controller.jsonl"], stderr=subprocess.DEVNULL,
                                      stdin=subprocess.DEVNULL)
        for row in controller:
            if row.get("run_id") != self.config["run_id"]:
                raise RuntimeError("controller_foreign_run")
            if row.get("event") == "target_created" and self.target is None:
                if self.caller is None or row.get("caller_pid") != self.caller.pid or row.get("launching_pid") != self.caller.pid:
                    raise RuntimeError("launching_caller_mismatch")
                number, original = row["parent_target_handle"], row["original_target_handle"]
                if type(number) is not int or type(original) is not int:
                    raise RuntimeError("target_handle_format")
                if not self.native.same_in_child(int(self.caller._handle), original, number):
                    raise RuntimeError("target_handle_object_mismatch")
                if self.native.k.GetProcessId(number) != row["pid"] or self.native.creation(number) != row["creation"]:
                    raise RuntimeError("target_handle_binding")
                self.target = self.native.keep(number)
                self._binding = dict(run_id=self.config["run_id"], pid=row["pid"], creation=row["creation"])
                self.pending_os.append(dict(self._binding, event="runtime_environment", qpc_ns=time.perf_counter_ns(), **self.runtime_environment))
            if self.target is not None and any(row.get(key) != self._binding[key] for key in ("pid", "creation")):
                raise RuntimeError("controller_foreign_target")
            if row.get("event") == "capabilities_verified" and self.target is not None:
                self.native.signal(self.arm)
            if row.get("event") == "ownership_proved":
                self.owner_thread = row["native_owner_thread"]
            if row.get("event") == "observation_saved" and not self.admitted:
                if self.target is None or collector.header is None or self.health():
                    continue
                collector.host(self._binding)
                self.admitted = True
                # Snapshot starts before granting the one request. WCT has its
                # own daemon worker and never blocks this retained-handle loop.
                self._snapshot()
                self.native.signal(self.admit)
            if row.get("event") == "request_begin" and self.request_ns is None:
                self.request_ns = row["qpc_ns"]

    def _snapshot(self) -> None:
        if self.target is not None and not self.snapshot_disabled and not any(not p.done for p in self.probes if p.kind == "os_snapshot"):
            handle = self.native.detach(self.native.duplicate(self.target, rights=0x100000 | 0x1000))
            binding = dict(self._binding)
            def observe_copy() -> dict[str, Any]:
                observer = Native()
                observer.keep(handle)
                try:
                    return snapshot(observer, handle, binding)
                finally:
                    observer.close()
            self.probes.append(Probe("os_snapshot", observe_copy, time.perf_counter_ns()))
        if self.owner_thread and not any(p.kind == "wct" for p in self.probes):
            owner = self.owner_thread
            self.probes.append(Probe("wct", lambda: wait_chain(owner), time.perf_counter_ns()))

    def binding(self) -> dict[str, Any]:
        """Return only the target identity copied from a verified original process handle."""
        return dict(self._binding)

    def observe(self) -> list[dict[str, Any]]:
        """Poll exact target wait even if caller, collector, snapshot or WCT has hung."""
        rows, self.pending_os = self.pending_os, []
        now = time.perf_counter_ns()
        if self.target is not None:
            rows.append(self.native.process_state(self.target, self._binding))
            if self.request_ns is not None and not self.stopping:
                schedule = (1, 5, 15, 30, 60, 180)
                if self.snapshot_index < len(schedule) and now - self.request_ns >= schedule[self.snapshot_index] * 1_000_000_000:
                    self.snapshot_index += 1
                    self._snapshot()
        for probe in self.probes:
            value = probe.poll(now)
            if value is not None:
                if probe.kind == "os_snapshot" and value.get("available") is False:
                    self.snapshot_disabled = True
                rows.append(dict(value, **self._binding))
        return rows

    def health(self) -> str | None:
        """Report helper failure while preserving target observation until exit/deadline."""
        if self.collector is not None and self.collector.poll() is not None and not self.stopping:
            return "collector_failed"
        if self.caller is not None and self.caller.poll() not in (None, 0):
            return "caller_failed"
        return None

    def stop(self, reason: str) -> None:
        """Revoke new COM stages and signal collector drain without process termination."""
        self.stopping = True
        if hasattr(self, "cancel"):
            self.native.signal(self.cancel)
        # Failure before closure revokes COM but collection must continue for the
        # target. Only a capture-end reason closes its observation interval.
        if reason in {"target_exit", "deadline", "cancelled", "blocked"} and hasattr(self, "stop_event"):
            self.native.signal(self.stop_event)

    def close(self) -> dict[str, Any]:
        """Copy child liveness then close handles without child.wait, kill or forced GC."""
        result = {name: None if child is None else dict(pid=child.pid, exit_code=child.poll(), alive=child.poll() is None)
                  for name, child in (("collector", self.collector), ("caller", self.caller))}
        result["target"] = None if self.target is None else dict(pid=self._binding["pid"], alive=not self.native.wait(self.target))
        for stream in self.spools.values():
            stream.close()
        alive = any(child is not None and child.poll() is None for child in (self.collector, self.caller))
        result["private_spool"] = "retained_child_alive" if alive else "removed"
        if hasattr(self, "spool_root") and not alive:
            shutil.rmtree(self.spool_root)
        self.native.close()
        return result

    def release_output(self) -> None:
        """Release directory leases only after summary, coverage and hashes are durable."""
        self.output_native.close()


def live_entry(arguments: Any) -> int:
    """Validate concrete inputs and run one new target; never install or register anything."""
    require_live_review()
    if arguments.package is None or arguments.pins is None or arguments.output is None:
        raise ValueError("capture_live_inputs_required")
    config = dict(package=str(arguments.package.absolute()), source=str(Path(__file__).resolve().parents[2]),
                  pins=read_json(arguments.pins), path=arguments.path, run_id=uuid4().hex)
    manifest = _validate(config)
    backend = WindowsBackend(config)
    cancelled = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda number, frame: cancelled.set())
    try:
        result = capture(arguments.output, dict(run_id=config["run_id"], package=manifest, path=config["path"]),
                         backend, cancelled=cancelled.is_set)
    finally:
        signal.signal(signal.SIGINT, previous)
    return 0 if not result["evidence_loss"] else 2


def controller_entry() -> int:
    """Require parent-bound capabilities and pins before directly creating one target."""
    require_live_review()
    config = primitive(json.loads(os.environ.pop("AEGIS_CAPTURE_CONFIG")))
    _validate(config)
    if registration_fingerprint() != config["registration_sha256"]:
        raise RuntimeError("controller_registration_changed")
    native = Native()
    if native.k.GetProcessId(config["parent"]) != config["parent_pid"] or native.creation(config["parent"]) != config["parent_creation"] or native.wait(config["parent"]):
        raise RuntimeError("controller_parent_binding")
    config.update(pid=0, creation=0)
    def emit(event: str, **fields: Any) -> None:
        if config.get("persist_failed"):
            return
        row = dict(fields, event=event, run_id=config["run_id"], pid=config["pid"], creation=config["creation"], qpc_ns=time.perf_counter_ns())
        try:
            sys.stdout.write(json.dumps(primitive(row), separators=(",", ":")) + "\n")
            sys.stdout.flush()
            os.fsync(sys.stdout.fileno())
        except Exception:
            config["persist_failed"] = True
            raise
    child = None
    try:
        from .ppt_launch import trusted_powerpoint_path
        if any(p["name"] in {"powerpnt.exe", "aegisdesk.exe"} for p in processes(native)):
            raise RuntimeError("baseline_not_empty")
        raw = [native.event() for _ in range(3)]
        controller = native.duplicate(native.k.GetCurrentProcess(), inherit=True, rights=0x100000)
        caps = [native.duplicate(h, inherit=True, rights=0x100002 if i == 1 else 0x100000) for i, h in enumerate(raw)]
        mapping = native.duplicate(config["mapping"], inherit=True)
        env = _clean_environment()
        config["nonce"] = uuid4().hex
        env.update(AEGIS_PPT_EXTERNAL_DIAGNOSTIC="1", AEGIS_PPT_LIFECYCLE_TRACE="1", AEGIS_PPT_CAPTURE_DIAGNOSTIC="1",
                   AEGIS_TEST_NONCE=config["nonce"], AEGIS_PPT_TRACE_RUN=config["run_id"], AEGIS_PPT_CAPTURE_MAPPING=str(mapping))
        for name, handle in zip(("START", "ACK", "COMPLETE", "CONTROLLER"), [*caps, controller]):
            env["AEGIS_TEST_CAP_" + name] = str(handle)
        executable = trusted_powerpoint_path(config["pins"]["office_bitness"])
        child = start_child([str(executable), "/EMBEDDING"], [*caps, controller, mapping], env=env,
                            cwd=executable.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        config.update(pid=child.pid, creation=native.creation(int(child._handle)), target_handle=int(child._handle))
        transferred = native.duplicate(int(child._handle), target=config["parent"], rights=0x100000 | 0x1000)
        emit("target_created", caller_pid=os.getpid(), launching_pid=os.getpid(), target_parent_pid=os.getpid(),
             original_target_handle=int(child._handle), parent_target_handle=transferred)
        for number, original in zip([*caps, controller, mapping], [*raw, native.k.GetCurrentProcess(), config["mapping"]]):
            if not native.same_in_child(int(child._handle), number, original):
                raise RuntimeError("actual_child_capability_mismatch")
        emit("capabilities_verified", capability_objects_verified=5)
        if not native.wait(config["arm"], 3000) or native.wait(config["cancel"]):
            emit("request_not_run", reason="independent_observer_not_armed")
            return 2
        config.update(start=raw[0], ack=raw[1], complete=raw[2])
        from .ppt_capture_com import run_apartment
        result = run_apartment(native, config, emit)
        return 2 if config.get("persist_failed") else result
    finally:
        native.close()
