"""Exercise the exact pinned live helper without Office or installation."""
import json
import pytest
from knowmate.tests.test_ppt_identity_controller import host_report
from knowmate.tests.test_ppt_identity_observation import Fake, ComFailure
from scripts.ppt_reader.ppt_identity_live import prepare_observed


@pytest.mark.parametrize('case', ['ready', 'refused', 'prepare_error', 'bind_refused',
    'bind_error', 'host_unknown', 'host_cleanup', 'observe_error', 'controller_fatal', 'export_error'])
def test_live_helper_complete_evidence_and_no_followup(tmp_path, case):
    calls = []
    class Service:
        def BindExternalBootstrap(self, *args):
            calls.append('bind')
            if case == 'bind_error': raise RuntimeError('private')
            return 'refused' if case == 'bind_refused' else 'bound'
        def ObserveExternalIdentity(self, *args):
            calls.append('observe')
            if case == 'observe_error': raise RuntimeError('private')
            report = host_report()
            if case == 'host_unknown': report['usable'] = False
            if case == 'host_cleanup': report['cleanupErrors'] = [{'hr': -1}]
            return json.dumps(report)
        def PrepareExternalBootstrap(self, *args):
            calls.append('prepare')
            if case == 'prepare_error': raise RuntimeError('private')
            return 'external_refused' if case == 'refused' else 'ready'
        def __getattr__(self, name): raise AssertionError('unexpected followup: ' + name)
    pins = dict(version='fixture', source_fingerprint='fixture', installed_files={}, helper_sha256='fixture')
    if case == 'export_error': (tmp_path/'identity-observation.json').mkdir()
    result = prepare_observed(tmp_path, 'run', Service(), object(), 'nonce', 42, 123, 1, pins,
        Fake(ComFailure(-2147417848)) if case == 'controller_fatal' else Fake())
    expected = ['bind'] if case.startswith('bind_') or case == 'controller_fatal' else ['bind', 'observe']
    if case in ('ready', 'refused', 'prepare_error'): expected.append('prepare')
    assert calls == expected
    final = json.loads((tmp_path/'identity-controller-result.json').read_text())
    assert final['observation'] == result and final['binding']['version'] == 'fixture'
    assert len(result['controller']['rows']) == 9 and len(result['host']['rows']) == 4
    assert result['prepare_calls'] == int('prepare' in expected)
    if case != 'export_error':
        initial = json.loads((tmp_path/'identity-observation.json').read_text())['observation']
        assert initial['prepare_calls'] == 0 and 'prepare_result' not in initial
    assert 'private' not in str(final)
