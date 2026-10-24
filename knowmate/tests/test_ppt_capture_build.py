"""Ensure diagnostic packaging cannot reuse normal versions or claim native approval."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ppt_reader import build_capture
from knowmate.secure.ppt_policy import REQUIRED_VERSION


def source_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Construct only controlled build inputs without compiling or starting Office."""
    root = tmp_path / "repo"
    for relative in ("scripts/ppt_reader/PptReader.cs", "scripts/ppt_reader/PptCallerQuit.cs", "scripts/ppt_events/PptEventSink.cs",
                     "scripts/ppt_shared/PptExtractionCore.cs", "scripts/diagnostics/ppt_capture/report.py",
                     "knowmate/secure/ppt_capture_native.py", "scripts/ppt_reader/build_capture.py",
                     "scripts/ppt_reader/build.ps1", "scripts/build_guard.py", "scripts/ppt_reader/manifest.py",
                     "scripts/ppt_reader/ppt_identity_controller.py", "knowmate/secure/ppt_launch.py",
                     "knowmate/build_info.py", "knowmate/version.py",
                     "knowmate/tests/ppt_capture_buffer_harness.cs", "knowmate/tests/test_ppt_capture_buffer_native.py"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('version = "1.0.5.10"\n', encoding="utf-8")
    info = tmp_path / "info"
    info.mkdir()
    (info / "PptBuildPolicy.cs").write_text("candidate\n", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    for name in (*build_capture.PAYLOADS, build_capture.COLLECTOR):
        (output / name).write_bytes(name.encode("ascii"))
    return root, info, output


def test_generated_version_does_not_change_normal_product_sources(tmp_path: Path) -> None:
    root, info, output = source_tree(tmp_path)
    generated = build_capture.copy_sources(output / "generated", info, root)
    assert REQUIRED_VERSION == "1.0.5.10"
    assert '"1.0.5.10"' in (root / "scripts/ppt_reader/PptReader.cs").read_text()
    assert '"1.0.6.0"' in (output / "generated/scripts/ppt_reader/PptReader.cs").read_text()
    assert "CaptureAssemblyVersion.cs" in generated
    assert generated["scripts/ppt_reader/PptReader.cs"] != build_capture.digest(root / "scripts/ppt_reader/PptReader.cs")


def test_capture_manifest_keeps_native_live_locked_and_helper_schema_separate(tmp_path: Path) -> None:
    root, info, output = source_tree(tmp_path)
    copied = build_capture.copy_sources(output / "generated", info, root)
    build = dict(commit="a" * 40, source_fingerprint="12345678", dirty=True)
    result = build_capture.create_manifests(output, build, copied, root)
    assert result["schema"] == 3 and result["capture_layout"] == 1
    assert result["default_enabled"] is False
    assert result["native_validation"] == dict(x86="UNEXECUTED", x64="UNEXECUTED", live_enabled=False)
    assert set(result["files"]) == {*build_capture.PAYLOADS, build_capture.COLLECTOR}
    assert result["source_files"]["knowmate/secure/ppt_capture_native.py"] == build_capture.digest(root / "knowmate/secure/ppt_capture_native.py")
    assert {"scripts/ppt_reader/ppt_identity_controller.py", "knowmate/secure/ppt_launch.py"} <= result["source_files"].keys()
    helper = json.loads((output / "manifest.json").read_text())
    assert helper["schema"] == 1 and helper["reader_version"] == "1.0.6.0"
    assert helper["auto_install_validated"] is False
    assert set(helper["files"]) == set(build_capture.PAYLOADS)
    with pytest.raises(FileExistsError):
        build_capture.create_manifests(output, build, copied, root)


def test_stale_or_non_candidate_metadata_is_rejected_before_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = dict(commit="a" * 40, source_fingerprint="12345678", dirty=True)
    build = dict(current, ppt_auto_install_mode="released")
    monkeypatch.setattr(build_capture.runpy, "run_path", lambda path: {"BUILD_INFO": build} if path.endswith("aegisdesk_build_info.py") else {"_build_info": lambda: current})
    with pytest.raises(ValueError, match="candidate_required"):
        build_capture.verify_metadata(tmp_path)
    build.update(ppt_auto_install_mode="candidate", source_fingerprint="stale")
    with pytest.raises(ValueError, match="source_changed"):
        build_capture.verify_metadata(tmp_path)


def test_added_package_initializer_changes_the_exact_source_set(tmp_path: Path) -> None:
    root, _, _ = source_tree(tmp_path)
    before = set(build_capture.source_paths(root))
    initializer = root / "scripts/__init__.py"
    initializer.write_text("# Newly executable package initialization.\n", encoding="utf-8")
    assert set(build_capture.source_paths(root)) - before == {initializer}
