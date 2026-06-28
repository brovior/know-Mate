"""KnowMate 진입점 — PyQt6 윈도우 + QWebEngineView."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# PyTorch/sentence-transformers가 import되기 전에 설정해야 효과 있음
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

from PyQt6.QtCore import QFile, QIODevice, QUrl, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWebEngineCore import QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWidgets import QApplication, QMainWindow, QSizeGrip

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowmate.app.bridge import Bridge
from knowmate.agents.registry import AgentRegistry

UI_DIR = Path(__file__).parent / "ui"
APP_ICON = UI_DIR / "assets" / "aegisdesk.ico"
logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Aegis Desk")
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(1100, 700)

        self._view = QWebEngineView(self)
        self.setCentralWidget(self._view)

        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(16, 16)
        self._grip.raise_()

        self._channel  = QWebChannel(self._view.page())
        self._registry = AgentRegistry()
        self._bridge   = Bridge(agent_registry=self._registry, main_window=self, parent=self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        self._init_collector()

        _inject_qwebchannel_js(self._view)
        self._view.load(QUrl.fromLocalFile(str(UI_DIR / "index.html")))

    def _init_collector(self) -> None:
        """수집기 파이프라인을 초기화하고 IdleScheduler를 시작한다."""
        try:
            from knowmate.config import get_config
            from knowmate.rag.embedding import get_embedding_client
            from knowmate.rag.indexer import Indexer
            from knowmate.secure import get_extractor
            from knowmate.secure.crypto import get_crypto_manager
            from knowmate.collector.scheduler import CollectorWorker, IdleScheduler

            cfg = get_config()
            db_path = Path(os.environ.get("APPDATA", ".")) / "KnowMate" / "index"
            db_path.mkdir(parents=True, exist_ok=True)

            chunking = cfg.get("chunking", {})
            self._cfg      = cfg
            self._indexer  = Indexer(
                db_path=db_path,
                embed_client=get_embedding_client(cfg),
                chunk_size=chunking.get("chunk_size", 400),
                overlap=chunking.get("overlap", 80),
                batch_size=cfg.get("embedding", {}).get("batch_size", 32),
                crypto=get_crypto_manager(cfg),
            )
            self._extractor = get_extractor(cfg.get("extractor", "fake"))

            # 단일 워커를 생성해 bridge에 연결한다 (수동·유휴 인덱싱 공유)
            self._make_worker()

            # 유휴시간 자동 인덱싱 스케줄러 (6-7)
            # 동일한 단일 워커를 재사용해 동시 실행을 방지한다.
            idle_sec = cfg.get("collector", {}).get("idle_seconds", 60)
            self._idle_scheduler = IdleScheduler(
                trigger=self._trigger_idle_index,
                is_busy=lambda: self._bridge._worker is not None
                and self._bridge._worker.isRunning(),
                idle_seconds=idle_sec,
                parent=self,
            )
            self._idle_scheduler.start()

        except Exception as exc:
            logger.warning("수집기 초기화 실패: %s", exc)

    def _make_worker(self):
        """단일 CollectorWorker를 생성하고 bridge에 연결한다."""
        from knowmate.collector.scheduler import CollectorWorker
        worker = CollectorWorker(
            config=self._cfg,
            indexer=self._indexer,
            extractor=self._extractor,
            parent=self,
        )
        self._bridge.set_worker(worker)
        return worker

    def _trigger_idle_index(self) -> None:
        """유휴 인덱싱을 트리거한다. 공유 워커가 멈춰 있을 때만 시작한다."""
        worker = self._bridge._worker
        if worker is not None and not worker.isRunning():
            worker.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._grip.move(self.width() - 16, self.height() - 16)

    def closeEvent(self, event) -> None:
        """앱 종료 시 스케줄러·워커를 정리해 스레드 누수를 방지한다."""
        try:
            scheduler = getattr(self, "_idle_scheduler", None)
            if scheduler is not None:
                scheduler.stop()
            worker = getattr(self._bridge, "_worker", None)
            if worker is not None and worker.isRunning():
                worker.cancel()
                # 현재 처리 중인 파일 완료 후 종료될 때까지 대기 (최대 10초)
                worker.wait(10000)
        except Exception as exc:
            logger.warning("종료 정리 중 예외: %s", exc)
        super().closeEvent(event)


def _inject_qwebchannel_js(view: QWebEngineView) -> None:
    """Qt 내부 리소스에서 qwebchannel.js를 읽어 페이지 스크립트로 주입한다."""
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("qwebchannel.js 리소스를 열 수 없습니다.")
    content = bytes(f.readAll()).decode("utf-8")
    f.close()

    script = QWebEngineScript()
    script.setName("qwebchannel_init")
    script.setSourceCode(content)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(False)
    view.page().scripts().insert(script)


def _set_windows_app_id(app_id: str) -> None:
    """Windows 작업표시줄이 python.exe 대신 앱 고유 아이콘을 쓰도록 AppUserModelID를 설정한다."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception as exc:  # noqa: BLE001 — 아이콘 그룹핑 실패는 치명적이지 않음
        logger.debug("AppUserModelID 설정 실패: %s", exc)


def main() -> None:
    from knowmate.config import get_config
    _log_level = getattr(logging, get_config().get("log_level", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=_log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    _set_windows_app_id("AegisDesk.App")
    app = QApplication(sys.argv)
    app.setApplicationName("Aegis Desk")
    if APP_ICON.exists():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
