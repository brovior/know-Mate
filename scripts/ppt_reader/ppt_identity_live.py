"""Pinned live-controller observation/Prepare path; no activation or retries."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.ppt_reader.ppt_identity_controller import CONTROLLER_ROWS, observe_export_prepare, skipped_host
from scripts.ppt_reader.ppt_identity_observation import Backend


def prepare_observed(root: Path, run_id: str, service: Any, app: Any, nonce: str,
                     pid: int, creation: int, hwnd: int, pins: dict[str, Any],
                     backend: Backend) -> dict[str, Any]:
    """Save all rows and the actual Prepare outcome, stopping after any refusal."""
    envelope = {"run_id": run_id, "pid": pid, "creation": creation,
                "version": pins["version"], "source_fingerprint": pins["source_fingerprint"],
                "installed_files": pins["installed_files"], "helper_sha256": pins["helper_sha256"]}

    def write(name: str, value: dict[str, Any]) -> None:
        with (root / name).open("x", encoding="utf-8") as stream:
            json.dump({"binding": envelope, "observation": value}, stream, indent=2, allow_nan=False)

    result = {"controller": {"usable": False, "rows": [
        {"name": name, "status": "not_run", "reason": "bind_not_completed"}
        for name in sorted(CONTROLLER_ROWS)]}, "host": skipped_host("bind_not_completed"),
        "prepare_calls": 0, "prepare_reason": "bind_not_completed", "exported": False}
    try:
        bound = service.BindExternalBootstrap(pid, str(creation), nonce, hwnd)
    except Exception as error:
        result["bind_error"] = {"type": type(error).__name__, "hresult": getattr(error, "hresult", None)}
    else:
        result["bind_result"] = bound
        if bound == "bound":
            result = observe_export_prepare(app, (nonce, app), "AegisDesk.PptReader", backend,
                lambda candidate: json.loads(service.ObserveExternalIdentity(nonce, candidate)),
                lambda snapshot: write("identity-observation.json", snapshot),
                service.PrepareExternalBootstrap)
            result["bind_result"] = bound
        else:
            result["prepare_reason"] = "bind_rejected"
    if result["prepare_reason"] in ("bind_not_completed", "bind_rejected"):
        write("identity-observation.json", result)
        result["exported"] = True
    # Local evidence only: no status/getter/cleanup RPC follows any failure here.
    write("identity-controller-result.json", result)
    return result
