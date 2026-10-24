"""Registration retries must not create a second host or weaken target checks."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowmate.config import ppt_registration_timeout_seconds
from knowmate.secure import ppt_active
from knowmate.secure.office_guard import OfficeCleanupPendingError


class LookupErrorWithCode(Exception):
    def __init__(self, code):
        self.hresult = code


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(ppt_active.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ppt_active.time, "sleep", lambda seconds: now.__setitem__(0, now[0]+seconds))
    return now


def test_only_unavailable_retries_without_activation(clock):
    app = Mock()
    client = SimpleNamespace(GetActiveObject=Mock(side_effect=[
        LookupErrorWithCode(-2147221021), LookupErrorWithCode(0x800401E3), app]), Dispatch=Mock())
    check = Mock()
    assert ppt_active.wait_for_active_app(client, check, 1) is app
    assert client.GetActiveObject.call_count == 3
    assert check.call_count == 4
    client.Dispatch.assert_not_called()
    app.Quit.assert_not_called()


def test_timeout_defers_without_new_activation(clock):
    client = SimpleNamespace(GetActiveObject=Mock(side_effect=LookupErrorWithCode(0x800401E3)), Dispatch=Mock())
    with pytest.raises(OfficeCleanupPendingError, match="ppt_registration_timeout"):
        ppt_active.wait_for_active_app(client, Mock(), .12)
    assert clock[0] == pytest.approx(.12)
    client.Dispatch.assert_not_called()


@pytest.mark.parametrize("code", [0x80040154, 0x80070005, 0x80010108, None])
def test_other_lookup_errors_are_not_retried(clock, code):
    error = LookupErrorWithCode(code)
    client = SimpleNamespace(GetActiveObject=Mock(side_effect=error), Dispatch=Mock())
    with pytest.raises(LookupErrorWithCode):
        ppt_active.wait_for_active_app(client, Mock(), 1)
    assert client.GetActiveObject.call_count == 1 and clock[0] == 0
    client.Dispatch.assert_not_called()


def test_target_change_during_registration_prevents_another_lookup(clock):
    check = Mock(side_effect=[None, OfficeCleanupPendingError("POWERPNT.EXE", "target_changed")])
    client = SimpleNamespace(GetActiveObject=Mock(side_effect=LookupErrorWithCode(0x800401E3)), Dispatch=Mock())
    with pytest.raises(OfficeCleanupPendingError, match="target_changed"):
        ppt_active.wait_for_active_app(client, check, 1)
    assert client.GetActiveObject.call_count == 1
    client.Dispatch.assert_not_called()


def test_target_change_after_success_rejects_the_returned_object(clock):
    app = Mock()
    check = Mock(side_effect=[None, OfficeCleanupPendingError("POWERPNT.EXE", "target_changed")])
    client = SimpleNamespace(GetActiveObject=Mock(return_value=app), Dispatch=Mock())
    with pytest.raises(OfficeCleanupPendingError, match="target_changed"):
        ppt_active.wait_for_active_app(client, check, 1)
    app.Quit.assert_not_called()
    client.Dispatch.assert_not_called()


def test_late_lookup_return_is_not_admitted(clock):
    def late(_):
        clock[0] = 2
        return Mock()
    with pytest.raises(OfficeCleanupPendingError, match="ppt_registration_timeout"):
        ppt_active.wait_for_active_app(SimpleNamespace(GetActiveObject=late), Mock(), 1)


@pytest.mark.parametrize("value", [0, -1, True, '10', float('nan'), float('inf'), 301])
def test_invalid_timeout_uses_the_single_bundled_default(value):
    assert ppt_registration_timeout_seconds({'ppt_registration_timeout_sec': value}) == ppt_registration_timeout_seconds({})


def test_runtime_timeout_reaches_the_ppt_reader():
    from knowmate.secure import get_extractor
    from knowmate.secure.com_reader import ComReader
    assert ppt_registration_timeout_seconds({'ppt_registration_timeout_sec': 2.5}) == 2.5
    assert get_extractor('auto', ppt_registration_timeout_sec=2.5)._ppt_registration_timeout_sec == 2.5
    assert ComReader(ppt_registration_timeout_sec=2.5)._ppt.registration_timeout_sec == 2.5
