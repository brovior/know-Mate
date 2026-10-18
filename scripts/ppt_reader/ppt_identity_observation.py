"""Read-only controller evidence; no Office activation or bootstrap invocation.

The caller supplies the exact prepared argument tuple and a backend for its COM
library. Results contain primitives only. This module is not wired into a live run.
"""
from __future__ import annotations

from typing import Any, Protocol


class Backend(Protocol):
    """Operations run synchronously on the caller's existing COM apartment."""

    def canonical(self, value: Any) -> Any:
        """Return a separately owned canonical IUnknown wrapper."""

    def typeinfo(self, value: Any) -> dict[str, Any]:
        """Return only guid, typekind, major and minor primitives."""

    def get(self, value: Any, name: str) -> Any:
        """Read exactly one named property, without default-member fallback."""

    def item(self, value: Any, prog_id: str) -> Any:
        """Invoke the explicitly named Item member once."""

    def unwrap_candidate(self, value: Any) -> tuple[Any, dict[str, Any]]:
        """Read only known local VARIANT storage, with no COM property lookup."""

    def raw(self, value: Any) -> Any:
        """Read a wrapper's local _oleobj_ storage."""

    def application(self, value: Any) -> Any:
        """Own one _Application interface obtained by QueryInterface."""


class Pywin32Backend:
    """Use the caller's initialized pythoncom module; never activate a server."""

    def __init__(self, pythoncom: Any, variant_type: type | None = None) -> None:
        self.com = pythoncom
        self.variant_type = variant_type

    def unwrap_candidate(self, value: Any) -> tuple[Any, dict[str, Any]]:
        """Unwrap only the exact caller-supplied pywin32 VARIANT class."""
        if self.variant_type is not None and type(value) is self.variant_type:
            state = object.__getattribute__(value, "__dict__")
            vt = state["varianttype"]
            if type(vt) is not int or vt not in (9, 13):
                raise ValueError("unsupported_candidate_variant")
            return state["_value"], {"explicit_variant": True, "vartype": vt}
        return value, {"explicit_variant": False, "vartype": None}

    @staticmethod
    def raw(value: Any) -> Any:
        """Unwrap Python state without a COM property/default-member lookup."""
        try:
            state = object.__getattribute__(value, "__dict__")
        except AttributeError:
            return value
        return state.get("_oleobj_", value)

    def application(self, value: Any) -> Any:
        """Query the public _Application IID without invoking Application."""
        return self.raw(value).QueryInterface(
            self.com.MakeIID("{91493442-5A91-11CF-8700-00AA0060263B}"),
            self.com.IID_IDispatch,
        )

    def canonical(self, value: Any) -> Any:
        """Own one PyIUnknown wrapper for local canonical comparison."""
        return self.raw(value).QueryInterface(self.com.IID_IUnknown)

    def typeinfo(self, value: Any) -> dict[str, Any]:
        """Return the TYPEATTR identity and version, without retaining ITypeInfo."""
        info = self.raw(value).GetTypeInfo()
        attr = info.GetTypeAttr()
        return {"guid": str(attr[0]), "typekind": attr[5], "major": attr[12], "minor": attr[13]}

    def get(self, value: Any, name: str) -> Any:
        """Resolve and invoke one explicit property; propagate original HRESULTs."""
        raw = self.raw(value)
        dispid = raw.GetIDsOfNames(0, name)
        return raw.Invoke(dispid, 0, self.com.DISPATCH_PROPERTYGET, True)

    def item(self, value: Any, prog_id: str) -> Any:
        """Invoke Item explicitly, without implicit default-member conversion."""
        raw = self.raw(value)
        dispid = raw.GetIDsOfNames(0, "Item")
        return raw.Invoke(dispid, 0, self.com.DISPATCH_METHOD, True, prog_id)


_MISSING = {0x80004002, 0x80020003, 0x80020006}
_FATAL = {0x800706BA, 0x800706BE, 0x800706BF, 0x80010006,
          0x80010007, 0x80010012, 0x80010108, 0x800401FD}


def _error(error: Exception) -> dict[str, Any]:
    hr = getattr(error, "hresult", None)
    info = getattr(error, "excepinfo", None)
    inner = info[5] if isinstance(info, tuple) and len(info) > 5 else None
    hr = hr if type(hr) is int else None
    inner = inner if type(inner) is int else None
    effective = inner if hr is not None and hr & 0xFFFFFFFF == 0x80020009 and inner is not None else hr
    unsigned = None if effective is None else effective & 0xFFFFFFFF
    # A plain AttributeError from a wrapper is not enough to prove a missing
    # remote member; only explicit COM absent-member HRESULTs allow continuation.
    outcome = "unavailable" if unsigned in _MISSING else "fatal" if unsigned in _FATAL else "unknown"
    return {"outcome": outcome, "hresult": hr, "inner_hresult": inner}


