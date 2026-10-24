"""Bounded existing-object lookup; this path never activates another Office host."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from knowmate.secure.com_fault import clear_com_error_frames, outer_hresult

logger = logging.getLogger(__name__)


def wait_for_active_app(client: Any, check_target: Callable[[], None], timeout: float) -> Any:
    """Wait for ROT registration while rechecking the original process capability."""
    from knowmate.secure.office_guard import OfficeCleanupPendingError

    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    logger.info("[ppt] COM registration wait: timeout_sec=%.3f", timeout)
    while True:
        check_target()
        if attempts and time.monotonic() >= deadline:
            break
        attempts += 1
        try:
            app = client.GetActiveObject("PowerPoint.Application")
        except Exception as error:
            code = outer_hresult(error)
            clear_com_error_frames(error)
            if code != 0x800401E3:
                logger.warning("[ppt] COM registration lookup failed: hresult=%s error_type=%s",
                               code, type(error).__name__)
                raise
        else:
            try:
                check_target()
            except BaseException:
                app = None
                raise
            if time.monotonic() >= deadline:
                app = None
                break
            logger.info("[ppt] COM registration ready: attempts=%d elapsed_ms=%d",
                        attempts, round((time.monotonic() - started) * 1000))
            return app
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    logger.warning("[ppt] COM registration timeout: attempts=%d elapsed_ms=%d",
                   attempts, round((time.monotonic() - started) * 1000))
    raise OfficeCleanupPendingError("POWERPNT.EXE", "ppt_registration_timeout")
