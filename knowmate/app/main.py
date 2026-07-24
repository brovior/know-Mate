"""Aegis Desk 진입점 — PyQt6 윈도우 + QWebEngineView."""
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
from PyQt6.QtGui import QIcon, QAction
from PyQt6.QtWebEngineCore import QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWidgets import QApplication, QMainWindow, QSizeGrip, QSystemTrayIcon, QMenu

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def resource_path(rel: str) -> Path:
    """소스 실행/PyInstaller 번들(frozen) 모두에서 동작하는 리소스 경로를 반환한다.

    frozen(exe)일 때는 파일들이 sys._MEIPASS(임시 해제 폴더) 아래 있다.
    rel은 ROOT 기준 상대경로(예: "knowmate/app/ui").
    """
    base = Path(getattr(sys, "_MEIPASS", ROOT))
    return base / rel


from knowmate.app.bridge import Bridge
from knowmate.agents.registry import AgentRegistry

UI_DIR = resource_path("knowmate/app/ui")
APP_ICON = UI_DIR / "assets" / "aegisdesk.ico"
logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, dirty_shutdown_detected: bool = False) -> None:
        """메인 윈도우를 초기화한다.

        dirty_shutdown_detected: 이전 실행이 강제 종료됐는지(main()이 시작 시
            check_and_remark_dirty_shutdown()으로 판정) — True면 트레이 초기화 후
            풍선 알림으로 재인덱싱을 권장한다(설계 리뷰 11차 M-1, 로그만으로는
            GUI 사용자가 놓치기 쉬움).
        """
        super().__init__()
        self.setWindowTitle("Aegis Desk")
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(1100, 700)

        self._tray: QSystemTrayIcon | None = None
        self._really_quit = False
        self._shutdown_done = False

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
        self._init_tray()
        if dirty_shutdown_detected and self._tray is not None:
            self._tray.showMessage(
                "Aegis Desk",
                "이전 실행이 정상적으로 종료되지 않았습니다. 검색 결과가 이상하면 "
                "폴더를 제거 후 재추가해 재인덱싱하는 것을 권장합니다.",
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )

        _inject_qwebchannel_js(self._view)
        self._view.load(QUrl.fromLocalFile(str(UI_DIR / "index.html")))

    def _init_tray(self) -> None:
        """시스템 트레이 아이콘과 메뉴를 초기화한다. 창을 닫아도 백그라운드 상주한다."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.debug("시스템 트레이 사용 불가 — 트레이 상주 비활성")
            return

        icon = QIcon(str(APP_ICON)) if APP_ICON.exists() else self.windowIcon()
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Aegis Desk")

        menu = QMenu()
        act_open = QAction("열기", self)
        act_open.triggered.connect(self._show_from_tray)
        act_reindex = QAction("지금 재인덱싱", self)
        act_reindex.triggered.connect(self._tray_reindex)
        act_quit = QAction("종료", self)
        act_quit.triggered.connect(self._quit_app)
        menu.addAction(act_open)
        menu.addAction(act_reindex)
        menu.addSeparator()
        menu.addAction(act_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason) -> None:
        """트레이 아이콘 클릭(더블클릭 포함) 시 창을 복원한다."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        """트레이에서 창을 복원하고 앞으로 가져온다."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_reindex(self) -> None:
        """트레이 메뉴에서 재인덱싱을 트리거한다(진행 중이면 무시)."""
        worker = getattr(self._bridge, "_worker", None)
        if worker is not None and not worker.isRunning():
            worker.start()

    def _quit_app(self) -> None:
        """트레이 '종료' — 실제 앱 종료 플래그를 세우고 닫는다."""
        self._really_quit = True
        self.close()

    def _init_collector(self) -> None:
        """수집기 파이프라인을 초기화하고 IdleScheduler를 시작한다."""
        try:
            from knowmate.config import get_config, get_data_dir
            from knowmate.rag.embedding import get_embedding_client
            from knowmate.rag.indexer import Indexer
            from knowmate.secure import get_extractor
            from knowmate.secure.crypto import get_crypto_manager
            from knowmate.collector.scheduler import CollectorWorker, IdleScheduler

            cfg = get_config()
            db_path = get_data_dir() / "index"
            db_path.mkdir(parents=True, exist_ok=True)

            chunking = cfg.get("chunking", {})
            batch_size = cfg.get("embedding", {}).get("batch_size", 32)
            embed_client = get_embedding_client(cfg)
            crypto = get_crypto_manager(cfg)
            self._cfg      = cfg
            self._indexer  = Indexer(
                db_path=db_path,
                embed_client=embed_client,
                chunk_size=chunking.get("chunk_size", 400),
                overlap=chunking.get("overlap", 80),
                batch_size=batch_size,
                crypto=crypto,
            )
            # 메일(.mysingle) 인덱서 — mail.enabled: true 일 때 워커가 사용
            from knowmate.rag.email_indexer import EmailIndexer
            self._email_indexer = EmailIndexer(
                db_path=db_path,
                embed_client=embed_client,
                chunk_size=chunking.get("chunk_size", 400),
                overlap=chunking.get("overlap", 80),
                batch_size=batch_size,
                crypto=crypto,
            )
            self._extractor = get_extractor(cfg.get("extractor", "fake"))

            # 단일 워커를 생성해 bridge에 연결한다 (수동·유휴 인덱싱 공유)
            self._make_worker()

            # 유휴시간 자동 인덱싱 스케줄러 (6-7). 설정에서 끌 수 있다(collector.idle_enabled).
            # 동일한 단일 워커를 재사용해 동시 실행을 방지한다.
            if cfg.get("collector", {}).get("idle_enabled", True):
                idle_sec = cfg.get("collector", {}).get("idle_seconds", 60)
                drm_idle_threshold_sec = cfg.get("collector", {}).get("drm_idle_threshold_sec", 480)
                self._idle_scheduler = IdleScheduler(
                    trigger=self._trigger_idle_index,
                    is_busy=lambda: self._bridge._worker is not None
                    and self._bridge._worker.isRunning(),
                    idle_seconds=idle_sec,
                    drm_idle_threshold_sec=drm_idle_threshold_sec,
                    parent=self,
                )
                self._idle_scheduler.start()
            else:
                logger.info("유휴 자동 인덱싱 비활성화됨 (collector.idle_enabled=false)")

        except Exception as exc:
            logger.warning("수집기 초기화 실패: %s", exc)

    def _make_worker(self):
        """단일 CollectorWorker를 생성하고 bridge에 연결한다."""
        from knowmate.collector.scheduler import CollectorWorker
        worker = CollectorWorker(
            config=self._cfg,
            indexer=self._indexer,
            extractor=self._extractor,
            email_indexer=getattr(self, "_email_indexer", None),
            parent=self,
        )
        self._bridge.set_worker(worker)
        return worker

    def _trigger_idle_index(self) -> None:
        """유휴 인덱싱을 트리거한다. 공유 워커가 멈춰 있을 때만 시작한다.

        DRM 의심 문서 스킵 판단(collector.drm_idle_threshold_sec)은 워커가
        사이클 도중 실시간 유휴를 직접 조회하므로 여기서 유휴 값을 넘기지 않는다.
        """
        worker = self._bridge._worker
        if worker is not None and not worker.isRunning():
            worker.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._grip.move(self.width() - 16, self.height() - 16)

    def closeEvent(self, event) -> None:
        """X/닫기 시 트레이가 있고 설정이 tray면 종료 대신 트레이로 숨긴다.

        ui.close_action 설정(tray|quit)으로 사용자가 동작을 바꿀 수 있다.
        실제 종료는 _quit_app 경유(트레이 메뉴의 [종료]) 또는 close_action=quit.
        """
        close_action = getattr(self, "_cfg", {}).get("ui", {}).get("close_action", "tray")
        if self._tray is not None and close_action == "tray" and not self._really_quit:
            event.ignore()
            self.hide()
            self._tray.showMessage(
                "Aegis Desk",
                "백그라운드에서 계속 실행 중입니다. 트레이 아이콘에서 종료할 수 있습니다.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            return
        self._shutdown()
        super().closeEvent(event)

    def _shutdown(self) -> None:
        """스케줄러·워커·트레이를 정리하고 프로세스를 반드시 끝낸다. 종료 시에만 호출된다
        (트레이 숨김 경로 제외).

        이전에는 이벤트 루프 종료를 Qt의 암묵 규칙(quitOnLastWindowClosed)에만 의존해,
        창이 트레이로 숨겨진 상태에서는 "보이는 창이 닫히는" 사건이 없어 app.exec()가
        영영 반환되지 않고 프로세스가 잔존했다(설계 ADR-0001). 이제 main()에서
        setQuitOnLastWindowClosed(False)로 암묵 종료를 끄고, 이 메서드 마지막의 최종
        판정(finalize_shutdown)이 창 가시성과 무관하게 항상 종료를 완수한다.

        워커가 COM Open 등에 블로킹돼 취소 플래그를 못 보는 경우(행오버) 정상 종료가
        안 되면 lifecycle.stop_worker가 스레드 강제 종료 → 프로세스 하드 종료로
        에스컬레이션한다. 앞 단계(스케줄러 정지·트레이 숨김·stop_worker) 중 하나가
        예외로 이탈해도 최종 판정에는 항상 도달한다 — quit() 또는 hard_exit() 중
        정확히 하나만 실행된다.
        """
        # 근접한 이중 종료 요청(트레이 [종료]와 X가 거의 동시 등)이 스케줄러·워커 정리를
        # 중복 실행하지 않도록 프로세스 수명 기준으로 1회만 수행한다.
        if self._shutdown_done:
            return
        self._shutdown_done = True

        # 각 단계를 독립 try로 감싸 한 곳의 실패가 이후 정리(특히 최종 판정)를
        # 건너뛰지 않게 한다.
        try:
            scheduler = getattr(self, "_idle_scheduler", None)
            if scheduler is not None:
                scheduler.stop()
        except Exception as exc:
            logger.warning("스케줄러 정리 중 예외: %s", exc)

        # 프로세스가 끝까지 못 죽더라도 트레이 아이콘은 먼저 치운다.
        try:
            if self._tray is not None:
                self._tray.hide()
        except Exception as exc:
            logger.warning("트레이 정리 중 예외: %s", exc)

        # 워커 종료 에스컬레이션(정상→강제→하드)은 lifecycle.stop_worker로 분리
        # (PyQt6 비의존이라 단위 테스트 가능). 스케줄러·트레이 정리는 위에서 이미 시도함.
        try:
            from knowmate.app.lifecycle import stop_worker
            stop_worker(getattr(self._bridge, "_worker", None))
        except Exception as exc:
            logger.warning("워커 정리 중 예외: %s", exc)

        # 최종 판정 — 항상 도달한다. stop_worker()가 정상 반환했다면 워커는 이미 멈췄지만,
        # stop_worker() 자체가 예외로 이탈했을 경우를 대비해 워커 실행 여부를 다시 조회한다.
        # 비실행 확인 시 quit(), 실행 중이거나 조회 자체가 실패(판정 불가)하면 보수적으로
        # hard_exit — quit과 hard_exit는 정확히 하나만 실행된다.
        from knowmate.app.lifecycle import finalize_shutdown
        finalize_shutdown(
            getattr(self._bridge, "_worker", None),
            quit_fn=QApplication.instance().quit,
        )


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


def _init_logging(log_level_name: str) -> None:
    """콘솔 + 파일(순환) 로깅을 초기화한다. 파일: %APPDATA%/AegisDesk/logs/aegisdesk.log."""
    from logging.handlers import RotatingFileHandler
    from knowmate.config import get_data_dir

    log_level = getattr(logging, log_level_name.upper(), logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    root = logging.getLogger()
    root.setLevel(log_level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        log_dir = get_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "aegisdesk.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        logging.getLogger(__name__).warning("파일 로그 초기화 실패: %s", exc)


def _install_exception_hook() -> None:
    """미처리 예외를 로그에 기록한다(콘솔이 없는 --windowed 빌드에서도 원인 추적 가능)."""

    def _hook(exc_type, exc_value, exc_tb) -> None:
        logging.getLogger("aegisdesk.uncaught").critical(
            "미처리 예외로 종료됨", exc_info=(exc_type, exc_value, exc_tb)
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def main() -> None:
    from knowmate.version import __version__
    from knowmate.config import get_config

    cfg = get_config()
    _init_logging(cfg.get("log_level", "INFO"))
    _install_exception_hook()

    logger.info("Aegis Desk %s 시작 (platform=%s)", __version__, sys.platform)

    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    _set_windows_app_id("AegisDesk.App")
    app = QApplication(sys.argv)
    app.setApplicationName("Aegis Desk")
    # 트레이 상주 앱 표준 관용구: 창이 트레이로 숨겨진 상태(hide())에서는 "보이는 창이
    # 닫히는" 사건 자체가 없어 기본값(True)으로는 lastWindowClosed가 오지 않아
    # app.exec()가 반환되지 않는다(종료 프로세스 잔존의 원인 — 설계 ADR-0001). 종료는
    # 전적으로 MainWindow._shutdown()의 명시적 quit()/hard_exit 판정에 맡긴다.
    app.setQuitOnLastWindowClosed(False)
    if APP_ICON.exists():
        app.setWindowIcon(QIcon(str(APP_ICON)))

    # 단일 인스턴스 보장 — 이미 실행 중이면 기존 창을 띄우도록 알리고 조용히 종료.
    # 트레이 상주 앱이라 중복 실행이 쉬운데, 두 인스턴스가 같은 LanceDB·state
    # 파일에 동시 쓰면 데이터 손상 위험이 있다(원칙8과 같은 이유).
    from knowmate.app.single_instance import (
        SingleInstanceServer, try_acquire_or_notify_existing,
    )
    if not try_acquire_or_notify_existing():
        return

    # 단일 인스턴스로 확정된 뒤에만 강제 종료 표식을 다룬다(설계 리뷰 12차 M-1) —
    # 그렇지 않으면 곧 조용히 종료할 보조 인스턴스가 주 인스턴스의 표식을 건드려
    # false-positive(또는 놓친) 감지를 유발할 수 있다. 이번 실행을 위한 표식은
    # 지금 남기고 정상 quit에서만 지워진다(11차 B-1). 반환값은 "직전 실행이
    # 표식을 못 지우고 끝났는가" = 강제 종료 여부. LanceDB 쓰기 도중이었을 가능성을
    # 배제할 수 없으므로(커밋 원자성 미검증, 10차 M-1) 자동 복구는 하지 않되, 로그 +
    # 트레이 알림(MainWindow 생성 후)으로 재인덱싱을 권장한다.
    from knowmate.app.lifecycle import check_and_remark_dirty_shutdown
    dirty_shutdown_detected = check_and_remark_dirty_shutdown()
    if dirty_shutdown_detected:
        logger.warning(
            "이전 실행이 강제 종료됐습니다 — 검색 결과가 이상하면 설정 패널에서 해당 "
            "폴더를 제거 후 재추가해 재인덱싱하는 것을 권장합니다."
        )

    win = MainWindow(dirty_shutdown_detected=dirty_shutdown_detected)
    single_instance_server = SingleInstanceServer(parent=win)
    single_instance_server.show_requested.connect(win._show_from_tray)

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
