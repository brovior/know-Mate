"""Core-first observation for new captures; historical checkpoint collection is unchanged."""
from __future__ import annotations

from typing import Any, Callable

from scripts.ppt_reader.ppt_identity_observation import Backend, _error

CORE = ("candidate", "rot_app", "candidate.raw_oleobj_", "candidate._Application", "rot_app._Application")
OPTIONAL = ("Application", "COMAddIns", "COMAddIns.Item", "COMAddIns.Item.Application")


def error_fields(error: Exception) -> dict[str, Any]:
    """Copy numeric COM diagnostics without retaining exception strings or tracebacks."""
    info = getattr(error, "excepinfo", None)
    args = getattr(error, "args", ())
    fields = _error(error)
    hr = fields["hresult"]
    fields["hresult_hex"] = None if hr is None else f"0x{hr & 0xffffffff:08X}"
    fields["wcode"] = info[0] if isinstance(info, tuple) and info and type(info[0]) is int else None
    fields["arg_error"] = args[3] if isinstance(args, tuple) and len(args) > 3 and type(args[3]) is int else None
    fields["exception_fields_present"] = [bool(isinstance(info, tuple) and len(info) > i and info[i] is not None) for i in range(6)]
    return fields


def collect_core(app: Any, prepared_args: tuple[Any, ...], backend: Backend,
                 emit: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Own canonical evidence before metadata; omit optional getters before attempting them."""
    rows = {name: {"name": name, "status": "not_run"} for name in CORE}
    rows.update({name: {"name": name, "status": "omitted_by_plan", "reason": "optional_controller_getter"} for name in OPTIONAL})
    report: dict[str, Any] = dict(schema=3, rows=list(rows.values()), steps=[], usable=False,
        stopped=False, primary=None, cleanup_errors=[], ownership={"unit": "owned_python_wrapper_hold", "acquired": 0, "drop_attempts": 0, "dropped": 0},
        candidate_same_as_rot=False, core_complete=False, optional_complete=False)
    held: list[Any] = []
    groups: list[Any] = []
    objects: dict[str, Any] = {}

    def step(row: str, operation: str, action: Callable[[], Any]) -> tuple[bool, Any]:
        begin = dict(row=row, operation=operation, outcome="begin")
        report["steps"].append(begin)
        try:
            if emit is not None:
                emit(dict(event="com_begin", **begin))
            result = action()
            outcome = dict(row=row, operation=operation, outcome="ok")
            if emit is not None:
                emit(dict(event="com_return", **outcome))
        except Exception as error:
            failure = dict(row=row, operation=operation, **error_fields(error))
            report["steps"].append(failure)
            rows[row]["status"] = failure["outcome"]
            rows[row]["reason"] = operation
            # A missing mandatory canonical/self-control interface cannot admit
            # a request even though old optional getter collection could continue.
            report.update(stopped=True, primary=failure)
            if emit is not None:
                emit(dict(event="com_fail", **failure))
            return False, None
        report["steps"].append(outcome)
        return True, result

    def hold(value: Any) -> Any:
        held.append(value)
        report["ownership"]["acquired"] += 1
        return value

    def canonical(name: str, value: Any) -> None:
        ok, unknown = step(name, "canonical_IUnknown", lambda: backend.canonical(value))
        if not ok:
            return
        hold(unknown)
        ok, group = step(name, "canonical_compare", lambda: next((i for i, other in enumerate(groups) if unknown == other), len(groups)))
        if not ok:
            return
        if group == len(groups):
            groups.append(unknown)
        rows[name]["group"] = group
        rows[name]["status"] = "observed"
        objects[name] = value

    try:
        if len(prepared_args) != 2 or type(prepared_args[0]) is not str:
            report.update(stopped=True, primary=dict(row="candidate", operation="arguments", outcome="invalid"))
            return report
        ok, unwrapped = step("candidate", "unwrap_local_variant", lambda: backend.unwrap_candidate(prepared_args[1]))
        if not ok:
            return report
        candidate, variant = unwrapped
        rows["candidate"].update(variant)
        canonical("candidate", candidate)
        if report["stopped"]:
            return report
        canonical("rot_app", app)
        for name in CORE[2:]:
            if report["stopped"]:
                break
            source = app if name.startswith("rot_app.") else candidate
            raw = name.endswith("raw_oleobj_")
            ok, value = step(name, "unwrap_local_oleobj" if raw else "QI_Application", lambda: backend.raw(source) if raw else backend.application(source))
            if not ok:
                break
            if not raw:
                hold(value)
            canonical(name, value)
            if report["stopped"]:
                break
            parent = "rot_app" if name.startswith("rot_app.") else "candidate"
            rows[name]["self_control"] = rows[name]["group"] == rows[parent]["group"]
            if not rows[name]["self_control"]:
                report.update(stopped=True, primary=dict(row=name, operation="self_control", outcome="invalid"))
        if report["stopped"]:
            return report
        report["core_complete"] = True
        report["candidate_same_as_rot"] = rows["candidate"]["group"] == rows["rot_app"]["group"]
        for name in CORE:
            ok, metadata = step(name, "GetTypeInfo", lambda: backend.typeinfo(objects[name]))
            if not ok:
                break
            rows[name]["typeinfo"] = {key: metadata[key] for key in ("guid", "typekind", "major", "minor") if key in metadata and type(metadata[key]) in (str, int)}
    finally:
        objects.clear()
        groups.clear()
        while held:
            report["ownership"]["drop_attempts"] += 1
            held.pop()
            report["ownership"]["dropped"] += 1
        for row in rows.values():
            if row["status"] == "not_run":
                row["reason"] = "mandatory_predecessor_failed"
        report["usable"] = report["core_complete"] and not report["stopped"] and not report["cleanup_errors"]
        report["outcome"] = "complete" if report["usable"] else "unknown"
    return report
