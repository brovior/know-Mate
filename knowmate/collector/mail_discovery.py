"""Worker-owned mail metadata discovery snapshot; never persisted."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MailDiscoverySnapshot:
    """Retain only one complete discovery list between collector cycles."""

    items: list[dict] = field(default_factory=list)
    signature: tuple | None = None
    refreshed_at: float | None = None

    def invalidate(self) -> None:
        """Discard discovery after manual requests or collection changes."""
        self.items = []
        self.signature = None
        self.refreshed_at = None
