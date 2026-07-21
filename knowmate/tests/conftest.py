"""pytest 전역 설정 — PyQt6 QObject/QTimer 테스트를 위한 QApplication 준비.

QTimer 등 QObject 파생 클래스는 프로세스에 QCoreApplication(또는 QApplication)
인스턴스가 존재해야 안전하게 생성·동작한다. 세션 스코프로 한 번만 만들어
프로세스 종료까지 살려 둔다(중간에 파괴하면 이후 QTimer가 "event dispatcher
has already been destroyed"로 실패한다). exec()는 호출하지 않는다 — 우리
테스트는 실제 이벤트 루프 구동 없이 QTimer의 start/stop/interval 상태만
확인하므로 이벤트 루프 실행은 불필요하다.

PyQt6가 없는 환경(사외)에서는 조용히 아무것도 하지 않는다.
"""
import pytest

try:
    from PyQt6.QtWidgets import QApplication
    _HAS_PYQT6 = True
except ImportError:
    _HAS_PYQT6 = False


@pytest.fixture(scope="session", autouse=True)
def _qapp_session():
    """세션 전체에서 재사용할 단일 QApplication 인스턴스를 보장한다."""
    if not _HAS_PYQT6:
        yield None
        return
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    # 세션 종료까지 app을 명시적으로 destroy하지 않는다(프로세스 종료 시 자연 정리).
