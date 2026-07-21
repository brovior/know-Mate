"""단일 인스턴스 보장 — QLocalServer/QLocalSocket 기반.

트레이 상주 앱 특성상(닫아도 종료되지 않음) 사용자가 바로가기를 여러 번
눌러 실수로 여러 인스턴스를 띄우기 쉽다. 두 인스턴스가 같은
%APPDATA%/AegisDesk의 LanceDB·index_state.json·threads.json에 동시에
쓰면 락 충돌·데이터 유실 위험이 있다(CLAUDE.md 원칙8 "수집기는 QThread
워커에서만 실행, multiprocessing 금지"와 같은 이유 — 동시 쓰기 자체가
문제).

동작:
  1. 앱 시작 시 명명된 로컬 소켓에 먼저 연결을 시도한다.
  2. 연결되면 이미 다른 인스턴스가 떠 있는 것 → "show" 메시지를 보내고
     새 프로세스는 즉시 종료한다(창을 만들지 않음).
  3. 연결되지 않으면 내가 첫 인스턴스 → 서버로 리슨하며, 이후 다른
     프로세스가 접속해 "show"를 보내면 기존 창을 복원한다.

QLocalServer/Socket은 QCoreApplication 인스턴스가 있어야 하므로, 반드시
QApplication 생성 이후에 호출해야 한다.
"""
import logging

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

_SERVER_NAME = "AegisDeskSingleInstance"
_CONNECT_TIMEOUT_MS = 500
_SHOW_MESSAGE = b"show"


def try_acquire_or_notify_existing() -> bool:
    """이미 실행 중인 인스턴스가 있으면 알리고 False, 없으면(내가 1등) True를 반환한다.

    False가 반환되면 호출부는 새 창을 만들지 말고 즉시 프로세스를 종료해야 한다.
    """
    socket = QLocalSocket()
    socket.connectToServer(_SERVER_NAME)
    if socket.waitForConnected(_CONNECT_TIMEOUT_MS):
        socket.write(_SHOW_MESSAGE)
        socket.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()
        logger.info("Aegis Desk가 이미 실행 중 — 기존 창을 표시하도록 알림")
        return False
    return True


class SingleInstanceServer(QObject):
    """첫 인스턴스에서 리슨하며, 이후 실행 요청이 오면 show_requested를 emit한다."""

    show_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        """서버를 시작한다. 이전 비정상 종료로 이름이 남아있으면 정리 후 재시도한다."""
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        QLocalServer.removeServer(_SERVER_NAME)
        if not self._server.listen(_SERVER_NAME):
            logger.warning("단일 인스턴스 서버 시작 실패(무시 — 중복 실행 방지 비활성): %s", self._server.errorString())

    def _on_new_connection(self) -> None:
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(lambda: self._on_ready_read(conn))

    def _on_ready_read(self, conn) -> None:
        data = bytes(conn.readAll())
        if data == _SHOW_MESSAGE:
            self.show_requested.emit()
        conn.disconnectFromServer()

    def close(self) -> None:
        """서버를 닫는다(명시적 정리용 — 프로세스 종료 시에도 OS가 자동 회수한다)."""
        self._server.close()
