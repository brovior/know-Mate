"""PPT payload provenance failures and shared managed extraction contracts."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from knowmate.secure.ppt_bundle import PAYLOAD_FILES, validate_manifest, verify_payload


@pytest.fixture
def payload(tmp_path):
    build = {"commit": "a" * 40, "source_fingerprint": "12345678", "dirty": True}
    for name in PAYLOAD_FILES:
        (tmp_path / name).write_bytes(b"synthetic payload")
    manifest = {
        "schema": 1, "reader_version": "1.0.0.0", "protocol": 2, "payload_schema": 1,
        "prog_id": "AegisDesk.PptReader", "reader_class": "AegisDesk.PptReader.Connect",
        "reader_clsid": "287A80D2-804A-46D3-A0E9-CA31C6FC3D09",
        "reader_iid": "65152412-DFD3-47EC-8346-3F2675ED0C15",
        "auto_install_validated": False, "build": build,
        "assembly_versions": {name: "1.0.0.0" for name in PAYLOAD_FILES},
        "files": {name: hashlib.sha256(b"synthetic payload").hexdigest() for name in PAYLOAD_FILES},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path, manifest, build


def test_payload_hash_bound_to_app_build(payload):
    path, manifest, build = payload
    assert verify_payload(path, build) == manifest
    other = dict(build, source_fingerprint="87654321")
    with pytest.raises(ValueError, match="source_mismatch"):
        verify_payload(path, other)


@pytest.mark.parametrize("change", ["missing", "changed", "version", "gate", "boolean", "files"])
def test_payload_rejects_missing_changed_or_unverified_metadata(payload, change):
    path, original, build = payload
    manifest = copy.deepcopy(original)
    if change == "missing":
        (path / PAYLOAD_FILES[0]).unlink()
    elif change == "changed":
        (path / PAYLOAD_FILES[0]).write_bytes(b"stale or corrupt assembly")
    elif change == "version":
        manifest["assembly_versions"][PAYLOAD_FILES[0]] = "0.1.3.0"
    elif change == "gate":
        manifest["auto_install_validated"] = True
    elif change == "boolean":
        manifest["schema"] = True
    elif change == "files":
        manifest["files"]["foreign.dll"] = "a" * 64
    with pytest.raises(ValueError):
        validate_manifest(manifest, path, build)


def test_frozen_selftest_payload_missing_fails_without_launch(monkeypatch, tmp_path):
    import sys
    from knowmate.app import selftest
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    failures = []
    selftest._check_ppt_payload(failures)
    assert failures and "PPT payload" in failures[0]


def test_production_managed_harness_on_windows(tmp_path):
    """Actual compiler and managed contracts require no Office or registry access."""
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Windows .NET Framework compiler needed")
    root = Path(__file__).resolve().parents[2]
    dll = tmp_path / "AegisDesk.PptReader.dll"
    commands = [[str(compiler), "/nologo", "/target:library", f"/out:{dll}",
                 "/reference:Microsoft.CSharp.dll", "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll",
                 str(root / "scripts/ppt_reader/PptReader.cs"), str(root / "scripts/ppt_shared/PptExtractionCore.cs")]]
    for name, args in (("ppt_reader_harness.cs", []),
                       ("ppt_probe_harness.cs", ["AegisDesk.PptReader.Connect"]),
                       ("ppt_probe_connection_harness.cs", ["--production"])):
        exe = tmp_path / (name + ".exe")
        commands.extend([[str(compiler), "/nologo", f"/out:{exe}", str(root / "knowmate/tests" / name)],
                         [str(exe), str(dll), *args]])
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SECRET" not in result.stdout + result.stderr


def test_manifest_generation_rejects_metadata_for_changed_source(monkeypatch, payload):
    """Prebuild metadata cannot be reused after source changed during compilation."""
    import importlib.util
    path, _, build = payload
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("ppt_build_manifest_test", root / "scripts/ppt_reader/manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_run(filename):
        if filename.endswith("aegisdesk_build_info.py"):
            return {"BUILD_INFO": build}
        return {"_build_info": lambda: dict(build, source_fingerprint="87654321")}

    monkeypatch.setattr(module.runpy, "run_path", fake_run)
    with pytest.raises(ValueError, match="source_changed"):
        module.create_manifest(path, path)
