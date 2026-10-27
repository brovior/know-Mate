"""Policy writers are never terminated mid-database or config mutation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowmate.app import lifecycle


@pytest.mark.parametrize("explicit", [True, False])
def test_policy_timeout_preserves_office_cleanup_and_skips_thread_terminate(explicit):
    """An unresponsive policy writer goes straight to process-level hard exit."""
    calls = []
    worker = SimpleNamespace(
        isRunning=lambda: True, cancel=lambda: calls.append("cancel"),
        wait=lambda ms: calls.append(("wait", ms)) or False,
        terminate=lambda: pytest.fail("policy QThread.terminate"),
        policy_job_pending=not explicit,
        maintenance_in_progress=False, blocking_shutdown_work=False,
    )
    result = lifecycle.stop_worker(
        worker, policy_active=explicit,
        office_cleanup=lambda _worker: calls.append("office_cleanup") or False,
        hard_exit=lambda code: calls.append(("hard_exit", code)),
    )
    assert result is True
    assert calls[-2:] == ["office_cleanup", ("hard_exit", 0)]


def test_policy_cooperative_exit_requires_no_hard_exit():
    """A joined policy writer follows normal bounded Office cleanup."""
    calls = []
    worker = SimpleNamespace(
        isRunning=lambda: True, cancel=lambda: calls.append("cancel"), wait=lambda _ms: True,
        terminate=lambda: pytest.fail("policy QThread.terminate"), policy_job_pending=True,
    )
    assert not lifecycle.stop_worker(
        worker, office_cleanup=lambda _worker: calls.append("office_cleanup") or False,
        hard_exit=lambda _code: pytest.fail("unexpected hard exit"),
    )
    assert calls == ["cancel", "office_cleanup"]
