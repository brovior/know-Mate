"""One-shot capture adapters; observations never replace the existing host guard."""
from __future__ import annotations

from typing import Any, Callable

from scripts.ppt_reader.ppt_identity_controller import complete_host, skipped_host
from scripts.ppt_reader.ppt_identity_observation import Backend

from .bundle import primitive
from .identity import collect_core, error_fields


def observe_for_capture(app: Any, candidate: Any, nonce: str, backend: Backend,
                        observe: Callable[[Any], dict[str, Any]],
                        export: Callable[[dict[str, Any]], None],
                        emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Persist exact-candidate evidence before admitting a single existing request gate."""
    result: dict[str, Any] = dict(schema=3, controller=None, host=skipped_host("controller_unusable"),
                                 eligible=False, exported=False, primary=None)
    try:
        result["controller"] = collect_core(app, (nonce, candidate), backend, emit)
        if result["controller"]["usable"]:
            emit(dict(event="host_observation_begin"))
            result["host"] = primitive(observe(candidate))
            emit(dict(event="host_observation_return"))
        host = result["host"]
        host_ok = complete_host(host)
        rows = {row["name"]: row for row in host["rows"]}
        result["eligible"] = bool(result["controller"]["usable"] and result["controller"]["candidate_same_as_rot"]
                                  and host_ok and rows["host"]["group"] == rows["candidate"]["group"])
        export(primitive(result))
        result["exported"] = True
    except Exception as error:
        result.update(eligible=False, primary=error_fields(error))
    return result


class OneShotRequest:
    """Consume admission before any COM request and never retry an ambiguous result."""

    def __init__(self, path: str, emit: Callable[[dict[str, Any]], None]) -> None:
        """Bind one requested path and a primitive event sink."""
        if path not in {"internal", "external"}:
            raise ValueError("capture_path")
        self.path = path
        self.emit = emit
        self.consumed = False

    def run(self, observation: dict[str, Any], internal: Callable[[], Any],
            prepare: Callable[[], str], status: Callable[[], str],
            start: Callable[[], None], ack: Callable[[int], bool],
            quit_call: Callable[[], Any], complete: Callable[[], None],
            match: Callable[[], None]) -> str:
        """Use only the selected path, preserving normal guard and external handshake."""
        if self.consumed:
            raise RuntimeError("request_consumed")
        self.consumed = True
        if not observation.get("eligible") or not observation.get("exported") or observation.get("primary"):
            self.emit(dict(event="request_not_run", reason="observation_or_export_refused"))
            return "blocked"
        try:
            match()
            if self.path == "internal":
                self.emit(dict(event="request_begin", path="internal"))
                value = internal()
                if value not in {"quit_requested", "protected", "not_supported", "failed"}:
                    raise RuntimeError("internal_result_unknown")
                self.emit(dict(event="request_return", path="internal", status=value))
                return value
            self.emit(dict(event="prepare_begin"))
            ready = prepare()
            self.emit(dict(event="prepare_return", ready=ready == "ready"))
            if ready != "ready":
                self.emit(dict(event="request_not_run", reason="prepare_refused"))
                return "blocked"
            tokens = status().split()
            if not all(token in tokens for token in ("identity_result=Same", "identity=2/2", "phase=Ready")):
                self.emit(dict(event="request_not_run", reason="ready_contract_unknown"))
                return "unknown"
            if ack(0):
                self.emit(dict(event="request_not_run", reason="ack_presignaled"))
                return "unknown"
            match()
            start()
            self.emit(dict(event="start_signaled_once"))
            if not ack(3000):
                self.emit(dict(event="request_not_run", reason="start_ack_missing"))
                return "unknown"
            self.emit(dict(event="start_ack_observed"))
            self.emit(dict(event="request_begin", path="external"))
            if quit_call() is not None:
                raise RuntimeError("quit_return_not_empty")
            self.emit(dict(event="request_return", path="external", status="normal_return"))
            complete()
            self.emit(dict(event="completion_signaled_after_normal_return"))
            return "quit_requested"
        except Exception as error:
            self.emit(dict(event="request_failed", error=error_fields(error)))
            return "unknown"
