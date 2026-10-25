"""Build-only PowerPoint installation policy; user configuration cannot enable it."""
from __future__ import annotations

import sys
from knowmate.build_info import get_build_info

REQUIRED_VERSION = "1.0.5.11"
AUTO_INSTALL_MODES = frozenset({"disabled", "candidate", "released"})


def auto_install_mode() -> str:
    """Read frozen build policy, with disabled as the source execution default."""
    if not getattr(sys, "frozen", False):
        return "disabled"
    mode = get_build_info().get("ppt_auto_install_mode")
    return mode if isinstance(mode, str) and mode in AUTO_INSTALL_MODES else "disabled"


def auto_install_enabled() -> bool:
    """Allow an explicit build candidate without claiming it is validated."""
    return auto_install_mode() in {"candidate", "released"}


def auto_shutdown_enabled() -> bool:
    """Permit only candidate experiments until Windows callback safety is verified."""
    return auto_install_mode() == "candidate"
