"""Single-writer failure state with a coalesced GUI polling mailbox."""
from __future__ import annotations

import threading
import logging
from dataclasses import replace
from pathlib import Path
from typing import Callable

from knowmate.collector import failure_state
from knowmate.collector.failure_state import FailureRecord


class ObservedFailures(dict[str, FailureRecord]):
    """Publish replaced records without sharing the mutable owner map."""

    def __init__(self, records: dict[str, FailureRecord], changed: Callable) -> None:
        """Seed records without treating the initial snapshot as a mutation."""
        super().__init__(records)
        self._changed = changed

    def __setitem__(self, path: str, record: FailureRecord) -> None:
        """Record an upsert only when the scalar state changed."""
        if self.get(path) == record:
            return
        super().__setitem__(path, record)
        self._changed(path, record)

    def __delitem__(self, path: str) -> None:
        """Publish a removal after deleting an existing record."""
        super().__delitem__(path)
        self._changed(path, None)

    def pop(self, path: str, default=None) -> FailureRecord | None:
        """Publish removals from the existing failure-state helpers."""
        if path not in self:
            return default
        value = super().pop(path)
        self._changed(path, None)
        return value


class FailureUpdates:
    """Own a canonical failure map and bounded per-path pending metadata."""

    def __init__(self, path: Path) -> None:
        """Create a lazy session without filesystem work on the GUI thread."""
        self.path = path
        self.records: ObservedFailures | None = None
        self._lock = threading.Lock()
        self._cycle_id = 0
        self._revision = 0
        self._persisted_revision = 0
        self._dirty = False
        self._baseline: dict | None = None
        self._changes: dict = {}
        self._terminal: dict | None = None
        self._scope = "all"
        self._ready = False

    @property
    def dirty(self) -> bool:
        """Report unsaved metadata without exposing the owner map."""
        with self._lock:
            return self._dirty

    @property
    def cycle_id(self) -> int:
        """Return the current generation for GUI lifecycle correlation."""
        with self._lock:
            return self._cycle_id

    def begin(self, scope: str) -> ObservedFailures:
        """Load durable state unless an unsaved snapshot must be retained."""
        with self._lock:
            self._cycle_id += 1
            self._revision = 0
            self._persisted_revision = -1 if self._dirty else 0
            self._scope = scope
            self._terminal = None
            self._changes = {}
            self._baseline = None
            self._ready = False
        if self.records is None or not self.dirty:
            self.records = None
            self.records = ObservedFailures(failure_state.load_failures(self.path), self._changed)
        with self._lock:
            self._baseline = {path: replace(record) for path, record in self.records.items()}
            self._ready = True
        return self.records

    def _changed(self, path: str, record: FailureRecord | None) -> None:
        """Coalesce arbitrary owner mutations into one record per path."""
        with self._lock:
            self._revision += 1
            self._dirty = True
            self._changes[path] = replace(record) if record is not None else None

    def save(self) -> bool:
        """Acknowledge a durable owner snapshot and retain failures on error."""
        if self.records is None:
            return True
        try:
            saved = failure_state.save_failures(self.path, self.records)
        except Exception as exc:
            logging.getLogger(__name__).error("[failure] 실패 이력 저장 오류: error_type=%s", type(exc).__name__)
            saved = False
        with self._lock:
            self._dirty = not saved
            if saved:
                self._persisted_revision = self._revision
        return saved

    def finish(self, status: str, message: str) -> None:
        """Publish one terminal record after state flush and COM cleanup."""
        with self._lock:
            self._terminal = {"status": status, "scope": self._scope,
                              "message": message, "persisted": not self._dirty}
            self._ready = True

    def drain(self) -> dict | None:
        """Copy and remove pending deltas while retaining terminal metadata."""
        with self._lock:
            if not self._cycle_id or not self._ready:
                return None
            envelope = {
                "cycle_id": self._cycle_id, "revision": self._revision,
                "baseline": self._baseline,
                "changes": self._changes,
                "terminal": dict(self._terminal) if self._terminal is not None else None,
                "persisted_revision": self._persisted_revision,
            }
            self._baseline = None
            self._changes = {}
            return envelope
