"""Create the hash/source manifest after the build verifies CLR assembly versions."""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def create_manifest(payload_dir: Path, build_info_dir: Path) -> dict[str, object]:
    """Bind precompiled payload hashes to the app's generated build identity."""
    from knowmate.secure.ppt_bundle import PAYLOAD_FILES, validate_manifest
    from knowmate.secure.ppt_policy import REQUIRED_VERSION, AUTO_INSTALL_MODES

    build_info = runpy.run_path(str(build_info_dir / "aegisdesk_build_info.py"))["BUILD_INFO"]
    # Guard metadata must describe the source currently being compiled, including
    # dirty C# and Python changes; an old metadata directory is never sufficient.
    current = runpy.run_path(str(REPO_ROOT / "scripts" / "build_guard.py"))["_build_info"]()
    if any(current[key] != build_info.get(key) for key in ("commit", "source_fingerprint", "dirty")):
        raise ValueError("ppt_build_source_changed")
    mode = build_info.get("ppt_auto_install_mode")
    if mode not in AUTO_INSTALL_MODES:
        raise ValueError("ppt_auto_install_mode")
    expected_policy = ('namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                       f'public const string AutoInstallMode = "{mode}";' + ' } }\n')
    if (build_info_dir / "PptBuildPolicy.cs").read_text(encoding="utf-8") != expected_policy:
        raise ValueError("ppt_build_policy_changed")
    manifest: dict[str, object] = {
        "schema": 1, "reader_version": REQUIRED_VERSION, "protocol": 2, "payload_schema": 1,
        "prog_id": "AegisDesk.PptReader", "reader_class": "AegisDesk.PptReader.Connect",
        "reader_clsid": "287A80D2-804A-46D3-A0E9-CA31C6FC3D09",
        "reader_iid": "65152412-DFD3-47EC-8346-3F2675ED0C15",
        "auto_install_mode": mode, "auto_install_enabled": mode != "disabled",
        "auto_install_validated": mode == "released",
        "build": {key: build_info[key] for key in ("commit", "source_fingerprint", "dirty")},
        "assembly_versions": {name: REQUIRED_VERSION for name in PAYLOAD_FILES},
        "files": {name: hashlib.sha256((payload_dir / name).read_bytes()).hexdigest()
                  for name in PAYLOAD_FILES},
    }
    validate_manifest(manifest, payload_dir, build_info)
    (payload_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    """Generate a manifest or report a fixed diagnostic without Office operations."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload-dir", required=True, type=Path)
    parser.add_argument("--build-info-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        create_manifest(args.payload_dir, args.build_info_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ppt_manifest_failed:{type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
