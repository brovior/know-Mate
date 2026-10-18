"""Pure fake coverage of the controller's conservative getter policy."""
from scripts.ppt_reader.ppt_identity_observation import collect


class ComFailure(Exception):
    def __init__(self, hr, inner=None):
        self.hresult = hr
        self.excepinfo = (None, None, None, None, None, inner)


class Fake:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def unwrap_candidate(self, value):
        return value, {"explicit_variant": False, "vartype": None}

    def raw(self, value):
        return value

    def application(self, value):
        return value

    def canonical(self, value):
        return value

    def typeinfo(self, value):
        return {"guid": "fixture", "typekind": 4, "major": 0, "minor": 0}

    def get(self, value, name):
        self.calls.append(name)
        if name == "Application" and len(self.calls) == 1 and self.failure:
            raise self.failure
        return value

    def item(self, value, prog_id):
        self.calls.append("Item")
        return value


def test_controller_missing_application_continues_and_records_exact_getters():
    app = object()
    backend = Fake(ComFailure(-2147352570))  # DISP_E_UNKNOWNNAME
    result = collect(app, ("nonce", app), "fixture", backend)
    assert backend.calls == ["Application", "COMAddIns", "Item", "Application"]
    assert result["rows"][5]["status"] == "unavailable"
    assert result["rows"][-1]["status"] == "observed"
    assert not result["stopped"] and not result["invoked"]


def test_controller_inner_efail_is_unknown_and_stops_all_further_rpc():
    app = object()
    backend = Fake(ComFailure(-2147352567, -2147467259))
    result = collect(app, ("nonce", app), "fixture", backend)
    assert backend.calls == ["Application"]
    assert result["stop_reason"] == "unknown"
    assert all(row["status"] == "not_run" for row in result["rows"][6:])
    assert result["steps"][-1]["inner_hresult"] == -2147467259


def test_controller_fatal_stops_and_argument_mismatch_never_probes():
    app = object()
    backend = Fake(ComFailure(-2147417848))  # RPC_E_DISCONNECTED
    assert collect(app, ("nonce", app), "fixture", backend)["stop_reason"] == "fatal"
    assert backend.calls == ["Application"]
    backend.calls.clear()
    assert collect(app, ("nonce",), "fixture", backend)["stop_reason"] == "argument_contract"
    assert backend.calls == []


def test_controller_plain_attribute_error_is_not_assumed_remote_absence():
    app = object()
    backend = Fake(AttributeError("private text must not be logged"))
    result = collect(app, ("nonce", app), "fixture", backend)
    assert result["stop_reason"] == "unknown"
    assert "private text" not in str(result)


def test_controller_item_failure_names_exact_stage_without_application_followup():
    class BrokenItem(Fake):
        def item(self, value, prog_id):
            self.calls.append("Item")
            raise ComFailure(-2147352567, -2147467259)
    app = object()
    backend = BrokenItem()
    result = collect(app, ("nonce", app), "fixture", backend)
    assert backend.calls == ["Application", "COMAddIns", "Item"]
    assert result["steps"][-1]["row"] == "COMAddIns.Item"
    assert result["steps"][-1]["operation"] == "Item"
    assert result["rows"][-1]["status"] == "not_run"


def test_controller_same_facade_groups_and_primitive_report():
    import json
    app = object()
    result = collect(app, ("nonce", app), "fixture", Fake())
    assert all(row["status"] == "observed" for row in result["rows"])
    assert [row["group"] for row in result["rows"]] == [0] * 9
    encoded = json.dumps(result)
    assert "0x" not in encoded


def test_controller_distinct_facades_are_not_collapsed():
    class Different(Fake):
        def get(self, value, name):
            return object()
        def item(self, value, prog_id):
            return object()
    app = object()
    result = collect(app, ("nonce", app), "fixture", Different())
    assert [row["group"] for row in result["rows"]] == [0, 0, 0, 0, 0, 1, 2, 3, 4]


def test_controller_describes_actual_submitted_candidate_separately_from_rot():
    app, candidate = object(), object()
    result = collect(app, ("nonce", candidate), "fixture", Fake())
    rows = {row["name"]: row for row in result["rows"]}
    assert result["usable"] and not result["candidate_is_app"]
    assert not result["candidate_same_as_rot"]
    assert rows["candidate"]["group"] == rows["candidate.raw_oleobj_"]["group"] == rows["candidate._Application"]["group"]
    assert rows["rot_app"]["group"] == rows["rot_app._Application"]["group"] != rows["candidate"]["group"]


def test_variant_unwrap_does_not_invoke_value_property_or_default_member():
    from scripts.ppt_reader.ppt_identity_observation import Pywin32Backend
    class Variant:
        def __init__(self, value):
            self.varianttype, self._value = 9, value
        @property
        def value(self):
            raise AssertionError("property must not run")
        def __getattr__(self, name):
            raise AssertionError("fallback must not run")
    value = object()
    backend = Pywin32Backend(None, Variant)
    unpacked, metadata = backend.unwrap_candidate(Variant(value))
    assert unpacked is value and metadata == {"explicit_variant": True, "vartype": 9}


def test_raw_unwrap_does_not_invoke_custom_oleobj_property():
    from scripts.ppt_reader.ppt_identity_observation import Pywin32Backend
    class NoWrapper:
        @property
        def _oleobj_(self):
            raise AssertionError("property must not run")
    value = NoWrapper()
    assert Pywin32Backend.raw(value) is value
