"""Whole-package synthetic fixtures that work in source and frozen diagnostics."""
from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from scripts.diagnostics.ppt_capture.bundle import reject_links
from scripts.diagnostics.ppt_lifetime.fixture import canonical_members


@dataclass(frozen=True)
class SyntheticFixture:
    """One generated file, authenticated by its complete fixed OPC contents."""
    path: Path
    sha256: str


def create_fixture(directory: Path) -> SyntheticFixture:
    """Create only the built-in deck; never accept an input-document path."""
    reject_links(directory)
    directory.mkdir(mode=0o700, exist_ok=False)
    path = directory / "synthetic.pptx"
    with ZipFile(path, "x", compression=ZIP_DEFLATED) as archive:
        for name, content in canonical_members().items():
            archive.writestr(name, content)
    return SyntheticFixture(path, hashlib.sha256(path.read_bytes()).hexdigest())


def verify_fixture(fixture: SyntheticFixture) -> None:
    """Reject extra, substituted or oversized members before any Office operation."""
    reject_links(fixture.path)
    if fixture.path.name != "synthetic.pptx" or not 0 < fixture.path.stat().st_size <= 4 * 1024 * 1024:
        raise ValueError("fixture_invalid")
    expected = canonical_members()
    with ZipFile(fixture.path) as archive:
        infos = archive.infolist()
        if len(infos) != len(expected) or {info.filename for info in infos} != set(expected):
            raise ValueError("fixture_members")
        for info in infos:
            if (info.file_size != len(expected[info.filename]) or info.flag_bits & 1
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or archive.read(info) != expected[info.filename]):
                raise ValueError("fixture_content")
    if hashlib.sha256(fixture.path.read_bytes()).hexdigest() != fixture.sha256:
        raise ValueError("fixture_digest")
