"""Independent source lock and truthful diagnostic-only package validation."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scripts.diagnostics.ppt_capture.gate import digest, read_json

LIVE_REVIEWED = False
LIVE_LOCK_REASON = "lifetime_live_locked_windows_office_provisioning_unexecuted"
VERSION = "1.0.7.0"
READER_IID = "F71674D8-CB02-405A-87D3-59177E1A1487"


def require_live_review() -> None:
    """Reject every live/private entry before native imports or side effects."""
    if not LIVE_REVIEWED:
        raise RuntimeError(LIVE_LOCK_REASON)


def validate_package(package: Path, source: Path, pins: dict[str, Any]) -> dict[str, Any]:
    """Verify only the lifetime manifest; never use normal install/selection contracts."""
    from scripts.ppt_reader.build_lifetime import source_paths
    from .fixture import generator_sha256, template_sha256
    path = package / "lifetime-manifest.json"
    manifest = read_json(path)
    expected = dict(schema=1, variant="lifetime_diagnostic", reader_version=VERSION,
                    protocol=3, reader_iid=READER_IID,
                    reader_clsid="287A80D2-804A-46D3-A0E9-CA31C6FC3D09",
                    prog_id="AegisDesk.PptReader", effective_internal_quit=False,
                    live_enabled=False, provisioning="UNIMPLEMENTED")
    if any(type(manifest.get(key)) is not type(value) or manifest[key] != value
           for key, value in expected.items()):
        raise ValueError("lifetime_package_identity")
    if digest(path) != pins.get("package_manifest_sha256"):
        raise ValueError("lifetime_manifest_pin")
    if manifest.get("fixture_generator_sha256") != generator_sha256() or manifest.get("fixture_template_sha256") != template_sha256():
        raise ValueError("lifetime_fixture_generator_pin")
    files, sources = manifest.get("files"), manifest.get("source_files")
    if (type(files) is not dict or set(files) != {"AegisDesk.PptReader.dll"}
            or type(sources) is not dict
            or set(sources) != {path.relative_to(source).as_posix() for path in source_paths(source)}):
        raise ValueError("lifetime_package_pin_set")
    for base, entries in ((package, files), (source, sources)):
        for name, sha in entries.items():
            relative = Path(name)
            if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                    or not re.fullmatch(r"[0-9a-f]{64}", str(sha)) or digest(base / relative) != sha):
                raise ValueError("lifetime_package_pin_changed")
    installed = Path(pins["installed_reader"])
    if digest(installed) != files["AegisDesk.PptReader.dll"]:
        raise ValueError("lifetime_installed_artifact_pin")
    return manifest
