"""Read-only capture package checks and the deliberately closed live review gate."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .bundle import primitive, reject_links

LIVE_REVIEWED = False
LIVE_LOCK_REASON = "capture_live_locked_native_x86_x64_and_package_office_review_unexecuted"
PAYLOADS = {"AegisDesk.PptReader.dll", "AegisDesk.PptReader.Events.dll", "PptReaderSetup.exe", "PptCaptureCollector.exe"}


def require_live_review() -> None:
    """Fail before native imports, resource creation, or Office on every live entry."""
    if not LIVE_REVIEWED:
        raise RuntimeError(LIVE_LOCK_REASON)


def read_json(path: Path, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    """Read bounded, nonlinked primitive metadata with duplicate-key rejection."""
    reject_links(path)
    if path.stat().st_size > max_bytes:
        raise ValueError("metadata_size")
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_metadata_key")
            result[key] = value
        return result
    value = primitive(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs))
    if type(value) is not dict:
        raise ValueError("metadata_object")
    return value


def digest(path: Path) -> str:
    """Hash a regular, nonlinked pin outside the request interval."""
    reject_links(path)
    if not path.is_file():
        raise ValueError("pin_file_missing")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_package(package: Path, source: Path, pins: dict[str, Any]) -> dict[str, Any]:
    """Bind new diagnostic binaries, current source, helper and synthetic fixture pins."""
    manifest = read_json(package / "capture-manifest.json")
    expected = dict(schema=3, capture_schema=3, capture_layout=1, reader_version="1.0.6.0",
                    protocol=3, mode="candidate", default_enabled=False)
    if any(type(manifest.get(k)) is not type(v) or manifest[k] != v for k, v in expected.items()):
        raise ValueError("capture_package_identity")
    if digest(package / "capture-manifest.json") != pins.get("package_manifest_sha256"):
        raise ValueError("capture_manifest_pin")
    files, sources = manifest.get("files"), manifest.get("source_files")
    if type(files) is not dict or set(files) != PAYLOADS or type(sources) is not dict or not sources:
        raise ValueError("capture_package_files")
    from scripts.ppt_reader.build_capture import source_paths
    if set(sources) != {path.relative_to(source).as_posix() for path in source_paths(source)}:
        raise ValueError("capture_source_pin_set")
    for base, entries in ((package, files), (source, sources)):
        for name, sha in entries.items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
                raise ValueError("capture_pin_format")
            if digest(base / relative) != sha:
                raise ValueError("capture_pin_changed")
    if pins.get("version") != "1.0.6.0" or pins.get("helper_sha256") != files["PptReaderSetup.exe"]:
        raise ValueError("capture_helper_pin")
    if digest(Path(pins["fixture_path"])) != pins.get("fixture_sha256"):
        raise ValueError("capture_fixture_pin")
    from .fixture import verify_fixture
    verify_fixture(Path(pins["fixture_path"]), pins)
    if (type(pins.get("fixture_text_length")) is not int or pins["fixture_text_length"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(pins.get("fixture_text_sha256")))):
        raise ValueError("capture_synthetic_expectation")
    return manifest
