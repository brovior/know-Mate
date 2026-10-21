"""Offline mock/replay CLI; the independent live/private source lock remains closed."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.diagnostics.ppt_capture.bundle import Bundle, MAX_FILE_BYTES
from scripts.diagnostics.ppt_capture.gate import digest, read_json
from scripts.diagnostics.ppt_capture.transport import JsonlTail
from .evidence import ARMS, summarize
from .gate import LIVE_LOCK_REASON, require_live_review
from .supervisor import compare


class MockBackend:
    """Produce explicitly simulated evidence without importing Office or native modules."""

    def __init__(self, arm: str) -> None:
        self.binding = dict(run_id=uuid4().hex, arm=arm,
                            pid=ARMS.index(arm) + 1, creation=ARMS.index(arm) + 1)
        self.sent = False

    def start(self, bundle: Bundle) -> dict[str, Any]:
        """Return a synthetic binding, never a real process claim."""
        return dict(self.binding)

    def controller(self) -> list[dict[str, Any]]:
        """Produce the ordered successful example once."""
        if self.sent:
            return []
        self.sent = True
        events = ["native_scope_acquired", "host_bound", "document_opened", "synthetic_read",
                  "document_closed", "idle_checked"]
        if self.binding["arm"] == ARMS[1]:
            events += ["quit_requested", "quit_returned"]
        events += ["references_dropped", "native_pair_completed", "caller_complete"]
        from .fixture import TEXT, TEXT_SHA256
        return [dict(self.binding, event=event, qpc_ns=time.perf_counter_ns(),
                     **(dict(characters=len(TEXT), sha256=TEXT_SHA256) if event == "synthetic_read" else {}))
                for event in events]

    def observe(self) -> dict[str, Any]:
        """Record a simulated exit, which never verifies an actual OS exit."""
        return dict(self.binding, event="os_observed", status="exited", qpc_ns=time.perf_counter_ns())

    def close(self) -> dict[str, Any]:
        """No native resources or processes exist."""
        return dict(caller_alive=False)

    def caller_finished(self) -> bool:
        """No caller process exists in the explicitly simulated backend."""
        return True


def replay(source: Path, output: Path) -> dict[str, Any]:
    """Validate bounded hashes/records and recompute one arm without trusting prior summary."""
    hashes = read_json(source / "hashes.json")
    names = {"manifest.json", "controller.jsonl", "os.jsonl", "summary.json"}
    if set(hashes) != names or any(digest(source / name) != sha for name, sha in hashes.items()):
        raise ValueError("lifetime_replay_hashes")
    manifest = read_json(source / "manifest.json")
    if (set(manifest) != {"schema", "variant", "mode", "binding"}
            or manifest["schema"] != 1 or manifest["variant"] != "lifetime_evidence"
            or set(manifest["binding"]) != {"run_id", "arm", "pid", "creation"}):
        raise ValueError("lifetime_replay_manifest")
    rows: dict[str, list[dict[str, Any]]] = {}
    partial = False
    for name in ("controller.jsonl", "os.jsonl"):
        tail = JsonlTail(source / name)
        rows[name] = tail.poll()
        partial |= bool(tail.partial)
    # Replay cannot independently authenticate past native observations.
    result = summarize(manifest, rows["controller.jsonl"], rows["os.jsonl"], None, partial)
    result.update(offline_replay=True, actual_os_exit_verified=False, experiment_complete=False)
    bundle = Bundle(output)
    bundle.write("manifest.json", manifest)
    for name, values in rows.items():
        stream = bundle.stream(name)
        for row in values:
            bundle.emit(stream, row)
    bundle.write("summary.json", result)
    bundle.finish()
    return result


def main(arguments: list[str] | None = None) -> int:
    """Run explicit offline modes while blocking both public and private live entries."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--replay", type=Path)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--caller", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--pins", type=Path)
    parser.add_argument("--office-bitness", choices=(32, 64), type=int, default=64)
    args = parser.parse_args(arguments)
    try:
        if args.live or args.caller:
            require_live_review()
            from knowmate.secure.ppt_lifetime_runtime import caller_entry, live_entry
            if args.caller:
                return caller_entry()
            if any(value is None for value in (args.output, args.package, args.source, args.pins)):
                parser.error("live requires --output --package --source --pins")
            return live_entry(args)
        if args.output is None:
            parser.error("--output is required")
        result = compare(args.output, MockBackend, mode="mock") if args.mock else replay(args.replay, args.output)
        print(json.dumps(dict(status="offline_complete", live_approved=False,
                              actual_os_exit_verified=False), separators=(",", ":")))
        return 0 if not result.get("evidence_incomplete", False) else 2
    except Exception as error:
        reason = LIVE_LOCK_REASON if str(error) == LIVE_LOCK_REASON else type(error).__name__
        print(json.dumps(dict(status="blocked", reason=reason), separators=(",", ":")))
        return 2
