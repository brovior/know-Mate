"""Offline-tested observation/export barrier before the existing Prepare call.

Callbacks bind the transport and trace writer. No server activation or fallback
Prepare is performed here. A successful observation grants no Quit permission.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from scripts.ppt_reader.ppt_identity_observation import Backend, collect

HOST_ROWS = {"host", "candidate", "host.Application", "candidate.Application"}
CONTROLLER_ROWS = {"rot_app", "candidate", "candidate.raw_oleobj_", "candidate._Application",
                   "rot_app._Application", "Application", "COMAddIns", "COMAddIns.Item", "COMAddIns.Item.Application"}


def skipped_host(reason: str) -> dict[str, Any]:
    """Keep every host row explicit even when transport must not be called."""
    return {"schema": 2, "usable": False, "outcome": "unknown", "stopped": True,
            "primary": None, "cleanupErrors": [], "acquired": 0, "releaseAttempts": 0, "released": 0,
            "ownership": [], "rows": [{"name": name, "status": "not_run", "reason": reason}
                                       for name in sorted(HOST_ROWS)]}


def complete_host(report: dict[str, Any]) -> bool:
    """Reject missing rows, failed cleanup, missing ownership and invalid identity."""
    rows = report.get("rows", [])
    if len(rows) != 4 or {r.get("name") for r in rows} != HOST_ROWS:
        return False
    if not report.get("usable") or report.get("outcome") != "complete" or report.get("stopped"):
        return False
    if report.get("cleanupFailed") or report.get("cleanupErrors") or report.get("primary") or report.get("dropped") != 0:
        return False
    if report.get("acquired", -1) != report.get("released", -2) or report.get("releaseAttempts") != report.get("acquired"):
        return False
    ownership = report.get("ownership", [])
    if not ownership or len({r.get("id") for r in ownership}) != len(ownership):
        return False
    if any(not r.get("acquired") or not r.get("attempted") or not r.get("released") or r.get("blocked") for r in ownership):
        return False
    mapping = {r["name"]: r for r in rows}
    if any(r.get("selfControl") == 0 for r in rows):
        return False
    if any(mapping[n].get("status") != "observed" or mapping[n].get("selfControl") != 1 for n in ("host", "candidate")):
        return False
    return all(r.get("status") == "observed" or (r.get("status") == "unavailable" and r.get("reason")) for r in rows)


def observe_export_prepare(app: Any, prepared_args: tuple[Any, ...], prog_id: str, backend: Backend,
                           observe: Callable[[Any], dict[str, Any]], export: Callable[[dict[str, Any]], None],
                           prepare: Callable[..., Any]) -> dict[str, Any]:
    """Export primitive evidence, then call existing Prepare only on a complete run."""
    result: dict[str, Any] = {"schema": 1, "controller": None, "host": skipped_host("controller_unusable"),
                              "host_reason": "controller_unusable", "prepare_calls": 0,
                              "prepare_reason": "observation_unusable", "exported": False,
                              "transport_primary": None}
    export_attempted = False
    try:
        controller = collect(app, prepared_args, prog_id, backend)
        result["controller"] = controller
        rows = controller.get("rows", [])
        controller_ok = controller.get("usable") and len(rows) == 9 and {r.get("name") for r in rows} == CONTROLLER_ROWS
        if controller_ok:
            result["host_reason"] = "observe_started"
            # Send the exact submitted candidate, including an explicit VARIANT.
            result["host"] = observe(prepared_args[1])
            result["host_reason"] = "observed"
            if not isinstance(result["host"], dict):
                result["host"] = skipped_host("invalid_transport_type")
        host_ok = controller_ok and complete_host(result["host"] or {})
        mapping = {r['name']: r for r in (result['host'] or {}).get('rows', [])}
        same = host_ok and mapping['host']['group'] == mapping['candidate']['group']
        ready = bool(same and controller["candidate_same_as_rot"])
        result["prepare_reason"] = "eligible_for_existing_gate" if ready else "identity_or_observation_refused"
        # Serialization is also a primitive-only boundary. No COM object may escape.
        if result["host"] is not None and {r.get('name') for r in result['host'].get('rows', [])} != HOST_ROWS:
            result['host']['usable'] = False
            result['host']['outcome'] = 'unknown'
            result['host']['transport_reason'] = 'missing_or_unexpected_rows'
            present = {r.get('name') for r in result['host'].get('rows', [])}
            for name in sorted(HOST_ROWS - present):
                result['host'].setdefault('rows', []).append({'name': name, 'status': 'not_run', 'reason': 'missing_from_transport'})
        snapshot = json.loads(json.dumps(result, allow_nan=False))
        export_attempted = True
        export(snapshot)
        result["exported"] = True
        if ready:
            result["prepare_calls"] = 1
            result["prepare_result"] = prepare(*prepared_args)
        return result
    except Exception as error:
        result["transport_primary"] = {"type": type(error).__name__, "hresult": getattr(error, "hresult", None)}
        result["prepare_reason"] = "transport_or_export_failed"
        if result['host_reason'] == 'observe_started':
            result['host_reason'] = 'transport_failed'
            result['host'] = skipped_host('transport_failed')
        if not export_attempted:
            try:
                try:
                    snapshot = json.loads(json.dumps(result, allow_nan=False))
                except (TypeError, ValueError):
                    result['host'] = skipped_host('nonprimitive_transport')
                    snapshot = json.loads(json.dumps(result, allow_nan=False))
                export(snapshot)
                result['exported'] = True
            except Exception as export_error:
                result['export_error'] = {'type': type(export_error).__name__}
        return result
