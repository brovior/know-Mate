"""Offline lifecycle diagnostics: real Framework, fake host/RPC, injected filesystem boundaries."""
from pathlib import Path
import os
import re
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
CSC = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework/v4.0.30319/csc.exe'


def run(command):
    result = subprocess.run(list(map(str, command)), capture_output=True, text=True, errors="replace", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.skipif(sys.platform != 'win32', reason='Framework required')
@pytest.mark.parametrize('arch', ['x86', 'x64'])
def test_native_lifecycle_tokens_and_rpc_invariants(tmp_path, arch):
    source = (ROOT / 'scripts/ppt_reader/PptShutdownState.cs').read_text()
    boundary = '\nnamespace AegisDesk.PptReader\n{\n    // Diagnostic-only, fail-closed writer.'
    assert source.count(boundary) == 1
    local = tmp_path / 'State.cs'
    local.write_text(source.split(boundary)[0])
    exe = tmp_path / 'Lifecycle.exe'
    run([CSC, '/nologo', '/reference:Microsoft.CSharp.dll', '/reference:System.Core.dll',
         '/platform:' + arch, '/out:' + str(exe), local,
         ROOT / 'scripts/ppt_reader/PptReader.cs', ROOT / 'scripts/ppt_reader/PptShutdownGuard.cs',
         *[ROOT / 'scripts/ppt_reader' / n for n in ('PptShutdownEvents.cs', 'PptOwnedMetadata.cs', 'PptEventValidator.cs', 'PptEventSignatures.cs')],
         ROOT / 'knowmate/tests/ppt_lifecycle_harness.cs',
         ROOT / 'knowmate/tests/ppt_managed_count_fixture.cs'])
    output = run([exe])
    assert 'PASS checks=' in output
    print(output)


@pytest.mark.skipif(sys.platform != 'win32', reason='Framework required')
@pytest.mark.parametrize('arch', ['x86', 'x64'])
def test_writer_failures_close_every_acquired_handle(tmp_path, arch):
    source = (ROOT / 'scripts/ppt_reader/PptShutdownState.cs').read_text()
    source, count = re.subn(r'\[System\.Runtime\.InteropServices\.DllImport\([^\]]*\)\]\s*private static extern[^;]+;', '', source)
    assert count == 5
    source = source.replace('internal static class PptLifecycleFile', 'internal static partial class PptLifecycleFile')
    assert source.count('stream.Write(bytes,0,bytes.Length);') == 1
    source = source.replace('stream.Write(bytes,0,bytes.Length);', 'if(Mode=="write") throw new System.IO.IOException("injected"); stream.Write(bytes,0,bytes.Length);')
    local = tmp_path / 'State.cs'; local.write_text(source)
    exe = tmp_path / 'Writer.exe'
    run([CSC, '/nologo', '/platform:' + arch, '/out:' + str(exe), local,
         ROOT / 'knowmate/tests/ppt_lifecycle_writer_harness.cs'])
    output = run([exe, tmp_path]); assert 'all_handles_closed' in output; print(output)


def test_python_diagnostics_disabled_and_slot_cleanup(monkeypatch):
    from knowmate.secure import com_reader as reader
    import threading
    class Opaque:
        def __repr__(self): raise AssertionError('COM repr forbidden')
        def __getattr__(self, name): raise AssertionError('COM properties forbidden')
    names = ('ppt', 'ppt_proof', 'ppt_service', 'ppt_launch_ticket', 'ppt_launch_ready')
    calls = []
    monkeypatch.setattr(reader.logger, 'info', lambda *args: calls.append(args))
    monkeypatch.setattr(reader, '_PPT_LIFECYCLE_TRACE_ENABLED', False)
    for name in names: monkeypatch.setattr(reader._tls, name, Opaque(), raising=False)
    reader._release_ppt_references()
    assert not calls and all(getattr(reader._tls, n) is None for n in names)
    monkeypatch.setattr(reader, '_PPT_LIFECYCLE_TRACE_ENABLED', True)
    for name in names: setattr(reader._tls, name, Opaque())
    reader._release_ppt_references()
    assert [args[-1] for args in calls] == [31, 0]
    assert all(args[-2] == threading.get_ident() for args in calls)
    monkeypatch.setattr(reader.logger, 'info', lambda *args: (_ for _ in ()).throw(RuntimeError('logging')))
    for name in names: setattr(reader._tls, name, Opaque())
    reader._release_ppt_references()
    assert all(getattr(reader._tls, n) is None for n in names)

@pytest.mark.skipif(sys.platform != 'win32', reason='Framework required')
@pytest.mark.parametrize('arch', ['x86', 'x64'])
def test_actual_ntfs_leaf_ancestor_rename_replacement_and_denial(tmp_path, arch):
    """Hold actual production ancestor handles; race only owned empty fixture paths."""
    source = (ROOT / 'scripts/ppt_reader/PptShutdownState.cs').read_text()
    declaration = r'\[System\.Runtime\.InteropServices\.DllImport\("kernel32.dll", CharSet=System.Runtime.InteropServices.CharSet.Unicode, SetLastError=true\)\]\s*private static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFile\([^;]+;'
    source, count = re.subn(declaration, '', source)
    assert count == 1
    source = source.replace('CreateFile(path,', 'NativeRaceHook.Create(path,').replace('CreateFile(file,', 'NativeRaceHook.Create(file,')
    anchor = '                string file=System.IO.Path.Combine(directory,name);'
    assert source.count(anchor) == 1
    source = source.replace(anchor, '                NativeRaceHook.AfterValidate(directory);\n' + anchor)
    local = tmp_path / 'State.cs'; local.write_text(source)
    exe = tmp_path / 'NativeRace.exe'
    run([CSC, '/nologo', '/platform:' + arch, '/out:' + str(exe), local,
         ROOT / 'knowmate/tests/ppt_lifecycle_native_race_harness.cs'])
    output = run([exe, tmp_path / 'owned'])
    assert 'leaf_middle_rename_denied32' in output
    assert 'injected_access_denied_no_fallback' in output
    print(output)

@pytest.mark.skipif(sys.platform != 'win32', reason='Framework required')
@pytest.mark.parametrize('arch', ['x86', 'x64'])
def test_actual_ntfs_normal_existing_missing_sharing_and_junction(tmp_path, arch):
    source = (ROOT / 'scripts/ppt_reader/PptShutdownState.cs').read_text()
    local = tmp_path / 'State.cs'
    local.write_text(source)
    exe = tmp_path / 'NativeSmoke.exe'
    run([CSC, '/nologo', '/platform:' + arch, '/out:' + str(exe), local,
         ROOT / 'knowmate/tests/ppt_lifecycle_native_writer_smoke.cs'])
    target = tmp_path / 'target'; (target / 'AegisDesk/ppt-lifecycle').mkdir(parents=True)
    link = tmp_path / 'link'
    # Both literal paths belong to this test's newly allocated directory; no ACL change.
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    run(['powershell.exe', '-NoProfile', '-Command',
         'New-Item -ItemType Junction -Path ' + quote(link) + ' -Target ' + quote(target) + ' | Out-Null'])
    output = run([exe, tmp_path / 'owned', link, target])
    assert 'PASS junction_rejected' in output and 'FAIL' not in output
    print(output)


@pytest.mark.skipif(sys.platform != 'win32', reason='Framework required')
@pytest.mark.parametrize('arch', ['x86', 'x64'])
def test_explicit_directory_actual_source_and_launcher(tmp_path, arch, monkeypatch):
    """Compile unchanged product code; never start Office or replace its output resolver."""
    import ctypes
    import hashlib
    from ctypes import wintypes
    from knowmate.secure.ppt_launch import LaunchedPowerPoint
    exe = tmp_path / 'Probe.exe'
    source = ROOT / 'scripts/ppt_reader/PptShutdownState.cs'
    run([CSC, '/nologo', '/platform:' + arch, '/out:' + str(exe), source,
         ROOT / 'knowmate/tests/ppt_lifecycle_explicit_directory_probe.cs'])
    profile = Path(os.environ['LOCALAPPDATA']) / 'AegisDesk/ppt-lifecycle'
    def snapshot():
        if not profile.exists():
            return None
        return profile.stat().st_mtime_ns, sorted((str(p.relative_to(profile)), p.stat().st_size,
                       p.stat().st_mtime_ns,
                       hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                      for p in profile.rglob('*'))
    before = snapshot()
    valid = tmp_path / 'valid'; valid.mkdir()
    blocker = tmp_path / 'file'; blocker.write_text('sentinel')
    cases = [None, '', ' ', 'relative', r'C:relative', r'\relative',
             r'\\localhost\C$\invalid', r'\\?\C:\invalid', str(tmp_path / 'missing'),
             str(blocker), str(tmp_path / 'bad:name')]
    base = os.environ.copy(); base['AEGIS_PPT_LIFECYCLE_TRACE'] = '1'
    for value in cases:
        env = base.copy()
        if value is None: env.pop('AEGIS_PPT_LIFECYCLE_DIR', None)
        else: env['AEGIS_PPT_LIFECYCLE_DIR'] = value
        result = subprocess.run([str(exe)], env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0 and 'token=0' in result.stdout
    junction = tmp_path / 'junction'
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    run(['powershell.exe', '-NoProfile', '-Command',
         'New-Item -ItemType Junction -Path ' + quote(junction) + ' -Target ' + quote(valid) + ' | Out-Null'])
    env = base.copy(); env['AEGIS_PPT_LIFECYCLE_DIR'] = str(junction)
    result = subprocess.run([str(exe)], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and 'token=0' in result.stdout and not list(valid.iterdir())
    assert blocker.read_text() == 'sentinel' and not (tmp_path / 'missing').exists()
    env = base.copy(); env['AEGIS_PPT_LIFECYCLE_DIR'] = str(valid)
    env.pop('AEGIS_PPT_LIFECYCLE_TRACE')
    result = subprocess.run([str(exe)], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and 'token=0' in result.stdout and not list(valid.iterdir())
    # Exact real launcher constructor; no COM bind or PowerPoint discovery/activation.
    monkeypatch.setenv('AEGIS_PPT_LIFECYCLE_TRACE', '1')
    monkeypatch.setenv('AEGIS_PPT_LIFECYCLE_DIR', str(valid))
    ticket = LaunchedPowerPoint(exe)
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        assert kernel.WaitForSingleObject(ticket._handle, 20000) == 0
        code = wintypes.DWORD()
        assert kernel.GetExitCodeProcess(ticket._handle, ctypes.byref(code)) and code.value == 0
        output = valid / f'host-{ticket.pid}.log'
        assert output.is_file() and ' 10 8 0 ' in output.read_text()
        assert ticket.identity is not None
        print(f'PASS actual_source inherited captured_directory pid={ticket.pid} identity={ticket.identity} output={output}')
    finally:
        ticket.close()
    assert snapshot() == before
    print('PASS absent_empty_invalid_relative_network_existingfile disabled profile_unchanged')


def test_python_lifecycle_directory_gate(monkeypatch, tmp_path):
    from knowmate.secure import com_reader as reader
    monkeypatch.delenv('AEGIS_PPT_LIFECYCLE_TRACE', raising=False)
    monkeypatch.setenv('AEGIS_PPT_LIFECYCLE_DIR', str(tmp_path))
    calls = []
    with monkeypatch.context() as disabled:
        disabled.setattr(os.path, 'isdir', lambda p: calls.append(p) or True)
        for flag in [None, '', '0', 'true', 'invalid']:
            if flag is None: disabled.delenv('AEGIS_PPT_LIFECYCLE_TRACE', raising=False)
            else: disabled.setenv('AEGIS_PPT_LIFECYCLE_TRACE', flag)
            assert not reader._ppt_lifecycle_trace_enabled()
        assert not calls
    monkeypatch.setenv('AEGIS_PPT_LIFECYCLE_TRACE', '1')
    file = tmp_path / 'file'; file.write_text('sentinel')
    for value in [None, '', ' ', 'relative', r'C:relative', r'\relative',
                  r'\\localhost\C$\invalid', r'\\?\C:\invalid', r'\\.\C:\invalid',
                  'C:\\bad:name', str(file), str(tmp_path / 'missing')]:
        if value is None: monkeypatch.delenv('AEGIS_PPT_LIFECYCLE_DIR', raising=False)
        else: monkeypatch.setenv('AEGIS_PPT_LIFECYCLE_DIR', value)
        assert not reader._ppt_lifecycle_trace_enabled(), value
    monkeypatch.setenv('AEGIS_PPT_LIFECYCLE_DIR', str(tmp_path))
    assert reader._ppt_lifecycle_trace_enabled() is (sys.platform == 'win32')


def test_python_lifecycle_disabled_never_reads_tls_or_logger(monkeypatch):
    from knowmate.secure import com_reader as reader
    calls = []
    class Forbidden:
        def __getattribute__(self, name):
            calls.append(name)
            raise AssertionError('disabled diagnostic touched ' + name)
    monkeypatch.setattr(reader, '_PPT_LIFECYCLE_TRACE_ENABLED', False)
    monkeypatch.setattr(reader, '_tls', Forbidden())
    monkeypatch.setattr(reader, 'logger', Forbidden())
    reader._trace_ppt_cache_slots('disabled')
    assert not calls


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows drive-absolute gate')
def test_python_lifecycle_import_captures_once(tmp_path):
    code = """
import os
from knowmate.secure import com_reader as r
assert r._PPT_LIFECYCLE_TRACE_ENABLED
os.environ.pop('AEGIS_PPT_LIFECYCLE_TRACE')
os.environ.pop('AEGIS_PPT_LIFECYCLE_DIR')
assert r._PPT_LIFECYCLE_TRACE_ENABLED
filesystem_calls=[]
os.path.isdir=lambda p: filesystem_calls.append(p) or False
calls=[]
r.logger.info=lambda *args: calls.append(args)
r._trace_ppt_cache_slots('captured')
assert len(calls)==1 and not filesystem_calls
"""
    env = os.environ.copy()
    env.update(AEGIS_PPT_LIFECYCLE_TRACE='1', AEGIS_PPT_LIFECYCLE_DIR=str(tmp_path))
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
