"""GUI-owned failure snapshots drained from a bounded worker mailbox."""
from __future__ import annotations

from typing import Any


class FailureView:
    """Merge immutable outcome copies without reading a running worker's map."""

    def __init__(self) -> None:
        """Start without a worker snapshot or outstanding terminal."""
        self.records: dict | None = None
        self.cycle_id = 0
        self.revision = -1
        self.active = False
        self._finalized = 0
        self._terminal: dict | None = None

    def poll(self, worker: Any) -> tuple[bool, dict | None]:
        """Drain deltas and return a terminal only after the native thread stops."""
        drain = getattr(worker, "drain_failure_updates", None)
        if drain is None:
            return False, None
        envelope = drain()
        changed = False
        if envelope is not None:
            cycle = envelope["cycle_id"]
            if cycle >= self.cycle_id:
                if cycle != self.cycle_id:
                    self.cycle_id, self.revision = cycle, -1
                    self.active = True
                    self._terminal = None
                baseline = envelope.get("baseline")
                if baseline is not None:
                    self.records = dict(baseline)
                    changed = True
                revision = envelope["revision"]
                if revision > self.revision:
                    if self.records is None:
                        self.records = {}
                    for path, record in envelope.get("changes", {}).items():
                        if record is None:
                            self.records.pop(path, None)
                        else:
                            self.records[path] = record
                    changed = changed or bool(envelope.get("changes"))
                    self.revision = revision
                if envelope.get("terminal") is not None:
                    self._terminal = dict(envelope["terminal"])
        if (self._terminal is not None and self._finalized != self.cycle_id
                and not worker.isRunning()):
            self._finalized = self.cycle_id
            self.active = False
            return True, self._terminal
        return changed, None
