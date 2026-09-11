"""메인 화면이 준비되는 동안 표시하는 가벼운 시작 화면."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QProgressBar, QVBoxLayout


class StartupSplash(QFrame):
    """앱 로고와 무한 진행 표시만 보여주는 시작 화면이다."""

    def __init__(self, logo_path: Path) -> None:
        super().__init__(
            None,
            Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint,
        )
        self.setObjectName("startupSplash")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(360, 270)
        self.setStyleSheet(
            """
            QFrame#startupSplash {
                background: #FFFFFF;
                border: 1px solid #D5DAE1;
                border-radius: 18px;
            }
            QLabel#startupTitle {
                color: #1F2937;
                font-family: "Malgun Gothic";
                font-size: 20px;
                font-weight: 700;
            }
            QLabel#startupStatus {
                color: #6B7280;
                font-family: "Malgun Gothic";
                font-size: 12px;
            }
            QProgressBar {
                min-height: 6px;
                max-height: 6px;
                border: none;
                border-radius: 3px;
                background: #E8EEF6;
            }
            QProgressBar::chunk {
                border-radius: 3px;
                background: #26BFAF;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(44, 28, 44, 28)
        layout.setSpacing(10)

        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(str(logo_path))
        if not pixmap.isNull():
            logo.setPixmap(
                pixmap.scaled(
                    128,
                    128,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        layout.addWidget(logo)

        title = QLabel("Aegis Desk")
        title.setObjectName("startupTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        status = QLabel("시작하는 중...")
        status.setObjectName("startupStatus")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(status)

        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        layout.addWidget(progress)

        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.center() - self.rect().center())
