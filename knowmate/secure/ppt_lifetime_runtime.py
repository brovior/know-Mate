"""Default-locked Windows lifetime supervision; never installs, registers or kills Office."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.diagnostics.ppt_capture.bundle import Bundle, primitive
from scripts.diagnostics.ppt_capture.gate import digest, read_json
from scripts.diagnostics.ppt_capture.transport import JsonlTail
from scripts.diagnostics.ppt_lifetime.fixture import create_fixture
from scripts.diagnostics.ppt_lifetime.gate import require_live_review, validate_package


def _validate(config: dict[str, Any]) -> dict[str, Any]:
    require_live_review()
    manifest = validate_package(Path(config["package"]), Path(config["source"]), config["pins"])
    approval_path = Path(config["pins"]["approval_path"])
    if digest(approval_path) != config["pins"].get("approval_sha256"):
        raise ValueError("lifetime_approval_pin")
    approval = read_json(approval_path)
    if (approval.get("package_manifest_sha256") != config["pins"]["package_manifest_sha256"]
            or approval.get("disposable_vm_approved") is not True
            or approval.get("native_x86") != "PASS" or approval.get("native_x64") != "PASS"):
        raise ValueError("lifetime_vm_native_unapproved")
    return manifest


class WindowsBackend:
    """One supervisor owns the original target observation independently of its caller."""

    def __init__(self, config: dict[str, Any], arm: str) -> None:
        """Require review before creating any native resources."""
        require_live_review()
        self.config, self.arm = config, arm
        self.native: Any = None
        self.target: Any = None
        self.caller: subprocess.Popen | None = None
        self.tail: JsonlTail | None = None
        self.bound = False
        self.spool: Any = None
        self.binding: dict[str, Any] = {}

    def start(self, bundle: Bundle) -> dict[str, Any]:
        """Start a fresh host only after exact package, VM and empty-baseline admission."""
        _validate(self.config)
        from .ppt_capture_native import Native, start_child
        from .ppt_capture_observer import processes
        from .ppt_launch import LaunchedPowerPoint, trusted_powerpoint_path
        self.native = Native()
        self.native.mutex()
        if any(row["name"] in {"powerpnt.exe", "aegisdesk.exe"} for row in processes(self.native)):
            raise RuntimeError("lifetime_baseline_not_empty")
        scratch = Path(tempfile.mkdtemp(prefix="aegis-lifetime-"))
        fixture = create_fixture(scratch / "fixture")
        launched = LaunchedPowerPoint(trusted_powerpoint_path(self.config["office_bitness"]))
        try:
            if type(launched.identity) is not int or launched.identity <= 0:
                raise RuntimeError("lifetime_launch_identity")
            self.binding = dict(run_id=uuid4().hex, arm=self.arm, pid=launched.pid, creation=launched.identity)
            self.target = launched.detach_observation()
        finally:
            launched.close()
        # A private regular-file spool never requires EOF or a caller join.
        self.spool = (scratch / "caller.jsonl").open("xb", buffering=0)
        self.tail = JsonlTail(scratch / "caller.jsonl")
        config = dict(self.config, binding=self.binding,
                      fixture=dict(asdict(fixture), path=str(fixture.path)))
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("AEGIS_PPT_", "AEGIS_CAPTURE_", "AEGIS_LIFETIME_"))}
        environment["AEGIS_LIFETIME_CONFIG"] = json.dumps(primitive(config), separators=(",", ":"))
        self.caller = start_child([sys.executable, "-m", "scripts.diagnostics.ppt_lifetime", "--caller"], [],
                                  cwd=self.config["source"], env=environment, stdin=subprocess.DEVNULL,
                                  stdout=self.spool, stderr=subprocess.DEVNULL)
        return dict(self.binding)

    def controller(self) -> list[dict[str, Any]]:
        """Consume available fixed records and recognize the exact COM-host binding."""
        rows = [] if self.tail is None else self.tail.poll()
        from scripts.diagnostics.ppt_lifetime.evidence import validate_record
        for row in rows:
            validate_record(row, self.binding)
            if row["event"] == "host_bound":
                self.bound = True
        return rows

    def observe(self) -> dict[str, Any]:
        """Poll the original process even when caller COM never returns."""
        observed = self.target.poll()
        status = {"timeout": "alive"}.get(observed.status, observed.status)
        if status == "exited" and not self.bound:
            status = "target_unconfirmed"
        return dict(self.binding, event="os_observed", qpc_ns=time.perf_counter_ns(),
                    status=status, error_code=observed.error_code)

    def close(self) -> dict[str, Any]:
        """Close owned handles only; preserve hung children/targets as pending evidence."""
        result = dict(caller_alive=None if self.caller is None else self.caller.poll() is None,
                      incomplete_transport=bool(self.tail and self.tail.partial))
        if self.target is not None:
            self.target.close()
        if self.native is not None:
            self.native.close()
        if self.spool is not None:
            self.spool.close()
        return result

    def caller_finished(self) -> bool:
        """Poll caller termination without waiting for its COM or its output EOF."""
        return self.caller is not None and self.caller.poll() is not None


def live_entry(arguments: Any) -> int:
    """Keep every live path gated before reading supplied files or importing native APIs."""
    require_live_review()
    from scripts.diagnostics.ppt_lifetime.supervisor import compare
    config = dict(package=str(arguments.package.resolve()), source=str(arguments.source.resolve()),
                  pins=read_json(arguments.pins), office_bitness=arguments.office_bitness)
    result = compare(arguments.output, lambda arm: WindowsBackend(config, arm))
    return 0 if len(result["arms"]) == 2 and all(
        row.get("experiment_complete") is True for row in result["arms"].values()) else 2


def caller_entry() -> int:
    """Validate again inside the private helper before native resources or COM import."""
    require_live_review()
    config = json.loads(os.environ["AEGIS_LIFETIME_CONFIG"])
    _validate(config)
    from .ppt_capture_native import Native
    from .ppt_lifetime_com import WindowsSession, run_caller
    native = Native()
    try:
        session = WindowsSession(config, native)
        def emit(event: str, **fields: Any) -> None:
            row = dict(config["binding"], event=event, qpc_ns=time.perf_counter_ns(), **fields)
            from scripts.diagnostics.ppt_lifetime.evidence import validate_record
            validate_record(row, config["binding"])
            print(json.dumps(primitive(row), separators=(",", ":")), flush=True)
        return run_caller(session, config["binding"]["arm"], emit)
    finally:
        native.close()
