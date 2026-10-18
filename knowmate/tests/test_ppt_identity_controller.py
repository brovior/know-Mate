"""Fail-closed export/Prepare barrier, with no Office activation."""
import copy
import pytest
from knowmate.tests.test_ppt_identity_observation import Fake
from scripts.ppt_reader.ppt_identity_controller import observe_export_prepare
from scripts.ppt_reader.ppt_identity_observation import collect


def host_report():
    return dict(schema=2, usable=True, outcome='complete', stopped=False, cleanupFailed=False,
        cleanupErrors=[], primary=None, dropped=0, acquired=1, releaseAttempts=1, released=1,
        ownership=[dict(id=1, acquired=True, attempted=True, released=True, blocked=False)],
        rows=[dict(name=n, status='observed', group=0, selfControl=1) for n in
              ('host', 'candidate', 'host.Application', 'candidate.Application')])


@pytest.mark.parametrize('fault', ['truncated', 'overflow', 'cleanup', 'release', 'selfcontrol',
                                   'stopped', 'ownership', 'blocked', 'primary'])
def test_invalid_observation_is_exported_but_never_prepared(fault):
    host = host_report()
    if fault == 'truncated': host['rows'].pop()
    elif fault == 'overflow': host['dropped'] = 1
    elif fault == 'cleanup': host['cleanupErrors'] = [dict(operation='VariantClear', hr=-1)]
    elif fault == 'release': host['released'] = 0
    elif fault == 'selfcontrol': host['rows'][2]['selfControl'] = 0
    elif fault == 'stopped': host['stopped'] = True
    elif fault == 'ownership': host['ownership'] = []
    elif fault == 'blocked': host['ownership'][0]['blocked'] = True
    elif fault == 'primary': host['primary'] = dict(outcome='fatal', hr=-2147417848)
    app = object()
    exports, calls = [], []
    result = observe_export_prepare(app, ('nonce', app), 'fixture', Fake(), lambda _: host,
                                    lambda value: exports.append(copy.deepcopy(value)), lambda *args: calls.append(args))
    assert not calls and result['prepare_calls'] == 0 and result['exported']
    assert len(exports[0]['controller']['rows']) == 9
    assert len(exports[0]['host']['rows']) == 4


def test_export_failure_never_prepares_and_is_not_retried():
    app = object()
    writes, prepares = [], []
    def fail(value):
        writes.append(1)
        raise OSError('private text')
    result = observe_export_prepare(app, ('nonce', app), 'fixture', Fake(), lambda _: host_report(),
                                    fail, lambda *args: prepares.append(args))
    assert writes == [1] and prepares == []
    assert not result['exported'] and 'private text' not in str(result)


def test_success_calls_existing_prepare_without_replacing_its_result():
    app = object()
    calls = []
    def prepare(*args):
        calls.append(args)
        return 'bootstrap_missing'
    result = observe_export_prepare(app, ('nonce', app), 'fixture', Fake(), lambda _: host_report(),
                                    lambda _: None, prepare)
    assert calls == [('nonce', app)] and result['prepare_result'] == 'bootstrap_missing'


def test_transport_failure_exports_explicit_skipped_host_rows():
    app = object()
    exports, prepares = [], []
    def failed(_):
        raise RuntimeError('private transport detail')
    result = observe_export_prepare(app, ('nonce', app), 'fixture', Fake(), failed,
                                    exports.append, lambda *args: prepares.append(args))
    assert not prepares and result['exported'] and len(exports) == 1
    assert all(row['reason'] == 'transport_failed' for row in exports[0]['host']['rows'])
    assert 'private transport detail' not in str(exports)


def test_invalid_controller_self_control_stops_before_any_getter():
    class BrokenRaw(Fake):
        def raw(self, value): return object()
    app = object()
    backend = BrokenRaw()
    result = collect(app, ('nonce', app), 'fixture', backend)
    assert not result['usable'] and result['stop_reason'] == 'invalid_self_control'
    assert backend.calls == []
