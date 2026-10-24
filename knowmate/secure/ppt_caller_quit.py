"""Request the Reader's owner-STA UI exit; never call external Application.Quit."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from knowmate.secure.com_scope import _NativeCom
from knowmate.secure.office_guard import OfficeCleanupPendingError

logger = logging.getLogger(__name__)


def request_guarded_quit(service: Any, app: Any, check_target: Callable[[], None]) -> str:
    """Keep the final collection check and UI exit together in the Reader's STA."""
    if _NativeCom().apartment() != (0, 1):
        raise OfficeCleanupPendingError("POWERPNT.EXE", "quit_not_mta")
    check_target()
    status = service.TryQuitIfIdle()
    if status != "quit_requested":
        return status
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            check_target()
            outcome = service.QueryQuitOutcome()
        except Exception as exc:
            logger.info("[ppt] host exit observation pending: error_type=%s", type(exc).__name__)
            return "pending"
        if outcome == "protected":
            logger.info("[ppt] host exit abandoned: reason=close_cancelled")
            return "protected"
        if outcome != "quit_requested":
            return "pending"
        time.sleep(0.05)
    return "quit_requested"
