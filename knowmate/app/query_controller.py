"""GUI-owned single-flight admission and lifetime for question workers."""
from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal, pyqtSlot

from knowmate.app.query_worker import QueryWorker, log_query_failure

_IDS = ("request_id", "thread_id", "mode", "view_generation")


class QueryController(QObject):
    """Keep admission closed until the previous QThread actually joins."""

    responseReady = pyqtSignal(str)
    stateChanged = pyqtSignal(str)

    def __init__(self, parent=None, worker_factory: Callable = QueryWorker) -> None:
        super().__init__(parent)
        self._worker_factory = worker_factory
        self._worker = None
        self._request: dict = {}
        self._last_id = ""
        self._terminal = False
        self._finished_seen = False
        self._closing = False
        self._retired = False
        self._timeout_ms = 0
        self._timeout: QTimer | None = None
        self._join_timer = QTimer(self)
        self._join_timer.setInterval(10)
        self._join_timer.timeout.connect(self._poll_join)

    @property
    def busy(self) -> bool:
        """Report unresolved ownership, including finished-before-join."""
        return self._worker is not None

    @property
    def closing(self) -> bool:
        """Report the permanent application-shutdown admission barrier."""
        return self._closing

    def state(self) -> dict:
        """Return UI state without exposing the query or configuration."""
        return {
            "request_id": self._request.get("request_id", self._last_id),
            "busy": self.busy,
            "state": "CLOSING" if self._closing else (
                "RETIRED_PENDING_EXIT" if self._retired and self.busy else
                "RUNNING" if self.busy else "IDLE"
            ),
            "timeout_ms": self._timeout_ms,
        }

    def submit(self, request: dict, config: dict, data_dir: Path, timeout_ms: int) -> None:
        """Start one worker or reject immediately without queueing."""
        if self._closing or self.busy:
            self.reject(request, "이전 질문 처리가 끝난 뒤 다시 시도해 주세요.")
            return
        self._request = {key: request[key] for key in _IDS}
        self._last_id = request["request_id"]
        self._terminal = self._finished_seen = self._retired = False
        self._timeout_ms = timeout_ms
        try:
            worker = self._worker_factory(deepcopy(request), deepcopy(config), data_dir, parent=self)
            self._worker = worker
            worker.resultReady.connect(self._on_result, Qt.ConnectionType.QueuedConnection)
            worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
            self._timeout = QTimer(self)
            self._timeout.setSingleShot(True)
            self._timeout.timeout.connect(self._on_timeout)
            self._publish_state()
            self._timeout.start(timeout_ms)
            worker.start()
        except Exception as exc:  # noqa: BLE001 - preserve the execution lease on worker errors
            log_query_failure("start failed", exc)
            self._emit_terminal("error", self._error_blocks())
            self._retired = True
            if self._worker is not None:
                try:
                    self._worker.cancel()
                except Exception as exc:  # noqa: BLE001 - worker may still own resources
                    log_query_failure("start rollback cancellation failed", exc)
                self._finished_seen = True
                self._poll_join()
                if self.busy:
                    self._join_timer.start()
                    self._publish_state()
            else:
                self._release_joined()

    def reject(self, request: dict, message: str) -> None:
        """Report a rejected submission without terminating an active request."""
        envelope = {key: request.get(key) for key in _IDS}
        envelope.update(status="rejected", blocks=[{"type": "text", "content": message}])
        self.responseReady.emit(json.dumps(envelope, ensure_ascii=False))

    def cancel(self, request_id: str, status: str = "cancelled") -> None:
        """Retire only the matching request and preserve its running lease."""
        if not self.busy or request_id != self._request.get("request_id"):
            return
        self._retired = True
        message = ("응답 대기 시간이 지났습니다. 이전 요청을 정리하는 중입니다."
                   if status == "timeout" else "질문 처리가 취소되었습니다.")
        self._emit_terminal(status, [{"type": "text", "content": message}])
        self._stop_timeout()
        self._publish_state()
        try:
            self._worker.cancel()
        except Exception as exc:  # noqa: BLE001 - preserve the execution lease on worker errors
            log_query_failure("cancel failed", exc)

    def begin_shutdown(self) -> None:
        """Suppress response delivery and request cancellation before Office cleanup."""
        self._closing = True
        self._retired = True
        self._stop_timeout()
        self._join_timer.stop()
        if self._worker is not None:
            self._worker.cancel()

    def wait_for_shutdown(self, timeout_ms: int = 8000) -> bool:
        """Join cooperatively at app exit; never terminate a query thread."""
        if self._worker is None:
            return True
        try:
            if not self._worker.wait(0) and not self._worker.wait(timeout_ms):
                return False
            self._release_joined()
            return True
        except Exception:  # noqa: BLE001 - failed join is unsafe, never normal termination
            return False

    @pyqtSlot(str)
    def _on_result(self, serialized: str) -> None:
        if self.sender() is not self._worker or self._terminal or self._closing or self._retired:
            return
        try:
            result = json.loads(serialized)
            if not isinstance(result, dict) or result.get("status") not in {"success", "error", "cancelled"}:
                raise ValueError("invalid worker result")
            blocks = result["blocks"]
            if not isinstance(blocks, list):
                raise TypeError("invalid blocks")
            self._emit_terminal(result["status"], blocks)
        except (ValueError, TypeError, KeyError):
            self._emit_terminal("error", self._error_blocks())

    @pyqtSlot()
    def _on_finished(self) -> None:
        if self.sender() is not self._worker:
            return
        self._finished_seen = True
        if not self._terminal and not self._closing:
            self._emit_terminal("error", self._error_blocks())
        self._poll_join()
        if self.busy and not self._closing:
            self._join_timer.start()

    @pyqtSlot()
    def _poll_join(self) -> None:
        if not self._finished_seen or self._worker is None:
            return
        try:
            joined = self._worker.wait(0)
        except Exception:  # noqa: BLE001 - keep ownership when Qt cannot confirm exit
            joined = False
        if joined:
            self._release_joined()

    @pyqtSlot()
    def _on_timeout(self) -> None:
        if self.sender() is self._timeout and not self._closing:
            self.cancel(self._request.get("request_id", ""), "timeout")

    def _emit_terminal(self, status: str, blocks: list) -> None:
        if self._terminal or self._closing:
            return
        self._terminal = True
        self._stop_timeout()
        envelope = {**self._request, "status": status, "blocks": blocks}
        self.responseReady.emit(json.dumps(envelope, ensure_ascii=False))

    def _release_joined(self) -> None:
        self._stop_timeout()
        self._join_timer.stop()
        worker, self._worker = self._worker, None
        self._request = {}
        self._retired = False
        if worker is not None:
            worker.deleteLater()
        if not self._closing:
            self._publish_state()

    def _stop_timeout(self) -> None:
        if self._timeout is not None:
            self._timeout.stop()
            self._timeout.deleteLater()
            self._timeout = None

    def _publish_state(self) -> None:
        if not self._closing:
            self.stateChanged.emit(json.dumps(self.state(), ensure_ascii=False))

    @staticmethod
    def _error_blocks() -> list[dict]:
        return [{"type": "text", "content": "질문을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."}]
