"""Exercise actual Windows directory sharing and ACL APIs in an isolated NTFS root."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_STAGES = frozenset({
    "account_check", "filesystem_check", "fixture_create", "parent_file_rejection",
    "child_file_rejection", "junction_create", "junction_rejection", "fresh_acquire",
    "fresh_verify", "fixture_reset", "legacy_create", "legacy_first_acquire",
    "legacy_second_acquire", "legacy_recheck", "rename_denial", "atomic_create",
    "atomic_replace", "atomic_verify", "lease_release", "sharing_conflict_open",
    "sharing_conflict_acquire", "reopen_after_conflict", "rename_after_release", "cleanup", "complete",
})


def _last_stage(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        output = output.decode("ascii", errors="replace")
    stage = "unavailable"
    for line in (output or "").splitlines():
        match = re.fullmatch(r"PPT_ACL_STAGE ([a-z_]+) elapsed_ms=[0-9]+", line)
        if match and match[1] in _STAGES:
            stage = match[1]
    return stage


def _run_native(command: list[str], phase: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True,
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"PPT ACL timeout: phase={phase} last_stage={_last_stage(error.stdout)} limit_sec=30",
                    pytrace=False)


@pytest.mark.parametrize("phase", ["compile", "harness"])
@pytest.mark.parametrize("output", [None, "PPT_ACL_STAGE atomic_create elapsed_ms=123\n",
    b"PPT_ACL_STAGE atomic_create elapsed_ms=123\n"])
def test_native_timeout_identifies_phase_and_last_stage(monkeypatch, phase, output):
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(command, 30, output=output)
    monkeypatch.setattr(subprocess, "run", timeout)
    expected = "unavailable" if output is None else "atomic_create"
    with pytest.raises(pytest.fail.Exception, match=f"phase={phase} last_stage={expected}"):
        _run_native(["fixture"], phase)


def test_native_stage_output_does_not_repeat_arbitrary_text():
    output = ("confidential-marker\nPPT_ACL_STAGE fresh_acquire elapsed_ms=1\n"
              "PPT_ACL_STAGE confidential_marker elapsed_ms=2\n")
    assert _last_stage(output) == "fresh_acquire"


def test_ppt_native_legacy_directory_lease(tmp_path: Path) -> None:
    """Require native ordinary-user behavior; never substitute a Linux model."""
    compiler = Path(os.environ.get("WINDIR", "")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if os.name != "nt" or not compiler.is_file():
        pytest.skip("Required native Windows ACL/lease gate unexecuted: Windows .NET compiler required")
    root = Path(__file__).resolve().parents[2]
    policy = tmp_path / "PptBuildPolicy.cs"
    policy.write_text('namespace AegisDesk.PptSetup { internal static class BuildPolicy { '
                      'public const string AutoInstallMode = "candidate"; } }', encoding="utf-8")
    sources = [root / "scripts/ppt_reader/PptReaderSetup.cs",
               *sorted((root / "scripts/ppt_reader").glob("PptSetup*.cs")),
               root / "knowmate/tests/ppt_acl_native_harness.cs", policy]
    executable = tmp_path / "NativeAclHarness.exe"
    compiled = _run_native(
        [str(compiler), "/nologo", "/main:NativeAclHarness", f"/out:{executable}",
         "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll", "/reference:System.Security.dll",
         *(str(path) for path in sources)],
        "compile",
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = _run_native(
        [str(executable), str(tmp_path / "native-anchor")],
        "harness",
    )
    if result.returncode == 77:
        pytest.skip(result.stdout.strip())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS native inherited parent" in result.stdout
