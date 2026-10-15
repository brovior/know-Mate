"""Validate bundled PPT payload hashes and source identity without COM or elevation."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

from knowmate.secure.ppt_policy import REQUIRED_VERSION, AUTO_INSTALL_MODES, auto_install_mode

PAYLOAD_FILES = ("AegisDesk.PptReader.dll", "PptReaderSetup.exe")


def payload_directory() -> Path:
    """Locate precompiled payloads in the portable bundle or source checkout."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "ppt_reader"
    return Path(__file__).resolve().parents[2] / "scripts" / "ppt_reader" / "bin"


def validate_manifest(
    manifest: Any, directory: Path, build_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Reject missing, changed, mismatched or unverified payload metadata."""
    if not isinstance(manifest, dict):
        raise ValueError("ppt_manifest_schema")
    expected = {
        "schema": 1, "reader_version": REQUIRED_VERSION, "protocol": 3, "payload_schema": 1,
        "prog_id": "AegisDesk.PptReader", "reader_class": "AegisDesk.PptReader.Connect",
        "reader_clsid": "287A80D2-804A-46D3-A0E9-CA31C6FC3D09",
        "reader_iid": "9A20F4A6-6364-4BA7-930B-3E8857BEAF60",
    }
    for key, value in expected.items():
        if type(manifest.get(key)) is not type(value) or manifest[key] != value:
            raise ValueError("ppt_manifest_identity")
    mode = (build_info.get("ppt_auto_install_mode") if build_info is not None else auto_install_mode())
    if not isinstance(mode, str) or mode not in AUTO_INSTALL_MODES:
        raise ValueError("ppt_auto_install_mode")
    if (manifest.get("auto_install_mode") != mode
            or type(manifest.get("auto_install_enabled")) is not bool
            or manifest["auto_install_enabled"] != (mode != "disabled")
            or type(manifest.get("auto_install_validated")) is not bool
            or manifest["auto_install_validated"] != (mode == "released")):
        raise ValueError("ppt_auto_install_gate")
    if (type(manifest.get("auto_shutdown_enabled")) is not bool
            or manifest["auto_shutdown_enabled"] != (mode == "candidate")):
        raise ValueError("ppt_auto_shutdown_gate")
    source = manifest.get("build")
    if not isinstance(source, dict) or not isinstance(source.get("commit"), str):
        raise ValueError("ppt_manifest_build")
    if not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise ValueError("ppt_manifest_build")
    dirty, fingerprint = source.get("dirty"), source.get("source_fingerprint")
    if type(dirty) is not bool or not isinstance(fingerprint, str):
        raise ValueError("ppt_manifest_build")
    if (dirty and not re.fullmatch(r"[0-9a-f]{8}", fingerprint)) or (not dirty and fingerprint):
        raise ValueError("ppt_manifest_build")
    if build_info is not None:
        if any(source.get(key) != build_info.get(key) for key in ("commit", "dirty", "source_fingerprint")):
            raise ValueError("ppt_manifest_source_mismatch")
    hashes, versions = manifest.get("files"), manifest.get("assembly_versions")
    if not isinstance(hashes, dict) or not isinstance(versions, dict):
        raise ValueError("ppt_manifest_files")
    if set(hashes) != set(PAYLOAD_FILES) or set(versions) != set(PAYLOAD_FILES):
        raise ValueError("ppt_manifest_files")
    for name in PAYLOAD_FILES:
        digest = hashes[name]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("ppt_manifest_hash")
        if versions[name] != REQUIRED_VERSION:
            raise ValueError("ppt_manifest_version")
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("ppt_payload_missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("ppt_payload_hash_mismatch")
    return dict(manifest)


def verify_payload(
    directory: Path | None = None, build_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read and validate payload files; do not start setup, CLR or Office."""
    directory = directory if directory is not None else payload_directory()
    path = directory / "manifest.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("ppt_manifest_missing")
    return validate_manifest(json.loads(path.read_text(encoding="utf-8")), directory, build_info)
