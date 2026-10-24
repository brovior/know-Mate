"""Bound caches held by long-lived indexing connections."""
from __future__ import annotations

from typing import Any

import lancedb

from knowmate.config import indexing_cache_settings


def create_indexing_session(config: dict[str, Any] | None = None) -> lancedb.Session:
    """Create a cache session shared by document and mail indexing connections."""
    index_bytes, metadata_bytes = indexing_cache_settings(config)
    return lancedb.Session(index_cache_size_bytes=index_bytes, metadata_cache_size_bytes=metadata_bytes)
