"""Real Framework AppDomain boundaries with external callers; no Office activation."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason=".NET Framework required")


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.mark.parametrize("arch", ["x86", "x64"])
@pytest.mark.parametrize("apartment", ["STA", "MTA"])
@pytest.mark.parametrize("observe_unload", [False, True])
def test_external_metadata_boundary(tmp_path, arch, apartment, observe_unload):
    """Exercise production metadata code across LoadFrom and real child domains."""
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if not compiler.exists():
        pytest.skip("Framework csc.exe unavailable")
    source = (ROOT / "scripts/ppt_reader/PptPackageIdentity.cs").read_text(encoding="utf-8")
    if observe_unload:
        # Diagnostic-only observer: no production seam or change to metadata decisions.
        anchor = "if (AppDomain.CurrentDomain.ReflectionOnlyGetAssemblies().Length != 0)"
        assert source.count(anchor) == 1
        source = source.replace(anchor, "AppDomain.CurrentDomain.DomainUnload += delegate { "
            'File.AppendAllText(Environment.GetEnvironmentVariable("AEGIS_METADATA_UNLOAD_LOG"), '
            '"unloaded\\n"); };\n            ' + anchor)
    metadata = tmp_path / "Metadata.cs"
    metadata.write_text(source, encoding="utf-8")
    packages = []
    for name in ("package", "other"):
        directory = tmp_path / name
        directory.mkdir()
        stamp = directory / "Stamp.cs"
        stamp.write_text('using System.Reflection; [assembly:AssemblyVersion("1.0.5.16")] '
                         f'public class Stamp {{public static string Value="{name}";}}', encoding="utf-8")
        dll = directory / "AegisDesk.PptReader.dll"
        _run([str(compiler), "/nologo", "/target:library", f"/out:{dll}", str(metadata), str(stamp)])
        packages.append(dll)
    runner = tmp_path / "runner"
    runner.mkdir()
    exe = runner / "BoundaryHarness.exe"
    _run([str(compiler), "/nologo", f"/platform:{arch}", f"/out:{exe}",
          str(ROOT / "knowmate/tests/ppt_metadata_boundary_harness.cs")])
    env = os.environ.copy()
    unload = tmp_path / "unload.log"
    env["AEGIS_METADATA_UNLOAD_LOG"] = str(unload)
    result = _run([str(exe), apartment, *map(str, packages)], env=env)
    assert "PASS loaded_mismatch_rejected" in result.stdout
    assert "PASS bad_metadata_rejected" in result.stdout
    if observe_unload:
        # Success, invalid metadata, and alternate MVID each unload their own domain.
        # Wrong hash fails before child creation.
        assert unload.read_text().splitlines() == ["unloaded"] * 3
