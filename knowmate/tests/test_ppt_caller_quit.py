"""The caller requests a host-owned UI exit and never invokes Application.Quit."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowmate.secure import ppt_caller_quit as caller
from knowmate.secure.office_guard import OfficeCleanupPendingError


@pytest.fixture(autouse=True)
def mta(monkeypatch):
    monkeypatch.setattr(caller, "_NativeCom", lambda: SimpleNamespace(apartment=lambda: (0, 1)))


@pytest.mark.parametrize("status", ["protected", "not_supported", "failed"])
def test_host_refusal_never_uses_an_external_exit(status):
    app = Mock()
    service = SimpleNamespace(TryQuitIfIdle=Mock(return_value=status), QueryQuitOutcome=Mock())
    check = Mock()
    assert caller.request_guarded_quit(service, app, check) == status
    check.assert_called_once()
    app.Quit.assert_not_called()
    service.QueryQuitOutcome.assert_not_called()


def test_late_close_cancellation_is_observed_after_the_request_returns():
    app = Mock()
    service = SimpleNamespace(TryQuitIfIdle=lambda: "quit_requested", QueryQuitOutcome=lambda: "protected")
    assert caller.request_guarded_quit(service, app, Mock()) == "protected"
    app.Quit.assert_not_called()


def test_disconnected_host_stops_rpc_and_leaves_exit_to_native_observation():
    app = Mock()
    query = Mock(side_effect=RuntimeError("disconnected"))
    service = SimpleNamespace(TryQuitIfIdle=Mock(return_value="quit_requested"), QueryQuitOutcome=query)
    assert caller.request_guarded_quit(service, app, Mock()) == "pending"
    query.assert_called_once()
    service.TryQuitIfIdle.assert_called_once()
    app.Quit.assert_not_called()


def test_changed_target_does_not_issue_any_service_request():
    service = Mock()
    with pytest.raises(OfficeCleanupPendingError):
        caller.request_guarded_quit(service, Mock(), Mock(side_effect=OfficeCleanupPendingError("POWERPNT.EXE", "changed")))
    service.TryQuitIfIdle.assert_not_called()


def test_unknown_outcome_does_not_repeat_exit_or_fall_back():
    app = Mock()
    service = SimpleNamespace(TryQuitIfIdle=Mock(return_value="quit_requested"), QueryQuitOutcome=lambda: "unrecognized")
    assert caller.request_guarded_quit(service, app, Mock()) == "pending"
    service.TryQuitIfIdle.assert_called_once()
    app.Quit.assert_not_called()


@pytest.mark.parametrize("apartment", [(0, 0), (0, 2), (-1, 1)])
def test_another_apartment_is_rejected(monkeypatch, apartment):
    monkeypatch.setattr(caller, "_NativeCom", lambda: SimpleNamespace(apartment=lambda: apartment))
    service = Mock()
    with pytest.raises(OfficeCleanupPendingError):
        caller.request_guarded_quit(service, Mock(), Mock())
    service.TryQuitIfIdle.assert_not_called()