def collect(app: Any, prepared_args: tuple[Any, ...], prog_id: str,
            backend: Backend) -> dict[str, Any]:
    """Describe the exact candidate and getter stages without sending the call."""
    names = ("rot_app", "candidate", "candidate.raw_oleobj_", "candidate._Application",
             "rot_app._Application", "Application", "COMAddIns", "COMAddIns.Item",
             "COMAddIns.Item.Application")
    report: dict[str, Any] = {
        "schema": 2, "invoked": False, "stopped": False, "usable": False,
        "outcome": "unknown", "primary": None, "cleanup_errors": [],
        "ownership": {"unit": "owned_python_wrapper_hold", "acquired": 0, "drop_attempts": 0, "dropped": 0},
        "candidate_is_app": len(prepared_args) == 2 and prepared_args[1] is app,
        "argument_count": len(prepared_args),
        "rows": [{"name": name, "status": "not_run"} for name in names],
        "steps": [],
    }
    if len(prepared_args) != 2 or not isinstance(prepared_args[0], str):
        report.update(stopped=True, stop_reason="argument_contract")
        report["primary"] = {"row": "candidate", "operation": "arguments", "outcome": "invalid"}
        for row in report["rows"]:
            row["reason"] = "argument_contract"
        return report
    groups: list[Any] = []
    owned: list[Any] = []

    def hold(value: Any) -> Any:
        owned.append(value)
        report["ownership"]["acquired"] += 1
        return value

    def step(row: dict[str, Any], operation: str, action: Any) -> tuple[bool, Any]:
        report["steps"].append({"row": row["name"], "operation": operation, "outcome": "begin"})
        try:
            result = action()
        except Exception as error:
            failure = _error(error)
            report["steps"].append({"row": row["name"], "operation": operation, **failure})
            row["status"] = failure["outcome"]
            if failure["outcome"] != "unavailable":
                report.update(stopped=True, stop_reason=failure["outcome"])
                if report["primary"] is None:
                    report["primary"] = {"row": row["name"], "operation": operation, **failure}
            row["reason"] = operation
            return False, None
        report["steps"].append({"row": row["name"], "operation": operation, "outcome": "ok"})
        return True, result

    def describe(row: dict[str, Any], value: Any) -> None:
        row["wrapper_type"] = type(value).__module__ + "." + type(value).__qualname__
        ok, canonical = step(row, "canonical_IUnknown", lambda: backend.canonical(value))
        if not ok:
            return
        hold(canonical)
        # Comparison belongs to the same caller STA; no addresses are exported.
        ok, group = step(row, "canonical_compare", lambda: next(
            (i for i, other in enumerate(groups) if canonical == other), len(groups)))
        if not ok:
            return
        if group == len(groups):
            groups.append(canonical)
        row["group"] = group
        ok, metadata = step(row, "GetTypeInfo", lambda: backend.typeinfo(value))
        if ok:
            row["typeinfo"] = {key: metadata[key] for key in ("guid", "typekind", "major", "minor")
                               if key in metadata and type(metadata[key]) in (str, int)}
            row["status"] = "observed"

    objects: dict[str, Any] = {"rot_app": app}
    try:
        candidate_row = report["rows"][1]
        candidate_row["submitted_type"] = type(prepared_args[1]).__module__ + "." + type(prepared_args[1]).__qualname__
        ok, unpacked = step(candidate_row, "unwrap_local_variant", lambda: backend.unwrap_candidate(prepared_args[1]))
        if not ok:
            return report
        candidate, variant = unpacked
        candidate_row.update(variant)
        report["unwrapped_candidate_is_app"] = candidate is app
        objects["candidate"] = candidate
        describe(candidate_row, candidate)
        if not report["stopped"]:
            describe(report["rows"][0], app)
        for row in report["rows"][2:5]:
            if report["stopped"]:
                break
            source = app if row["name"].startswith("rot_app.") else candidate
            raw = row["name"].endswith("raw_oleobj_")
            ok, result = step(row, "unwrap_local_oleobj" if raw else "QI_Application",
                              lambda: backend.raw(source) if raw else backend.application(source))
            if ok:
                if not raw:
                    hold(result)
                describe(row, result)
                parent_row = report["rows"][0 if row["name"].startswith("rot_app.") else 1]
                if row["status"] == parent_row["status"] == "observed":
                    row["self_control"] = row["group"] == parent_row["group"]
                    if not row["self_control"]:
                        report.update(stopped=True, stop_reason="invalid_self_control")
                        report["primary"] = {"row": row["name"], "operation": "self_control", "outcome": "invalid"}
        for row in report["rows"][5:]:
            if report["stopped"]:
                break
            name = row["name"]
            parent = "candidate" if name in ("Application", "COMAddIns") else name.rsplit(".", 1)[0]
            if parent not in objects:
                continue
            value = objects[parent]
            operation = "Item" if name == "COMAddIns.Item" else name.rsplit(".", 1)[-1]
            ok, result = step(row, operation, lambda: backend.item(value, prog_id)
                              if operation == "Item" else backend.get(value, operation))
            if ok:
                objects[name] = result
                describe(row, result)
    finally:
        # Only owned wrappers obtained by this call are dropped; no explicit
        # Release on the caller's app and no collection-wide GC.
        groups.clear()
        objects.clear()
        while owned:
            report["ownership"]["drop_attempts"] += 1
            owned.pop()
            report["ownership"]["dropped"] += 1
        rows = {row["name"]: row for row in report["rows"]}
        for row in report["rows"]:
            if row["status"] == "not_run":
                row["reason"] = report.get("stop_reason", "dependency_unavailable")
                if not report["stopped"]:
                    row["status"] = "skipped"
        for derived, parent in (("candidate.raw_oleobj_", "candidate"), ("candidate._Application", "candidate"),
                                ("rot_app._Application", "rot_app")):
            if rows[derived]["status"] == rows[parent]["status"] == "observed":
                same = rows[derived]["group"] == rows[parent]["group"]
                rows[derived]["self_control"] = same
                if not same:
                    report.update(stopped=True, stop_reason="invalid_self_control")
                    if report["primary"] is None:
                        report["primary"] = {"row": derived, "operation": "self_control", "outcome": "invalid"}
        report["candidate_same_as_rot"] = rows["candidate"].get("group", -1) == rows["rot_app"].get("group", -2)
        report["usable"] = not report["stopped"] and not report["cleanup_errors"] and all(
            rows[name]["status"] == "observed" for name in ("candidate", "rot_app", "candidate.raw_oleobj_"))
        report["outcome"] = "complete" if report["usable"] else "unknown"
    return report
