"""Compile the production native metadata/connection path with the real event sink."""
from pathlib import Path
import os
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/ppt_reader"


def _run(command):
    result = subprocess.run(list(map(str, command)), capture_output=True, text=True,
                            errors="replace", timeout=90,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows Framework and Office typelib required")
@pytest.mark.parametrize("arch", ["x86", "x64"])
def test_owned_connection_product_path(tmp_path, arch):
    """Use shipping guard/validator/RawCount and PIA sink; never activate Office."""
    windows = Path(os.environ["WINDIR"])
    csc = windows / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    gac = windows / "assembly/GAC_MSIL"
    pia = gac / "Microsoft.Office.Interop.PowerPoint/15.0.0.0__71e9bce111e9429c/Microsoft.Office.Interop.PowerPoint.dll"
    office = gac / "office/15.0.0.0__71e9bce111e9429c/office.dll"
    if not all(p.is_file() for p in (csc, pia, office)):
        pytest.skip("Framework/PIA unavailable")
    policy = tmp_path / "Policy.cs"
    policy.write_text("namespace AegisDesk.PptReader { internal static class ShutdownBuildPolicy { public const bool Enabled=true; } }")
    exe = tmp_path / "OwnedConnection.exe"
    names = ("PptReader.cs", "PptShutdownGuard.cs", "PptShutdownEvents.cs", "PptOwnedMetadata.cs",
             "PptShutdownState.cs", "PptRawCount.cs", "PptSinkLoader.cs", "PptPackageIdentity.cs",
             "PptEventSignatures.cs", "PptEventValidator.cs")
    fixtures = ("ppt_owned_connection_fixture.cs", "ppt_owned_metadata_fixture.cs", "ppt_owned_connection_harness.cs")
    _run([csc, "/nologo", "/optimize+", "/main:Probe", "/platform:" + arch, "/out:" + str(exe),
          "/reference:Microsoft.CSharp.dll", "/reference:System.Core.dll", "/reference:System.Web.Extensions.dll",
          *[SOURCE / n for n in names], ROOT / "scripts/ppt_shared/PptExtractionCore.cs", policy,
          *[ROOT / "knowmate/tests" / n for n in fixtures]])
    events = tmp_path / "AegisDesk.PptReader.Events.dll"
    _run([csc, "/nologo", "/target:library", "/platform:" + arch, "/out:" + str(events),
          "/reference:" + str(exe), "/reference:" + str(pia), "/reference:" + str(office),
          ROOT / "scripts/ppt_events/PptEventSink.cs"])
    output = _run([exe, events])
    (tmp_path / "run.log").write_text(output, encoding="utf-8")
    assert "PRODUCT_OWNED_RESULT cases=38" in output
    assert output.count("PRODUCT_OWNED_PASS") == 38
