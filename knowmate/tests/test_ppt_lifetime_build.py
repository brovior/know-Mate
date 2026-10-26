"""Pin the isolated lifetime variant without compiling a DLL or registering COM."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.ppt_reader import build_lifetime as builder

ROOT = Path(__file__).resolve().parents[2]


def _source_tree(tmp_path: Path) -> tuple[Path, Path]:
    root, output = tmp_path / "source", tmp_path / "output"
    for relative in (*builder.CORE, "scripts/ppt_reader/build_lifetime.py"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    output.mkdir()
    return root, output


def test_generated_sources_change_only_the_isolated_version(tmp_path: Path) -> None:
    root, output = _source_tree(tmp_path)
    before = builder.source_hashes(root)
    copied = builder.copy_sources(output / "generated", root)
    assert builder.source_hashes(root) == before
    text = (output / "generated" / builder.CORE[0]).read_text()
    assert 'AssemblyVersion("1.0.7.0")' in text
    assert 'AssemblyFileVersion("1.0.7.0")' in text
    assert set(copied) == set(builder.CORE)
    assert not any("Setup" in path or "Events" in path for path in copied)
    with pytest.raises(FileExistsError):
        builder.copy_sources(output / "generated", root)


def test_manifest_never_claims_normal_install_or_live_approval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root, output = _source_tree(tmp_path)
    (output / builder.DLL).write_bytes(b"unexecuted synthetic artifact")
    monkeypatch.setattr(builder, "_fixture_hashes", lambda: {
        "fixture_generator_sha256": "a" * 64, "fixture_template_sha256": "b" * 64})
    copied = builder.copy_sources(output / "generated", root)
    result = builder.create_manifest(output, copied, root)
    assert result == json.loads((output / builder.MANIFEST).read_text())
    assert result["reader_version"] == "1.0.7.0" and result["protocol"] == 3
    assert result["reader_iid"] == builder.IID
    assert result["effective_internal_quit"] is result["live_enabled"] is False
    assert result["provisioning"] == "UNIMPLEMENTED"
    assert result["native_validation"] == {"x86": "UNEXECUTED", "x64": "UNEXECUTED"}
    assert set(result["files"]) == {builder.DLL}
    assert not (output / "manifest.json").exists()
    assert not (output / "PptReaderSetup.exe").exists()
    with pytest.raises(FileExistsError):
        builder.create_manifest(output, copied, root)


def test_missing_or_linked_sources_cannot_be_pinned(tmp_path: Path) -> None:
    root, output = _source_tree(tmp_path)
    source = root / builder.CORE[1]
    source.unlink()
    with pytest.raises(ValueError, match="build_file"):
        builder.source_hashes(root)
    source.symlink_to(ROOT / builder.CORE[1])
    with pytest.raises(ValueError, match="build_file"):
        builder.source_hashes(root)


def test_added_executable_initializer_invalidates_source_set(tmp_path: Path) -> None:
    root, _ = _source_tree(tmp_path)
    before = builder.source_hashes(root)
    (root / "scripts/__init__.py").write_text("# changed executable input\n")
    after = builder.source_hashes(root)
    assert set(after) - set(before) == {"scripts/__init__.py"}


def test_wrong_source_version_is_rejected(tmp_path: Path) -> None:
    root, output = _source_tree(tmp_path)
    source = root / builder.CORE[0]
    source.write_text(source.read_text().replace("1.0.5.16", "2.0.0.0"))
    with pytest.raises(ValueError, match="source_version"):
        builder.copy_sources(output / "generated", root)


def test_non_windows_build_fails_before_creating_any_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(builder.sys, "platform", "linux")
    with pytest.raises(ValueError, match="windows_build_required"):
        builder.build(tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("change_source", [False, True])
def test_build_recipe_is_dll_only_and_rejects_inputs_changed_during_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change_source: bool,
) -> None:
    root, _ = _source_tree(tmp_path)
    output = tmp_path / "candidate"
    calls: list[list[str]] = []
    monkeypatch.setattr(builder.sys, "platform", "win32")
    monkeypatch.setenv("WINDIR", str(tmp_path / "fake-windows"))
    monkeypatch.setattr(builder, "_fixture_hashes", lambda: {
        "fixture_generator_sha256": "a" * 64, "fixture_template_sha256": "b" * 64})

    def fake_tool(command: list[str], *, check: bool) -> None:
        assert check
        calls.append(command)
        if command[0].endswith("csc.exe"):
            (output / builder.DLL).write_bytes(b"mocked compilation, not an executable artifact")
        elif change_source:
            source = root / builder.CORE[0]
            source.write_text(source.read_text() + "\n// changed during inspection\n")

    monkeypatch.setattr(builder.subprocess, "run", fake_tool)
    if change_source:
        with pytest.raises(ValueError, match="source_changed"):
            builder.build(output, root)
        assert not (output / builder.MANIFEST).exists()
    else:
        assert builder.build(output, root) == output
        assert (output / builder.MANIFEST).exists()
    assert len(calls) == 2
    assert "/target:library" in calls[0]
    assert "/define:" + builder.DEFINE in calls[0]
    assert all("Interop" not in arg and "Events" not in arg and "Setup" not in arg
               and "RegAsm" not in arg for command in calls for arg in command)


def _preprocess(path: Path, *defines: str) -> str:
    cpp = shutil.which("cpp")
    if cpp is None:
        pytest.skip("C preprocessor required for source-only conditional-branch check")
    result = subprocess.run([cpp, "-P", "-traditional-cpp", *["-D" + flag for flag in defines], str(path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_lifetime_branch_excludes_all_shutdown_guard_and_capture_code() -> None:
    text = _preprocess(ROOT / builder.CORE[0], builder.DEFINE)
    for forbidden in ("PptShutdownGuard", "PptSinkLoader", "PptLifecycleTrace", "BeginExternal",
                      "ArmDeferredQuit", "InvokeExistingShutdown", "ObservationFence"):
        assert forbidden not in text
    assert "ComDefaultInterface(typeof(IPptReaderLifetimeDiagnostic))" in text
    assert 'return "not_supported";' in text
    assert "PptExtractionCore.ExtractSlide(application, presentationPath, slide)" in text
    for mutation in (".Quit(", ".Add(", ".Caption", "AutomationSecurity", "ReleaseComObject"):
        assert mutation not in text


def test_normal_and_capture_interfaces_keep_their_existing_defaults() -> None:
    normal = _preprocess(ROOT / builder.CORE[0])
    capture = _preprocess(ROOT / builder.CORE[0], "AEGIS_PPT_EXTERNAL_DIAGNOSTIC", "AEGIS_PPT_DEFERRED_DIAGNOSTIC")
    assert "ComDefaultInterface(typeof(IPptReaderClientShutdown))" in normal
    assert "ComDefaultInterface(typeof(IPptExternalBootstrap))" in capture
    assert "IPptReaderLifetimeDiagnostic" not in normal + capture
    assert "new PptShutdownGuard" in normal and "new PptShutdownGuard" in capture
    for old_iid in ("65152412-DFD3-47EC-8346-3F2675ED0C15", "9A20F4A6-6364-4BA7-930B-3E8857BEAF60"):
        assert old_iid in normal and old_iid in capture


def test_identity_interface_is_exclusive_and_native_only() -> None:
    text = _preprocess(ROOT / builder.CORE[1], builder.DEFINE)
    assert builder.IID in text
    assert "GetCurrentProcessId()" in text and "GetProcessTimes(GetCurrentProcess()" in text
    assert 'public string GetHostProcessIdentity()' in text
    assert "RequireHostThread();" in text
    assert "CultureInfo.InvariantCulture" in text
    assert "GetHostProcessIdentity" not in _preprocess(ROOT / builder.CORE[1])
    for forbidden in (".Quit(", "new PptShutdownGuard", ".Add(", "File.Write", "Console."):
        assert forbidden not in text
