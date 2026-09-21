"""원자적 QLocalServer 선점으로 Aegis Desk 단일 인스턴스를 보장한다."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QLockFile, QObject, QStandardPaths, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

_SERVER_NAME = "AegisDeskSingleInstance"
_CONNECT_TIMEOUT_MS = 500
_SHOW_MESSAGE = b"show"
_LOCK_FILENAME = "single_instance.lock"


@dataclass(frozen=True)
class SingleInstanceAcquireResult:
    """원자적 선점 시도의 결과다."""

    server: "SingleInstanceServer | None"
    secondary_notified: bool = False

    @property
    def acquired(self) -> bool:
        """이 프로세스가 유일한 primary인지 반환한다."""
        return self.server is not None


def _notify_existing() -> bool:
    """실행 중인 primary에 창 표시 요청을 보내고 성공 여부를 반환한다."""
    socket = QLocalSocket()
    try:
        socket.connectToServer(_SERVER_NAME)
        if not socket.waitForConnected(_CONNECT_TIMEOUT_MS):
            return False
        socket.write(_SHOW_MESSAGE)
        socket.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
        logger.info("Aegis Desk가 이미 실행 중 — 기존 창을 표시하도록 알림")
        return True
    finally:
        socket.disconnectFromServer()
        socket.deleteLater()


def _authority_lock_path() -> str:
    """현재 사용자 AppData 아래의 단일 인스턴스 권위 락 경로를 반환한다."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        directory = Path(appdata) / "AegisDesk"
    else:
        directory = Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation,
        ))
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / _LOCK_FILENAME)


def _new_authority_lock() -> QLockFile:
    """프로세스 수명 동안 보유할 사용자별 권위 락을 만든다."""
    lock = QLockFile(_authority_lock_path())
    lock.setStaleLockTime(0)
    return lock


def acquire_or_notify_existing(parent: QObject | None = None) -> SingleInstanceAcquireResult:
    """권위 락 뒤에 서버를 원자적으로 선점하거나 기존 primary에 show를 요청한다.

    authority lock을 얻은 프로세스만 stale endpoint를 제거할 수 있다. 락을 얻지
    못한 secondary 후보는 notify만 재시도하고 끝까지 endpoint를 건드리지 않는다.
    """
    lock = _new_authority_lock()
    if not lock.tryLock(0):
        if _notify_existing():
            return SingleInstanceAcquireResult(None, secondary_notified=True)
        if _notify_existing():
            return SingleInstanceAcquireResult(None, secondary_notified=True)
        logger.critical("단일 인스턴스 권위 락을 확보할 수 없어 실행을 중단합니다")
        return SingleInstanceAcquireResult(None)
    return _listen_with_authority_lock(parent, lock)


def _listen_with_authority_lock(
    parent: QObject | None,
    lock: QLockFile,
) -> SingleInstanceAcquireResult:
    """권위 락 보유자만 stale server endpoint를 정리하고 listen한다."""
    server = SingleInstanceServer(parent, authority_lock=lock)
    if server.listen():
        return SingleInstanceAcquireResult(server)
    if _notify_existing():
        server.close()
        return SingleInstanceAcquireResult(None, secondary_notified=True)

    QLocalServer.removeServer(_SERVER_NAME)
    if server.listen():
        logger.info("단일 인스턴스의 오래된 로컬 endpoint를 정리하고 선점했습니다")
        return SingleInstanceAcquireResult(server)

    logger.critical("단일 인스턴스 선점 실패: %s", server.error_string())
    server.close()
    return SingleInstanceAcquireResult(None)


class SingleInstanceServer(QObject):
    """원자적으로 선점된 local server와 초기화 전 show 요청을 보관한다."""

    show_requested = pyqtSignal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        authority_lock: QLockFile | None = None,
    ) -> None:
        """아직 listen하지 않은 server를 만든다."""
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._connections: dict[QLocalSocket, bytearray] = {}
        self._show_pending = False
        self._authority_lock = authority_lock

    def listen(self) -> bool:
        """이름을 원자적으로 선점하고 성공 여부를 반환한다."""
        return self._server.listen(_SERVER_NAME)

    def error_string(self) -> str:
        """마지막 listen 오류를 반환한다."""
        return self._server.errorString()

    def take_pending_show(self) -> bool:
        """창 준비 전 들어온 show 요청을 한 번 소비한다."""
        pending = self._show_pending
        self._show_pending = False
        return pending

    def _on_new_connection(self) -> None:
        """한 Qt 이벤트에 쌓인 모든 pending 연결을 보관하고 읽는다."""
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is None:
                break
            self._connections[conn] = bytearray()
            conn.readyRead.connect(lambda conn=conn: self._on_ready_read(conn))
            conn.disconnected.connect(lambda conn=conn: self._release_connection(conn))
            if conn.bytesAvailable():
                self._on_ready_read(conn)

    def _on_ready_read(self, conn: QLocalSocket) -> None:
        """완전한 show 메시지만 처리하고 연결을 닫는다."""
        buffer = self._connections.get(conn)
        if buffer is None:
            return
        buffer.extend(bytes(conn.readAll()))
        if len(buffer) < len(_SHOW_MESSAGE):
            return
        if bytes(buffer) == _SHOW_MESSAGE:
            self._show_pending = True
            self.show_requested.emit()
        conn.disconnectFromServer()

    def _release_connection(self, conn: QLocalSocket) -> None:
        """완료된 secondary 연결의 강한 참조와 Qt 객체를 함께 정리한다."""
        self._connections.pop(conn, None)
        conn.deleteLater()

    def close(self) -> None:
        """서버와 보류 중 연결을 닫는다."""
        for conn in tuple(self._connections):
            conn.disconnectFromServer()
            conn.deleteLater()
        self._connections.clear()
        self._server.close()
        if self._authority_lock is not None:
            self._authority_lock.unlock()
            self._authority_lock = None
