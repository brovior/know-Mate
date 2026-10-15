"""Exercise actual Windows directory sharing and ACL APIs in an isolated NTFS root."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


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
    compiled = subprocess.run(
        [str(compiler), "/nologo", "/main:NativeAclHarness", f"/out:{executable}",
         "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll", "/reference:System.Security.dll",
         *(str(path) for path in sources)],
        check=False, capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run(
        [str(executable), str(tmp_path / "native-anchor")],
        check=False, capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode == 77:
        pytest.skip(result.stdout.strip())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS native inherited parent" in result.stdout
