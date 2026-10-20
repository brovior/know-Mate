"""Create a bounded allowlisted diagnostic bundle without replacing prior evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, TextIO

FILES = frozenset({"summary.json", "manifest.json", "controller.jsonl", "host.jsonl", "os.jsonl", "coverage.json", "hashes.json"})
MAX_FILE_BYTES = 8 * 1024 * 1024


def primitive(value: Any) -> Any:
    """Require JSON primitives and reject custom objects, nonfinite values, and huge output."""
    def check(item: Any, depth: int = 0) -> None:
        if depth > 20:
            raise ValueError("record_depth")
        if item is None or type(item) in (bool, int, float, str):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise TypeError("nonprimitive_record")
    check(value)
    encoded = json.dumps(value, allow_nan=False, ensure_ascii=True)
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("record_size")
    return json.loads(encoded)


def reject_links(path: Path) -> None:
    """Reject existing symlink/reparse components before opening offline evidence."""
    for component in [path, *path.parents]:
        if component.exists() or component.is_symlink():
            info = component.lstat()
            if component.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("reparse_output_path")


class Bundle:
    """Own new output streams; native live mode additionally holds directory leases."""

    def __init__(self, root: Path) -> None:
        reject_links(root)
        self.root = root.absolute()
        self.root.mkdir(mode=0o700, parents=False, exist_ok=False)
        self._streams: dict[str, TextIO] = {}
        self._created: set[str] = set()

    def stream(self, name: str) -> TextIO:
        """Create one allowlisted stream with exclusive creation."""
        if name not in FILES or name in self._created:
            raise ValueError("output_name_or_reuse")
        reject_links(self.root)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.root / name, flags, 0o600)
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        self._streams[name] = stream
        self._created.add(name)
        return stream

    def write(self, name: str, value: Any) -> None:
        """Write a new JSON document and fsync it before reporting success."""
        stream = self.stream(name)
        json.dump(primitive(value), stream, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()

    def emit(self, stream: TextIO, value: dict[str, Any]) -> None:
        """Append a primitive line within the evidence size bound."""
        encoded = json.dumps(primitive(value), allow_nan=False, separators=(",", ":")) + "\n"
        if stream.tell() + len(encoded) > MAX_FILE_BYTES:
            raise ValueError("evidence_size_limit")
        stream.write(encoded)

    def finish(self) -> None:
        """Flush all owned streams and hash only explicitly created evidence files."""
        for stream in self._streams.values():
            if not stream.closed:
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
        hashes = {}
        for name in sorted(self._created):
            path = self.root / name
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("evidence_size_limit")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write("hashes.json", hashes)
