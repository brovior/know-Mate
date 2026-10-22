"""Authenticated transfer of the original launch handle into a waiting supervisor."""
from __future__ import annotations

import json
import ctypes
import os
from pathlib import Path
from typing import Any

from scripts.diagnostics.ppt_capture.bundle import reject_links

KIND = "ppt_quit_compare_bootstrap"
VERSION = 1


def validate_bootstrap(config: dict[str, Any]) -> None:
    """Validate a versioned launch request without claiming a target already exists."""
    from .ppt_quit_compare import validate_binding
    if type(config) is not dict or set(config) != {
            "kind", "version", "arm", "run_id", "token", "parent_pid", "parent_creation",
            "fixture_path", "fixture_sha256", "timeout", "evidence", "allowed_pids",
            "office_bitness", "handoff"}:
        raise ValueError("caller_config")
    if (config["kind"] != KIND or type(config["version"]) is not int or config["version"] != VERSION
            or type(config["office_bitness"]) is not int or config["office_bitness"] not in {32, 64}
            or type(config["handoff"]) is not str):
        raise ValueError("caller_config")
    legacy = {key: value for key, value in config.items() if key not in {
        "kind", "version", "office_bitness", "handoff"}}
    validate_binding(dict(legacy, pid=1, creation=1))
    evidence, handoff = Path(config["evidence"]), Path(config["handoff"])
    if (not handoff.is_absolute() or handoff.name != "launch-handoff.json"
            or handoff.parent != evidence.parent or config["allowed_pids"] != [config["parent_pid"]]):
        raise ValueError("caller_paths")


def publish_launch(native: Any, parent: int, launched: Any, config: dict[str, Any]) -> None:
    """Duplicate the original CreateProcess capability, then atomically publish its metadata."""
    if native.wait(parent) or native.creation(parent) != config["parent_creation"]:
        raise RuntimeError("handoff_parent_changed")
    # LaunchedPowerPoint still owns this exact original handle until detach.
    remote = native.duplicate(launched._handle, target=parent, rights=0x100000 | 0x1000)
    value = dict(kind="ppt_quit_compare_launch", version=VERSION,
                 **{key: config[key] for key in ("run_id", "token", "parent_pid", "parent_creation")},
                 caller_pid=os.getpid(), pid=launched.pid, creation=launched.identity, handle=remote)
    path = Path(config["handoff"])
    temporary = path.with_name("launch-handoff.pending")
    reject_links(path)
    reject_links(temporary)
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")))
    os.replace(temporary, path)


def receive_launch(native: Any, path: Path, config: dict[str, Any], caller_pid: int) -> Any:
    """Adopt only the transferred handle whose kernel identity matches its authenticated metadata."""
    from .ppt_process_observation import RetainedProcess
    reject_links(path)
    if not path.exists():
        return None
    if not 0 < path.stat().st_size <= 4096:
        raise ValueError("handoff_record")
    try:
        with path.open("rb") as stream:
            content = stream.read(4097)
        if len(content) > 4096:
            raise ValueError("handoff_record")
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in items:
                if key in result:
                    raise ValueError("handoff_record")
                result[key] = item
            return result
        value = json.loads(content, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise ValueError("handoff_record") from None
    if (type(value) is not dict or set(value) != {
            "kind", "version", "run_id", "token", "parent_pid", "parent_creation",
            "caller_pid", "pid", "creation", "handle"}
            or value["kind"] != "ppt_quit_compare_launch" or type(value["version"]) is not int
            or value["version"] != VERSION):
        raise ValueError("handoff_record")
    for key in ("run_id", "token", "parent_pid", "parent_creation"):
        if type(value[key]) is not type(config[key]) or value[key] != config[key]:
            raise ValueError("handoff_binding")
    if type(value["caller_pid"]) is not int or value["caller_pid"] != caller_pid:
        raise ValueError("handoff_binding")
    for key, maximum in (("pid", 0xffffffff), ("creation", 0xffffffffffffffff),
                         ("handle", (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1)):
        if type(value[key]) is not int or not 0 < value[key] <= maximum:
            raise ValueError("handoff_identity")
    handle = value["handle"]
    # Do not OpenProcess by PID: this is the original kernel object from CreateProcess.
    if int(native.k.GetProcessId(handle)) != value["pid"] or native.creation(handle) != value["creation"]:
        raise ValueError("handoff_identity")
    return RetainedProcess(value["pid"], value["creation"], handle, native.k)
