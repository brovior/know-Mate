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
    build = {"commit": "a" * 40, "source_fingerprint": "12345678", "dirty": True, "ppt_auto_install_mode": "disabled"}
    for name in PAYLOAD_FILES:
        (tmp_path / name).write_bytes(b"synthetic payload")
    manifest = {
        "schema": 1, "reader_version": "1.0.5.15", "protocol": 3, "payload_schema": 2,
        "prog_id": "AegisDesk.PptReader", "reader_class": "AegisDesk.PptReader.Connect",
        "reader_clsid": "287A80D2-804A-46D3-A0E9-CA31C6FC3D09",
        "reader_iid": "9A20F4A6-6364-4BA7-930B-3E8857BEAF60",
        "auto_install_validated": False, "auto_install_mode": "disabled",
        "auto_install_enabled": False, "auto_shutdown_enabled": False, "build": build,
        "assembly_versions": {name: "1.0.5.15" for name in PAYLOAD_FILES},
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


@pytest.mark.parametrize("change", ["missing", "changed", "version", "gate", "boolean", "files", "events_missing", "events_changed", "old_payload_schema"])
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
    elif change == "events_missing":
        (path / "AegisDesk.PptReader.Events.dll").unlink()
    elif change == "events_changed":
        (path / "AegisDesk.PptReader.Events.dll").write_bytes(b"changed events")
    elif change == "old_payload_schema":
        manifest["payload_schema"] = 1
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
    """Managed-host contracts use the Count fixture; native product integration is tested separately."""
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Windows .NET Framework compiler needed")
    root = Path(__file__).resolve().parents[2]
    dll = tmp_path / "AegisDesk.PptReader.dll"
    policy = tmp_path / "PptBuildPolicy.cs"
    policy.write_text('namespace AegisDesk.PptReader { internal static class ShutdownBuildPolicy { public const bool Enabled = true; } }')
    commands = [[str(compiler), "/nologo", "/target:library", f"/out:{dll}",
                 "/reference:Microsoft.CSharp.dll", "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll",
                 str(root / "scripts/ppt_reader/PptReader.cs"), str(root / "scripts/ppt_reader/PptCallerQuit.cs"), str(root / "scripts/ppt_shared/PptExtractionCore.cs"),
                 str(root / "scripts/ppt_reader/PptShutdownEvents.cs"), str(root / "scripts/ppt_reader/PptOwnedMetadata.cs"),
                 str(root / "scripts/ppt_reader/PptShutdownGuard.cs"),
                 str(root / "knowmate/tests/ppt_managed_count_fixture.cs"),
                 *[str(root / "scripts/ppt_reader" / name) for name in ("PptShutdownState.cs", "PptSinkLoader.cs", "PptPackageIdentity.cs", "PptEventSignatures.cs", "PptEventValidator.cs")], str(policy)]]
    for name, args in (("ppt_reader_harness.cs", []),
                       ("ppt_probe_harness.cs", ["AegisDesk.PptReader.Connect"]),
                       ("ppt_probe_connection_harness.cs", ["--production"])):
        exe = tmp_path / (name + ".exe")
        commands.extend([[str(compiler), "/nologo", "/reference:Microsoft.CSharp.dll", "/reference:System.Core.dll", f"/out:{exe}", f"/reference:{dll}", str(root / "knowmate/tests" / name)],
                         [str(exe), str(dll), *args]])
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=30,
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


@pytest.mark.parametrize("mode", ["disabled", "candidate", "released"])
def test_profile_payload_rejects_other_profile_and_source(payload, mode):
    directory, original, build = payload
    manifest = copy.deepcopy(original)
    build = dict(build, ppt_auto_install_mode=mode)
    manifest.update(auto_install_mode=mode, auto_install_enabled=mode != "disabled",
                    auto_install_validated=mode == "released", auto_shutdown_enabled=mode == "candidate")
    manifest["build"] = dict(build)
    assert validate_manifest(manifest, directory, build) == manifest
    for other in {"disabled", "candidate", "released"} - {mode}:
        with pytest.raises(ValueError, match="auto_install_gate"):
            validate_manifest(manifest, directory, dict(build, ppt_auto_install_mode=other))
    with pytest.raises(ValueError, match="source_mismatch"):
        validate_manifest(manifest, directory, dict(build, commit="b" * 40))


def test_runtime_helper_rejects_dll_corruption_and_foreign_source(monkeypatch, payload):
    from knowmate.secure import ppt_installation
    import knowmate.build_info
    directory, manifest, build = payload
    monkeypatch.setattr(ppt_installation.sys, "frozen", True, raising=False)
    monkeypatch.setattr(knowmate.build_info, "get_build_info", lambda: build)
    assert ppt_installation._validate_helper(directory / "PptReaderSetup.exe")
    monkeypatch.setattr(knowmate.build_info, "get_build_info", lambda: dict(build, commit="b" * 40))
    assert not ppt_installation._validate_helper(directory / "PptReaderSetup.exe")
    monkeypatch.setattr(knowmate.build_info, "get_build_info", lambda: build)
    (directory / PAYLOAD_FILES[0]).write_bytes(b"corrupt reader")
    assert not ppt_installation._validate_helper(directory / "PptReaderSetup.exe")


@pytest.mark.parametrize("mode", ["disabled", "candidate", "released"])
def test_manifest_generator_binds_compiled_policy(monkeypatch, payload, mode):
    import importlib.util
    directory, _, info = payload
    info = dict(info, ppt_auto_install_mode=mode)
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("ppt_manifest_policy_test", root / "scripts/ppt_reader/manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def fake_run(filename):
        if filename.endswith("aegisdesk_build_info.py"):
            return {"BUILD_INFO": info}
        return {"_build_info": lambda: info}
    monkeypatch.setattr(module.runpy, "run_path", fake_run)
    policy = directory / "PptBuildPolicy.cs"
    policy.write_text('namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                      f'public const string AutoInstallMode = "{mode}";' + ' } }\n'
                      'namespace AegisDesk.PptReader { internal static class ShutdownBuildPolicy { '
                      f'public const bool Enabled = {str(mode == "candidate").lower()};' + ' } }\n')
    manifest = module.create_manifest(directory, directory)
    assert manifest["auto_install_mode"] == mode
    assert manifest["auto_install_validated"] == (mode == "released")
    policy.write_text('namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                      'public const string AutoInstallMode = "wrong"; } }\n')
    with pytest.raises(ValueError, match="build_policy_changed"):
        module.create_manifest(directory, directory)
