"""Bounded regular-file transport; no pipe EOF or child completion is awaited."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bundle import MAX_FILE_BYTES, primitive, reject_links
from .layout import CAPACITY, OUTCOMES, STAGES


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("transport_duplicate_key")
        result[key] = value
    return result


class JsonlTail:
    """Copy complete lines from an already-created evidence stream without blocking."""

    def __init__(self, path: Path) -> None:
        """Start at offset zero and retain at most one incomplete bounded line."""
        reject_links(path)
        self.path = path
        self.offset = 0
        self.partial = b""

    def poll(self) -> list[dict[str, Any]]:
        """Read available bytes once, refusing truncation, oversized or malformed rows."""
        size = self.path.stat().st_size
        if size < self.offset or size > MAX_FILE_BYTES:
            raise ValueError("transport_size_or_truncation")
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(MAX_FILE_BYTES + 1)
        self.offset += len(data)
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        rows = []
        for line in lines:
            row = primitive(json.loads(line, object_pairs_hook=_pairs))
            if type(row) is not dict:
                raise ValueError("transport_row_object")
            rows.append(row)
        return rows


class CollectorRecords:
    """Validate collector messages against one independent target binding."""

    def __init__(self, run_id: str) -> None:
        """Retain primitive header/slots until the supervisor binds the target."""
        self.run_id = run_id
        self.ready = False
        self.ended = False
        self.header: dict[str, Any] | None = None
        self.slots: list[dict[str, Any]] = []
        self.persisted_seq = 0

    def accept(self, row: dict[str, Any]) -> None:
        """Reject reordered control frames, invalid enums and missing slot sequences."""
        kind = row.get("kind")
        fields = {
            "collector_ready": {"kind", "run_id"},
            "header": {"kind", "schema", "layout", "run_id", "pid", "creation", "frequency", "owner_native_thread", "owner_managed_thread", "apartment", "qualifier", "published_seq", "dropped", "offthread", "append_reentry", "capture_start_ticks", "pre_attach_records", "capture_scope", "counters_are_lower_bounds"},
            "slot": {"kind", "seq", "qpc_ticks", "stage_id", "outcome_id", "a", "b", "c", "native_thread", "managed_thread", "service_depth", "event_depth", "generation", "apartment", "qualifier"},
            "collector_end": {"kind", "persisted_seq", "producer_ready"},
        }
        if kind not in fields or set(row) != fields[kind]:
            raise ValueError("collector_frame_fields")
        if self.ended:
            raise ValueError("collector_after_end")
        if kind == "collector_ready":
            if self.ready or row.get("run_id") != self.run_id:
                raise ValueError("collector_ready_binding")
            self.ready = True
        elif kind == "header":
            if not self.ready or row.get("run_id") != self.run_id or row.get("schema") != 3 or row.get("layout") != 1:
                raise ValueError("collector_header_binding")
            if self.header and any(row.get(k) != self.header.get(k) for k in ("pid", "creation", "frequency", "owner_native_thread")):
                raise ValueError("collector_header_changed")
            if type(row.get("frequency")) is not int or row["frequency"] <= 0:
                raise ValueError("collector_frequency")
            for key in fields[kind] - {"kind", "run_id", "capture_scope", "counters_are_lower_bounds"}:
                if type(row[key]) is not int or row[key] < 0 or row[key] > 0x7fffffffffffffff:
                    raise ValueError("collector_header_integer")
            if (row["pid"] <= 0 or row["creation"] <= 0 or row["owner_native_thread"] <= 0
                    or row["owner_managed_thread"] <= 0 or row["apartment"] not in (0, 3)
                    or row["capture_start_ticks"] <= 0 or row["published_seq"] > CAPACITY or row["capture_scope"] != "bind_to_capture_end"
                    or row["counters_are_lower_bounds"] is not True):
                raise ValueError("collector_header_range")
            self.header = row
        elif kind == "slot":
            if self.header is None or len(self.slots) >= CAPACITY or type(row.get("seq")) is not int or row["seq"] != len(self.slots) + 1:
                raise ValueError("collector_slot_sequence")
            for key in fields[kind] - {"kind", "qpc_ticks"}:
                if type(row[key]) is not int or not -0x80000000 <= row[key] <= 0x7fffffff:
                    raise ValueError("collector_slot_integer")
            if any(row[key] < 0 for key in ("service_depth", "event_depth", "generation")) or any(row[key] <= 0 for key in ("native_thread", "managed_thread")):
                raise ValueError("collector_slot_depth_or_thread")
            if (row["native_thread"] != self.header["owner_native_thread"]
                    or row["managed_thread"] != self.header["owner_managed_thread"]
                    or row["apartment"] != self.header["apartment"] or row["qualifier"] != self.header["qualifier"]):
                raise ValueError("collector_slot_owner_binding")
            stage, outcome, ticks = row.get("stage_id"), row.get("outcome_id"), row.get("qpc_ticks")
            if (type(stage) is not int or not 0 <= stage < len(STAGES) or type(outcome) is not int
                    or not 0 <= outcome < len(OUTCOMES) or type(ticks) is not int or ticks < 0):
                raise ValueError("collector_slot_enum")
            self.slots.append(dict(row, stage=STAGES[stage], outcome=OUTCOMES[outcome],
                                   qpc_ns=ticks * 1_000_000_000 // self.header["frequency"]))
        elif kind == "collector_end":
            if not self.ready or type(row.get("producer_ready")) is not bool or type(row.get("persisted_seq")) is not int or row["persisted_seq"] != len(self.slots) or row["producer_ready"] != (self.header is not None):
                raise ValueError("collector_end_tail")
            self.ended = True
            self.persisted_seq = row["persisted_seq"]
        else:
            raise ValueError("collector_failed_or_unknown_frame")

    def host(self, binding: dict[str, Any]) -> list[dict[str, Any]]:
        """Attach only independently checked identity, never trust a slot to choose PID."""
        if self.header is None:
            return []
        if any(self.header.get(key) != binding.get(key) for key in ("run_id", "pid", "creation")):
            raise ValueError("collector_foreign_target")
        return [dict(row, **binding) for row in self.slots]
