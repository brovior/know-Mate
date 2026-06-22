"""KnowMate 진입점 — PyQt6 윈도우 + QWebEngineView."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QFile, QIODevice, QUrl, Qt
from PyQt6.QtWebEngineCore import QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWidgets import QApplication, QMainWindow, QSizeGrip

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowmate.app.bridge import Bridge
from knowmate.agents.registry import AgentRegistry
from knowmate.config import get_config

UI_DIR = Path(__file__).parent / "ui"

logger = logging.getLogger(__name__)


def _build_collector_worker(cfg: dict, parent=None):
    """config를 읽어 CollectorWorker를 조립해 반환한다."""
    import os
    from knowmate.rag.embedding import EmbeddingClient
    from knowmate.rag.indexer import Indexer
    from knowmate.collector.scheduler import CollectorWorker

    embed_cfg = cfg.get("embedding", {})
    fake_mode = cfg.get("extractor", "fake") == "fake"

    embed_client = EmbeddingClient(
        base_url=embed_cfg.get("base_url", "http://localhost"),
        host_header=embed_cfg.get("host_header", ""),
        fake=fake_mode,
    )

    chunk_cfg = cfg.get("chunking", {})
    appdata = os.environ.get("APPDATA", str(Path.home()))
    db_path = Path(appdata) / "KnowMate" / "index"

    indexer = Indexer(
        db_path=db_path,
        embed_client=embed_client,
        chunk_size=int(chunk_cfg.get("chunk_size", 400)),
        overlap=int(chunk_cfg.get("overlap", 80)),
        batch_size=int(embed_cfg.get("batch_size", 32)),
    )

    extractor_mode = cfg.get("extractor", "fake")
    if extractor_mode == "fake":
        from knowmate.secure.fake_reader import FakeReader
        extractor = FakeReader()
    elif extractor_mode == "plain":
        from knowmate.secure.plain_reader import PlainReader
        extractor = PlainReader()
    else:  # auto
        from knowmate.secure.plain_reader import PlainReader
        extractor = PlainReader()

    return CollectorWorker(cfg, indexer, extractor, parent=parent)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("KnowMate")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(1100, 700)

        self._view = QWebEngineView(self)
        self.setCentralWidget(self._view)

        # 프레임리스 창 우하단 리사이즈 그립
        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(16, 16)
        self._grip.raise_()

        self._channel  = QWebChannel(self._view.page())
        self._registry = AgentRegistry()
        self._bridge   = Bridge(agent_registry=self._registry, main_window=self, parent=self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        _inject_qwebchannel_js(self._view)

        # CollectorWorker 조립 및 브리지 연결
        try:
            cfg = get_config()
            worker = _build_collector_worker(cfg, parent=self)
            self._bridge.set_worker(worker)
            logger.info("CollectorWorker 초기화 완료")
        except Exception as exc:
            logger.error("CollectorWorker 초기화 실패: %s", exc)

        self._view.load(QUrl.fromLocalFile(str(UI_DIR / "index.html")))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._grip.move(self.width() - 16, self.height() - 16)


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


def main() -> None:
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    app = QApplication(sys.argv)
    app.setApplicationName("KnowMate")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
