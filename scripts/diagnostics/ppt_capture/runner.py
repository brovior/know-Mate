"""Offline artifact replay and reviewed live-entry routing; no native imports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .bundle import Bundle, MAX_FILE_BYTES
from .gate import LIVE_LOCK_REASON, digest, read_json, require_live_review
from .report import summarize
from .transport import CollectorRecords, JsonlTail


def replay(source: Path, output: Path) -> dict[str, Any]:
    """Recompute conservative coverage from primitive evidence into a new bundle."""
    manifest = read_json(source / "manifest.json")
    previous = read_json(source / "summary.json", MAX_FILE_BYTES)
    binding = previous["binding"]
    collector = CollectorRecords(binding["run_id"])
    issues: list[str] = list(previous.get("evidence_issues", []))
    hashes = read_json(source / "hashes.json")
    expected_names = {"manifest.json", "summary.json", "coverage.json", "host.jsonl", "controller.jsonl", "os.jsonl"}
    if set(hashes) != expected_names or any(digest(source / name) != sha for name, sha in hashes.items()):
        raise ValueError("replay_bundle_hashes")
    rows: dict[str, list[dict[str, Any]]] = {}
    for name in ("host.jsonl", "controller.jsonl", "os.jsonl"):
        tail = JsonlTail(source / name)
        rows[name] = tail.poll()
        if tail.partial:
            issues.append("incomplete_transport_line")
    for row in rows["host.jsonl"]:
        collector.accept(row)
    if not collector.ended:
        issues.append("collector_final_drain_unconfirmed")
    result = summarize(binding, collector.header, collector.host(binding), rows["controller.jsonl"],
                       rows["os.jsonl"], previous["capture_closed"], collector.persisted_seq, issues)
    bundle = Bundle(output)
    bundle.write("manifest.json", dict(manifest, replay=True))
    for name, values in rows.items():
        stream = bundle.stream(name)
        for row in values:
            bundle.emit(stream, row)
    bundle.write("summary.json", result)
    bundle.write("coverage.json", result["coverage"])
    bundle.finish()
    return result


def main(arguments: list[str] | None = None) -> int:
    """Provide a useful offline CLI while every live/private entry stays locked."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", type=Path)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--controller", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--make-fixture", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--pins", type=Path)
    parser.add_argument("--path", choices=("internal", "external"), default="internal")
    args = parser.parse_args(arguments)
    try:
        if args.live or args.controller:
            require_live_review()
            from knowmate.secure.ppt_capture_runtime import controller_entry, live_entry
            return controller_entry() if args.controller else live_entry(args)
        if args.output is None:
            parser.error("--output is required")
        if args.make_fixture:
            from .fixture import create_fixture
            print(create_fixture(args.output))
            return 0
        result = replay(args.replay, args.output)
        print(json.dumps({"capture_closed": result["capture_closed"], "os_exit": result["os_exit"],
                          "evidence_loss": result["evidence_loss"]}, separators=(",", ":")))
        return 0 if not result["evidence_loss"] else 2
    except Exception as error:
        reason = LIVE_LOCK_REASON if str(error) == LIVE_LOCK_REASON else type(error).__name__
        print(json.dumps({"status": "blocked", "reason": reason}, separators=(",", ":")))
        return 2
