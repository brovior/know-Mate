"""Native Count lifetime with real guard, event sink and diagnostic writer; no Office activation."""
from pathlib import Path
import os
import re
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/ppt_reader"
CSC = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
GAC = Path(os.environ.get("WINDIR", "C:/Windows")) / "assembly/GAC_MSIL"
PIA = GAC / "Microsoft.Office.Interop.PowerPoint/15.0.0.0__71e9bce111e9429c/Microsoft.Office.Interop.PowerPoint.dll"
OFFICE = GAC / "office/15.0.0.0__71e9bce111e9429c/office.dll"


def run(command):
    """Run a compiler or isolated native fixture with a bounded timeout."""
    result = subprocess.run(list(map(str, command)), capture_output=True, text=True,
                            errors="replace", timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows Framework and registered Office typelib required")
@pytest.mark.parametrize("arch", ["x86", "x64"])
@pytest.mark.parametrize("fault_copy", [False, True], ids=["shipping-source", "cleanup-fault-copy"])
def test_native_raw_count_integrated(tmp_path, arch, fault_copy):
    """Compile shipping source or its explicit fault copy with real events and diagnostics."""
    if not all(p.is_file() for p in (CSC, PIA, OFFICE)):
        pytest.skip("Framework compiler and Office PIA are required; no activation or installation")
    raw = (SOURCE / "PptRawCount.cs").read_text(encoding="utf-8")
    assert "Func<IntPtr,int> Clear" not in raw and "Action<string,Action> Cleanup" not in raw
    # Keep fatal classification synchronized with the actual guard.
    guard = (SOURCE / "PptShutdownGuard.cs").read_text(encoding="utf-8")
    assert set(re.findall(r"case (0x[0-9A-F]+):", raw)) == set(re.findall(r"case (0x[0-9A-F]+):", guard))
    harness = (ROOT / "knowmate/tests/ppt_raw_count_harness.cs").read_text(encoding="utf-8")
    if fault_copy:
        anchor = "internal static class RawCount {"
        assert raw.count(anchor) == 1 and raw.count("try {action();}") == 1
        raw = raw.replace(anchor, anchor + "\n internal static Func<IntPtr,int> Clear=NativeClear;"
                          "\n internal static Action<string,Action> Cleanup=(label,action)=>action();")
        raw = raw.replace("try {action();}", "try {Cleanup(label,action);}")
        raw = raw.replace("HResult(NativeClear(owned))", "HResult(Clear(owned))")
    else:
        start = harness.index(" const int FatalA=")
        end = harness.index(" static void ValidateCount(", start)
        harness = harness[:start] + harness[end:]
        harness = harness.replace("  FailureMatrix(lib);", "")
        harness = harness.replace('"fatal_cleanup","cleanup_failure",', "")
        harness = "\n".join(line for line in harness.splitlines()
                            if not line.startswith('  if(mode=="cleanup_failure"'))
        harness = harness.replace("finally{RawCount.Clear=RawCount.NativeClear;}", "finally{}")
    raw_file = tmp_path / "RawCount.cs"
    raw_file.write_text(raw, encoding="utf-8")
    harness_file = tmp_path / "Harness.cs"
    harness_file.write_text(harness, encoding="utf-8")
    policy = tmp_path / "Policy.cs"
    policy.write_text("namespace AegisDesk.PptReader { internal static class ShutdownBuildPolicy { public const bool Enabled=true; } }", encoding="utf-8")
    exe = tmp_path / "RawCount.exe"
    sources = [SOURCE / name for name in ("PptReader.cs", "PptCallerQuit.cs", "PptShutdownGuard.cs", "PptShutdownState.cs",
               "PptShutdownEvents.cs", "PptOwnedMetadata.cs", "PptSinkLoader.cs", "PptSetupInstallRootPolicy.cs", "PptPackageIdentity.cs", "PptEventSignatures.cs", "PptEventValidator.cs")]
    run([CSC, "/nologo", "/optimize+", "/reference:Microsoft.CSharp.dll", "/reference:System.Core.dll",
         "/reference:System.Web.Extensions.dll", "/platform:" + arch, "/out:" + str(exe),
         *sources, ROOT / "scripts/ppt_shared/PptExtractionCore.cs", raw_file, harness_file, policy])
    events = tmp_path / "AegisDesk.PptReader.Events.dll"
    run([CSC, "/nologo", "/optimize+", "/target:library", "/platform:" + arch, "/out:" + str(events),
         "/reference:" + str(exe), "/reference:" + str(PIA), "/reference:" + str(OFFICE),
         ROOT / "scripts/ppt_events/PptEventSink.cs"])
    output = run([exe, events, tmp_path / "diagnostics"])
    (tmp_path / "run.log").write_text(output, encoding="utf-8")
    assert "PASS INTEGRATED" in output and "owned_collection_refs=0 mode=alias" in output
    if fault_copy:
        assert "failure=clear_before_fatal" in output and "fixture_recovery=1" in output
    print(output)
