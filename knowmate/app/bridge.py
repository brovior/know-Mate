"""JS <-> Python QWebChannel 브리지."""
from __future__ import annotations

import json
import os
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal


class Bridge(QObject):
    """JS에서 호출하는 슬롯과 JS로 내보내는 시그널을 모두 여기에 둔다."""

    # Python -> JS
    responseReady  = pyqtSignal(str)  # JSON 문자열
    indexProgress  = pyqtSignal(str)  # 인덱싱 진행률 JSON
    indexFinished  = pyqtSignal(str)  # 인덱싱 완료 메시지
    indexAlert     = pyqtSignal(str)  # 대량삭제 차단 등 UI 알림
    statusUpdated  = pyqtSignal(str)  # 인덱싱 완료 후 건수 현황 JSON

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

        query  = data.get("query", "").strip()
        mode   = data.get("mode", "knowledge")
        scopes = data.get("scopes", [])

        if not query:
            self._emit_error("empty query")
            return

        if self._registry is None:
            blocks = [{"type": "text", "content": f"[echo] {query}"}]
        else:
            try:
                agent = self._registry.get(mode)
                blocks = agent.handle(query, {"mode": mode, "scopes": scopes})
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

    @pyqtSlot(result=str)
    def selectFolder(self) -> str:
        """네이티브 폴더 선택 다이얼로그를 열고 선택된 경로를 반환한다. 취소 시 빈 문자열."""
        from PyQt6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self._win, "폴더 선택")
        return path or ""

    @pyqtSlot(result=str)
    def getFolders(self) -> str:
        """현재 watch_folders 목록을 JSON 배열로 반환한다."""
        from knowmate.config import get_config
        folders = get_config().get("collector", {}).get("watch_folders", [])
        return json.dumps(folders, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def addWatchFolder(self, path: str) -> str:
        """폴더를 watch_folders에 추가하고 갱신된 목록을 JSON으로 반환한다."""
        from knowmate.config import get_config, update_watch_folders
        folders: list[str] = get_config().get("collector", {}).get("watch_folders", [])
        normalized = path.replace("\\", "/")
        if normalized not in folders:
            folders.append(normalized)
            update_watch_folders(folders)
        return json.dumps(folders, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def removeWatchFolder(self, path: str) -> str:
        """폴더를 watch_folders에서 제거하고 갱신된 목록을 JSON으로 반환한다."""
        from knowmate.config import get_config, update_watch_folders
        folders: list[str] = get_config().get("collector", {}).get("watch_folders", [])
        normalized = path.replace("\\", "/")
        folders = [f for f in folders if f != normalized]
        update_watch_folders(folders)
        return json.dumps(folders, ensure_ascii=False)

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
        """현재 인덱싱 상태를 JSON으로 반환한다. LanceDB를 직접 조회해 실제 건수를 반환."""
        running = bool(self._worker and self._worker.isRunning())
        local_count = 0
        shared_count = 0
        last_indexed = self._last_indexed

        try:
            if self._worker and hasattr(self._worker, "_indexer"):
                df = self._worker._indexer.table.to_arrow().to_pandas()
                active = df[~df["is_deleted"]]
                # 문서 수 = 고유 file_path 개수 (청크 행 수가 아님)
                local_count  = int(active.loc[active["scope"] == "local", "file_path"].nunique())
                shared_count = int(active.loc[active["scope"] == "shared", "file_path"].nunique())
                self._doc_count = local_count + shared_count
                # DB에서 가장 최근 인덱싱 시각 조회 (UTC → 로컬 시간 변환)
                if not active.empty and "indexed_at" in active.columns:
                    raw_ts = active["indexed_at"].max()
                    if raw_ts:
                        from datetime import datetime
                        dt = datetime.fromisoformat(str(raw_ts))
                        last_indexed = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

        status = {
            "status":        "running" if running else "idle",
            "last_indexed":  last_indexed,
            "doc_count":     self._doc_count,
            "local_count":   local_count,
            "shared_count":  shared_count,
        }
        return json.dumps(status, ensure_ascii=False)

    def set_worker(self, worker) -> None:
        """단일 수집기 워커를 등록하고 시그널을 바인딩한다 (수동·유휴 인덱싱 공유)."""
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

        local_count = 0
        shared_count = 0
        try:
            if self._worker and hasattr(self._worker, "_indexer"):
                df = self._worker._indexer.table.to_arrow().to_pandas()
                active = df[~df["is_deleted"]]
                # 문서 수 = 고유 file_path 개수 (청크 행 수가 아님)
                local_count  = int(active.loc[active["scope"] == "local", "file_path"].nunique())
                shared_count = int(active.loc[active["scope"] == "shared", "file_path"].nunique())
                self._doc_count = local_count + shared_count
        except Exception:
            pass

        status = {
            "last_indexed":  self._last_indexed,
            "doc_count":     self._doc_count,
            "local_count":   local_count,
            "shared_count":  shared_count,
        }
        self.statusUpdated.emit(json.dumps(status, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 대화 스레드 (6-12)
    # ------------------------------------------------------------------

    @pyqtSlot(str, result=str)
    def getThreads(self, mode: str) -> str:
        """mode의 스레드 목록을 JSON 배열로 반환한다."""
        from knowmate.app.threads import load_threads
        data = load_threads()
        return json.dumps(data.get(mode, []), ensure_ascii=False)

    @pyqtSlot(str, str)
    def saveThread(self, mode: str, thread_json: str) -> None:
        """스레드를 저장한다. id 기준 upsert."""
        from knowmate.app.threads import upsert_thread
        try:
            thread = json.loads(thread_json)
            upsert_thread(mode, thread)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("스레드 저장 실패: %s", exc)

    @pyqtSlot(str, str)
    def deleteThread(self, mode: str, thread_id: str) -> None:
        """스레드를 삭제한다."""
        from knowmate.app.threads import delete_thread
        delete_thread(mode, thread_id)

    def _emit_error(self, msg: str) -> None:
        err = [{"type": "text", "content": f"오류: {msg}"}]
        self.responseReady.emit(json.dumps({"blocks": err}, ensure_ascii=False))
