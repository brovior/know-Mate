"""JS <-> Python QWebChannel 브리지."""
from __future__ import annotations

import json
import os
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal


class Bridge(QObject):
    """JS에서 호출하는 슬롯과 JS로 내보내는 시그널을 모두 여기에 둔다."""

    # Python -> JS
    responseReady = pyqtSignal(str)   # JSON 문자열
    indexProgress = pyqtSignal(str)   # 인덱싱 진행률 JSON
    indexFinished = pyqtSignal(str)   # 인덱싱 완료 메시지
    indexAlert = pyqtSignal(str)      # 대량삭제 차단 등 UI 알림

    def __init__(self, agent_registry=None, main_window=None, collector_worker=None, parent=None):
        super().__init__(parent)
        self._registry = agent_registry
        self._win = main_window
        self._worker = collector_worker
        self._last_indexed: str = ""
        self._doc_count: int = 0

    # ------------------------------------------------------------------
    # 에이전트 질의
    # ------------------------------------------------------------------

    @pyqtSlot(str)
    def sendQuery(self, payload: str) -> None:
        """JS가 호출하는 진입점. payload = JSON {"query": "...", "mode": "..."}"""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self._emit_error("invalid JSON payload")
            return

        query = data.get("query", "").strip()
        mode  = data.get("mode", "knowledge")

        if not query:
            self._emit_error("empty query")
            return

        if self._registry is None:
            blocks = [{"type": "text", "content": f"[echo] {query}"}]
        else:
            try:
                agent = self._registry.get(mode)
                blocks = agent.handle(query, {"mode": mode})
            except Exception as exc:
                self._emit_error(str(exc))
                return

        self.responseReady.emit(json.dumps({"blocks": blocks}, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 윈도우 컨트롤
    # ------------------------------------------------------------------

    @pyqtSlot()
    def minimizeWindow(self) -> None:
        """윈도우 최소화."""
        if self._win:
            self._win.showMinimized()

    @pyqtSlot()
    def maximizeWindow(self) -> None:
        """최대화 <-> 복원 토글."""
        if self._win:
            if self._win.isMaximized():
                self._win.showNormal()
            else:
                self._win.showMaximized()

    @pyqtSlot()
    def closeWindow(self) -> None:
        """윈도우 닫기."""
        if self._win:
            self._win.close()

    @pyqtSlot()
    def startWindowDrag(self) -> None:
        """OS 네이티브 드래그 이동 시작 (마우스 버튼 누른 상태에서 호출)."""
        if self._win:
            handle = self._win.windowHandle()
            if handle:
                handle.startSystemMove()

    @pyqtSlot(str, result=str)
    def openFile(self, path: str) -> str:
        """소스 카드 클릭 시 원본 파일 열기. 결과를 문자열로 반환."""
        import pathlib
        p = pathlib.Path(path)
        if p.exists():
            os.startfile(p)
            return "ok"
        return "not_found"

    # ------------------------------------------------------------------
    # 수집기 슬롯
    # ------------------------------------------------------------------

    @pyqtSlot()
    def startReindex(self) -> None:
        """증분 재인덱싱을 시작한다."""
        if self._worker is None:
            self.indexAlert.emit("수집기가 초기화되지 않았습니다.")
            return
        if self._worker.isRunning():
            self.indexAlert.emit("인덱싱이 이미 진행 중입니다.")
            return
        self._worker.start()

    @pyqtSlot()
    def cancelReindex(self) -> None:
        """진행 중인 재인덱싱을 취소한다."""
        if self._worker and self._worker.isRunning():
            self._worker.cancel()

    @pyqtSlot(result=str)
    def getIndexStatus(self) -> str:
        """현재 인덱싱 상태를 JSON으로 반환한다."""
        running = bool(self._worker and self._worker.isRunning())
        status = {
            "status": "running" if running else "idle",
            "last_indexed": self._last_indexed,
            "doc_count": self._doc_count,
        }
        return json.dumps(status, ensure_ascii=False)

    def set_worker(self, worker) -> None:
        """CollectorWorker를 연결하고 시그널을 바인딩한다."""
        self._worker = worker
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.indexing_needed.connect(self.indexAlert)

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _on_worker_progress(self, current: int, total: int, filename: str) -> None:
        """워커 진행률 시그널을 JSON으로 변환해 JS에 전달한다."""
        payload = json.dumps({"current": current, "total": total, "filename": filename}, ensure_ascii=False)
        self.indexProgress.emit(payload)

    def _on_worker_finished(self, message: str) -> None:
        """워커 완료 시그널 처리."""
        from datetime import datetime
        self._last_indexed = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.indexFinished.emit(message)

    def _emit_error(self, msg: str) -> None:
        err = [{"type": "text", "content": f"오류: {msg}"}]
        self.responseReady.emit(json.dumps({"blocks": err}, ensure_ascii=False))
