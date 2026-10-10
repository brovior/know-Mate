"""수집 워커 범위의 임베딩 취소와 활성 HTTP 소켓 중단."""
import logging
import socket
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

logger = logging.getLogger(__name__)


class EmbeddingCancelledError(RuntimeError):
    """사용자 취소로 임베딩 작업을 중단했다."""


class EmbeddingCancellation:
    """워커의 취소 요청을 해당 스레드의 임베딩 요청에만 전달한다."""

    def __init__(self) -> None:
        """취소 상태와 진행 중인 소켓을 초기화한다."""
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._in_progress = False

    @property
    def in_progress(self) -> bool:
        """HTTP 연결·요청·응답 처리가 진행 중인지 반환한다."""
        with self._lock:
            return self._in_progress

    def check(self) -> None:
        """취소 요청이 있으면 후속 요청·재시도를 중단한다."""
        if self._cancelled.is_set():
            raise EmbeddingCancelledError("임베딩 작업 취소됨")

    def cancel(self) -> None:
        """취소를 기록하고 연결된 소켓의 입출력 대기를 해제한다."""
        self._cancelled.set()
        with self._lock:
            active_socket = self._socket
        self._interrupt(active_socket)

    def bind_socket(self, active_socket: socket.socket | None) -> None:
        """응답이 연결에서 분리돼도 취소할 수 있게 소켓을 보존한다."""
        with self._lock:
            self._socket = active_socket
        if self._cancelled.is_set():
            self._interrupt(active_socket)
        self.check()

    @staticmethod
    def _interrupt(active_socket: socket.socket | None) -> None:
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError as exc:
                logger.debug("[embed] 취소 소켓 중단 미확인: error_type=%s", type(exc).__name__)
            try:
                # makefile() 참조가 있으면 socket.close()만으로 OS 핸들이 닫히지 않는다.
                descriptor = active_socket.detach()
                if descriptor != -1:
                    socket.close(descriptor)
            except OSError as exc:
                logger.debug("[embed] 취소 소켓 닫기 미확인: error_type=%s", type(exc).__name__)

    @contextmanager
    def request(self) -> Iterator[None]:
        """HTTP 처리 중 상태를 게시하고 완료 시 소켓 참조를 해제한다."""
        with self._lock:
            self._in_progress = True
        try:
            self.check()
            yield
        finally:
            with self._lock:
                self._socket = None
                self._in_progress = False


_current: ContextVar[EmbeddingCancellation | None] = ContextVar("embedding_cancellation", default=None)


def current_embedding_cancellation() -> EmbeddingCancellation | None:
    """현재 요청 스레드의 취소 범위를 반환한다."""
    return _current.get()


@contextmanager
def embedding_cancellation_scope(cancellation: EmbeddingCancellation) -> Iterator[None]:
    """수집 워커의 임베딩 호출에만 취소 범위를 적용한다."""
    token = _current.set(cancellation)
    try:
        yield
    finally:
        _current.reset(token)
