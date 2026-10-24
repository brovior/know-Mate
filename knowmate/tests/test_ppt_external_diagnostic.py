"""Compile final product diagnostic sources against synthetic, Office-free fixtures."""
from pathlib import Path
import os
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'scripts/ppt_reader'
FIXTURE = ROOT / 'knowmate/tests/ppt_external_fixture'
CSC = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework/v4.0.30319/csc.exe'
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows Framework required')
CASES = ['ordinary_normal','ordinary_reading','ordinary_external_disabled','ordinary_reconnect','ordinary_secondary_sta','diagnostic_secondary_sta','diagnostic_missing_binding','invalid_capability','diagnostic_normal','diagnostic_deferred','diagnostic_legacy_normal','reading_at_admission','reading_during_identity','reconnect_during_claim','disconnect_during_prepare','reading_before_commit','reconnect_ready','external_normal','external_cancelled','unknown_late_completion','unknown_new_domain','unknown_new_connect','unknown_reconnect','unknown_optout_bypass','bounded_records']
CLEANUP = ['nested_priority','independent_cleanup','normal_fatal','normal_unknown','cleanup_fatal','cleanup_only','reader_begin_failure','prepare_fatal','disconnect_bound','reconnect_bound','disconnect_ready','begin_shutdown_reentrant']
CASES += ['status_different', 'status_unknown', 'status_ready', 'status_body_refused',
          'status_disconnected', 'status_broken', 'status_entered', 'status_attempted',
          'status_reading', 'status_combined', 'status_priority']
CASES += ['decision_presignaled', 'decision_generation', 'decision_release_failed',
          'decision_second_failed']

