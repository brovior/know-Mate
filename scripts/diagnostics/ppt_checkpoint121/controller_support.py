"""Office-independent controller protocol and local evidence collection."""
import importlib.util
import json
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('trace_report121', SOURCE/'scripts/ppt_reader/ppt_trace_report.py')
report_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_module)


def perform_handshake(root, run_id, service, app, launched, pid, hwnd, match, emit):
    """Issue each protocol operation once; stop on rejection or unknown failure."""
    from scripts.ppt_reader.ppt_identity_live import prepare_observed
    pins=json.loads((root/'package-pins.json').read_text())
    result=prepare_observed(root,run_id,service,app,launched.nonce,pid,launched.identity,hwnd,pins,_backend())
    emit('identity_observation_and_prepare_recorded',prepare_calls=result['prepare_calls'],
         prepare_result=result.get('prepare_result'),observation_exported=result['exported'])
    # No host/service RPC after any observation, export or Prepare failure/refusal.
    if result.get('transport_primary') or not result['exported'] or result.get('prepare_result')!='ready':
        return 'identity_or_prepare_refused_no_followup'
    ready=result['prepare_result']
    status = service.ExternalBootstrapStatus()
    emit('bootstrap_prepare_result', result=ready, status=status)
    if ready != 'ready':
        return 'refused'
    if not all(token in status.split() for token in ('identity_result=Same', 'identity=2/2', 'phase=Ready')):
        return 'ready_contract_mismatch'
    if launched.ack():
        raise RuntimeError('unexpected_preexisting_ack')
    match()
    launched.signal(0)
    emit('start_signaled_once', pid=pid)
    if not launched.ack(3000):
        return 'start_ack_missing_no_quit'
    emit('start_ack_observed', pid=pid, same_canonical_identity=True, sink_reserved=True)
    dispid = app._oleobj_.GetIDsOfNames('Quit')
    if dispid != 2021:
        return 'quit_dispid_changed'
    emit('external_quit_begin_service_retained', pid=pid, dispid=dispid, flags=3, quit_calls=1)
    result = app._oleobj_.Invoke(dispid, 0, 3, 1)
    if result is not None:
        return 'quit_result_not_empty'
    emit('external_quit_normal_return', pid=pid)
    launched.signal(2)
    emit('completion_signaled_after_normal_return', pid=pid)
    return 'completed'


def collect_trace(root, run_id, pid, creation, timeout=3):
    """Collect local files only; no status polling or any host/COM operation."""
    deadline = time.monotonic() + timeout
    while True:
        report = report_module.read_terminal_trace(root/'native-trace', run_id, pid, creation)
        if report['evidence_complete'] or time.monotonic() >= deadline:
            break
        time.sleep(.02)
    report['collected_qpc_ns'] = time.perf_counter_ns()
    report['controller_file'] = str(root/'control.jsonl')
    report['observer_file'] = str(root/'process-events.jsonl')
    (root/'host-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def perform_and_collect(root, run_id, service, app, launched, pid, hwnd, match, emit):
    """Always recover available evidence, including normal refusal and exceptions."""
    try:
        return perform_handshake(root, run_id, service, app, launched, pid, hwnd, match, emit)
    finally:
        report = collect_trace(root, run_id, pid, launched.identity)
        emit('host_trace_collected', host_trace=report['host_trace'], evidence_complete=report['evidence_complete'])


def _backend():
    import pythoncom
    import win32com.client
    from scripts.ppt_reader.ppt_identity_observation import Pywin32Backend
    return Pywin32Backend(pythoncom, win32com.client.VARIANT)
