"""Offline diagnostic cleanup, capability rejection, and retained guard state."""
from pathlib import Path
import os
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'scripts/ppt_reader'
CSC = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework/v4.0.30319/csc.exe'

def run(command):
    result = subprocess.run(list(map(str, command)), capture_output=True, timeout=45)
    output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
    assert result.returncode == 0, output
    return output

@pytest.mark.skipif(os.name != 'nt', reason='Windows Framework required')
@pytest.mark.parametrize('arch', ['x86', 'x64'])
def test_deferred_cleanup_failures(tmp_path, arch):
    exe = tmp_path / 'Cleanup.exe'
    run([CSC, '/nologo', '/define:AEGIS_PPT_DEFERRED_DIAGNOSTIC', '/platform:' + arch,
         '/out:' + str(exe), SOURCE / 'PptDeferredGate.cs', ROOT / 'knowmate/tests/ppt_deferred_cleanup_harness.cs'])
    output = run([exe]); assert 'GATE_RESULT cases=9' in output

@pytest.mark.skipif(os.name != 'nt', reason='Windows Framework required')
@pytest.mark.parametrize('arch', ['x86', 'x64'])
def test_deferred_existing_guard_and_unowned_sentinels(tmp_path, arch):
    state = (SOURCE / 'PptShutdownState.cs').read_text(encoding='utf-8')
    boundary = '\nnamespace AegisDesk.PptReader\n{\n    // Diagnostic-only, fail-closed writer.'
    assert state.count(boundary) == 1
    local = tmp_path / 'State.cs'; local.write_text(state.split(boundary)[0], encoding='utf-8')
    exe = tmp_path / 'Connect.exe'
    names = ['PptReader.cs','PptCallerQuit.cs','PptShutdownGuard.cs','PptShutdownEvents.cs','PptOwnedMetadata.cs',
             'PptEventValidator.cs','PptEventSignatures.cs','PptDeferredGate.cs','PptDeferredDiagnostic.cs']
    run([CSC, '/nologo', '/define:AEGIS_PPT_DEFERRED_DIAGNOSTIC', '/main:DeferredConnectHarness',
         '/reference:System.Windows.Forms.dll', '/reference:Microsoft.CSharp.dll', '/reference:System.Core.dll',
         '/platform:' + arch, '/out:' + str(exe), local, *[SOURCE / n for n in names],
         ROOT / 'knowmate/tests/ppt_lifecycle_harness.cs', ROOT / 'knowmate/tests/ppt_managed_count_fixture.cs',
         ROOT / 'knowmate/tests/ppt_deferred_connect_harness.cs'])
    output = run([exe]); assert 'CONNECT_DEFERRED_RESULT PASS' in output
    for mode in ['prepare', 'pending_protocol', 'pending_version', 'pending_read', 'new_disconnect',
                 'normal', 'attempted', 'broken', 'reading', 'disconnect']:
        assert 'PASS adopted_existing_' + mode in run([exe, mode])

def test_default_build_keeps_diagnostic_conditional():
    for name in ['PptDeferredGate.cs', 'PptDeferredDiagnostic.cs']:
        assert (SOURCE / name).read_text().startswith('#if AEGIS_PPT_DEFERRED_DIAGNOSTIC')
    source = (SOURCE / 'PptDeferredDiagnostic.cs').read_text()
    assert 'Environment.GetEnvironmentVariable("AEGIS_PPT_DEFERRED_DIAGNOSTIC")!="1"' in source
