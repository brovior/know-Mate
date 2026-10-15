"""Run one question on its own thread without sharing search handles."""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from knowmate.rag.embedding_cancel import (
    EmbeddingCancellation,
    EmbeddingCancelledError,
    embedding_cancellation_scope,
)

logger = logging.getLogger(__name__)


def log_query_failure(stage: str, error: Exception) -> None:
    """Log a fixed operation and exception type without risking UI delivery."""
    try:
        logger.warning("[query] %s: error_type=%s", stage, type(error).__name__)
    except Exception:  # noqa: BLE001, S110 - diagnostics cannot abort worker cleanup
        pass


class QueryCancellation:
    """Cancel subsequent query stages and the current embedding request."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.embedding = EmbeddingCancellation()

    def check(self) -> None:
        """Raise before starting further work after retirement."""
        if self._event.is_set():
            raise EmbeddingCancelledError("query cancelled")
        self.embedding.check()

    def cancel(self) -> None:
        """Mark the query retired before attempting socket interruption."""
        self._event.set()
        self.embedding.cancel()


def make_registry(config: dict, data_dir: Path) -> object:
    """Construct a request-owned registry in the calling worker thread."""
    from knowmate.agents.registry import AgentRegistry
    return AgentRegistry(config=config, data_dir=data_dir)


class QueryWorker(QThread):
    """Own and release all search resources within one QThread invocation."""

    resultReady = pyqtSignal(str)

    def __init__(self, request: dict, config: dict, data_dir: Path,
                 registry_factory: Callable = make_registry, parent=None) -> None:
        super().__init__(parent)
        self._request = request
        self._config = config
        self._data_dir = data_dir
        self._registry_factory = registry_factory
        self._cancellation = QueryCancellation()

    def cancel(self) -> None:
        """Request cooperative cancellation without terminating the thread."""
        self._cancellation.cancel()

    def run(self) -> None:
        """Build, use and discard the registry before emitting a safe result."""
        registry = agent = context = request = blocks = None
        serialized = None
        status = "success"
        try:
            with embedding_cancellation_scope(self._cancellation.embedding):
                self._cancellation.check()
                registry = self._registry_factory(self._config, self._data_dir)
                self._cancellation.check()
                request = self._request
                context = {key: request[key] for key in (
                    "mode", "scopes", "request_id", "thread_id", "view_generation",
                )}
                context["cancel_check"] = self._cancellation.check
                context["embedding_cancellation"] = self._cancellation.embedding
                if request["mode"] == "knowledge" and not request["scopes"]:
                    raise ValueError("empty scopes")
                agent = registry.get(request["mode"])
                blocks = agent.handle(request["query"], context)
                self._cancellation.check()
                serialized = json.dumps({"status": status, "blocks": blocks}, ensure_ascii=False)
        except EmbeddingCancelledError:
            status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - isolate arbitrary agent/resource failures
            status = "error"
            log_query_failure("worker failed", exc)
        finally:
            if registry is not None:
                try:
                    registry.close()
                except Exception as exc:  # noqa: BLE001 - isolate arbitrary agent/resource failures
                    status = "error"
                    log_query_failure("resource release failed", exc)
            registry = agent = context = request = blocks = None
            self._config = {}
            self._request = {}
        if status != "success":
            serialized = json.dumps({"status": status, "blocks": [{
                "type": "text", "content": "질문 처리가 취소되었습니다." if status == "cancelled"
                else "질문을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            }]}, ensure_ascii=False)
        if serialized is not None:
            try:
                self.resultReady.emit(serialized)
            except Exception as exc:  # noqa: BLE001 - isolate arbitrary agent/resource failures
                log_query_failure("result delivery failed", exc)