def run(command, log, env=None, timeout=60):
    """Keep fixture output and fail on an unsuccessful owned child."""
    result = subprocess.run(list(map(str, command)), capture_output=True, env=env, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    log.write_bytes(result.stdout + result.stderr)
    assert result.returncode == 0, log.read_text(errors='replace')

def build(folder, arch, main, diagnostic=True, faults=False, actual_counts=False):
    """Compile product source directly; only host/count/extraction are synthetic."""
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / 'Fixture.exe'
    names = ['PptReader.cs','PptCallerQuit.cs','PptShutdownGuard.cs','PptShutdownEvents.cs','PptOwnedMetadata.cs',
             'PptShutdownState.cs','PptEventSignatures.cs','PptEventValidator.cs','PptDeferredDiagnostic.cs']
    sources = [SOURCE / n for n in names] + list(SOURCE.glob('PptExternal*.cs'))
    fixtures = ['Fixtures.cs','CountFixture.cs']
    if actual_counts:
        fixtures.remove('CountFixture.cs')
        sources.append(SOURCE / 'PptRawCount.cs')
        native = folder / 'NativeObject.cs'
        native.write_text((ROOT / 'knowmate/tests/ppt_raw_count_harness.cs').read_text(encoding='utf-8').split('class Point:')[0], encoding='utf-8')
        sources.append(native)
    if diagnostic:
        sources.append(SOURCE / 'PptDeferredGate.cs')
        fixtures += ['FixtureServer.cs','RawTraceTests.cs','BootstrapCleanupFaults.cs'] if main == 'RawTraceTests' else ['FixtureServer.cs'] if main == 'Program' else ['Tests.cs','CleanupTests.cs','TraceTests.cs','Capabilities.cs','BootstrapCleanupFaults.cs']
    else:
        fixtures += ['DefaultProbe.cs']
    args = ['/define:AEGIS_PPT_DEFERRED_DIAGNOSTIC;AEGIS_PPT_EXTERNAL_DIAGNOSTIC' + (';AEGIS_BOOTSTRAP_CLEANUP_TEST' if faults else '')] if diagnostic else []
    run([CSC,'/nologo','/main:'+main,'/platform:'+arch,'/out:'+str(exe),
         '/reference:Microsoft.CSharp.dll','/reference:System.Core.dll','/reference:System.Web.Extensions.dll',
         '/reference:System.Windows.Forms.dll',*args,*sources,*[FIXTURE/n for n in fixtures]],folder/'compile.log')
    gac = Path(os.environ['WINDIR']) / 'assembly/GAC_MSIL'
    events = folder / 'AegisDesk.PptReader.Events.dll'
    run([CSC,'/nologo','/target:library','/platform:'+arch,'/out:'+str(events),'/reference:'+str(exe),
         '/reference:'+str(gac/'Microsoft.Office.Interop.PowerPoint/15.0.0.0__71e9bce111e9429c/Microsoft.Office.Interop.PowerPoint.dll'),
         '/reference:'+str(gac/'office/15.0.0.0__71e9bce111e9429c/office.dll'),ROOT/'scripts/ppt_events/PptEventSink.cs'],folder/'events-compile.log')
    return exe, events

@pytest.mark.parametrize('arch',['x86','x64'])
def test_external_final_source_transitions_and_cleanup(tmp_path, arch):
    """Exercise final Connect, guard, fences and cleanup with test-only injections."""
    for main, cases in [('Tests',CASES),('CleanupTests',CLEANUP)]:
        exe, events = build(tmp_path/main,arch,main,faults=True)
        for case in cases:
            env = {k:v for k,v in os.environ.items() if not k.startswith(('AEGIS_PPT_','AEGIS_TEST_'))}
            if not case.startswith('ordinary_'): env['AEGIS_PPT_EXTERNAL_DIAGNOSTIC']='1'
            if case=='diagnostic_legacy_normal':
                env.pop('AEGIS_PPT_EXTERNAL_DIAGNOSTIC');env['AEGIS_PPT_DEFERRED_DIAGNOSTIC']='1'
            run([exe,case,events],exe.parent/(case+'.log'),env)

@pytest.mark.parametrize('arch',['x86','x64'])
def test_external_final_source_native_com(tmp_path, arch):
    """Use real inherited handles and COM roundtrips without fault seams or Office."""
    build(tmp_path/arch,arch,'Program',faults=False)
    run([sys.executable,FIXTURE/'run_native.py',tmp_path,arch],tmp_path/'native.log',timeout=180)

@pytest.mark.parametrize('arch',['x86','x64'])
def test_external_default_off(tmp_path, arch):
    """Default build excludes bootstrap and retains the ordinary dual interfaces."""
    exe, events = build(tmp_path,arch,'DefaultProbe',diagnostic=False)
    env = {k:v for k,v in os.environ.items() if not k.startswith('AEGIS_PPT_')}
    run([exe,events],tmp_path/'default.log',env)

@pytest.mark.parametrize('arch',['x86','x64'])
def test_external_terminal_trace_invariance(tmp_path, arch):
    """Terminal snapshots cannot change guard outcomes or consume the Ready export."""
    exe, events = build(tmp_path,arch,'TraceTests',faults=True)
    for case in ['earlyrefusal','normal','unknown','writerfail','primarycleanup','overflow','nativewriter','processloss']:
        env = {k:v for k,v in os.environ.items() if not k.startswith(('AEGIS_PPT_','AEGIS_TEST_'))}
        env.update(AEGIS_PPT_EXTERNAL_DIAGNOSTIC='1', AEGIS_PPT_LIFECYCLE_TRACE='1',
                   AEGIS_PPT_TRACE_RUN='1234567890abcdef1234567890abcdef')
        directory = tmp_path / case
        directory.mkdir()
        env['AEGIS_PPT_LIFECYCLE_DIR'] = str(directory)
        run([exe,case,events],tmp_path/(case+'.log'),env)
        if case == 'processloss':
            assert not list(directory.glob('*.log')), 'Abrupt owned exit must be reported as unrecovered.'

@pytest.mark.parametrize('arch',['x86','x64'])
def test_external_native_count_error_trace(tmp_path, arch):
    """Real raw IDispatch failure and cleanup preserve distinct HRESULT records."""
    exe, _ = build(tmp_path, arch, 'RawTraceTests', faults=True, actual_counts=True)
    env = {k:v for k,v in os.environ.items() if not k.startswith(('AEGIS_PPT_','AEGIS_TEST_'))}
    env.update(AEGIS_PPT_EXTERNAL_DIAGNOSTIC='1', AEGIS_PPT_LIFECYCLE_TRACE='1',
               AEGIS_PPT_TRACE_RUN='1234567890abcdef1234567890abcdef')
    run([exe],tmp_path/'raw-trace.log',env)
