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
    def getVersion(self) -> str:
        """앱 버전 문자열을 반환한다."""
        from knowmate.version import __version__
        return __version__

    # ------------------------------------------------------------------
    # 설정 패널
    # ------------------------------------------------------------------

    @pyqtSlot(result=str)
    def getSettings(self) -> str:
        """설정 UI에 필요한 값만 추려 JSON으로 반환한다."""
        from knowmate.config import get_config
        from knowmate.rag.embedding import EMBEDDING_MODEL
        cfg = get_config()
        data = {
            "llm": {
                "base_url": cfg.get("llm", {}).get("base_url", ""),
                "model": cfg.get("llm", {}).get("model", ""),
            },
            "embedding": {
                "base_url": cfg.get("embedding", {}).get("base_url", ""),
                "model": EMBEDDING_MODEL,  # 읽기 전용 (코드 상수 — CLAUDE.md 원칙2)
            },
            "search": {
                "score_threshold": cfg.get("search", {}).get("score_threshold", 0.3),
                "top_k_max": cfg.get("search", {}).get("top_k_max", 10),
            },
            "collector": {
                "idle_enabled": cfg.get("collector", {}).get("idle_enabled", True),
                "idle_seconds": cfg.get("collector", {}).get("idle_seconds", 60),
            },
            "mail": {
                "enabled": cfg.get("mail", {}).get("enabled", True),
            },
            "chunking": {
                "max_file_size_mb": cfg.get("chunking", {}).get("max_file_size_mb", 30),
            },
            "ui": {
                "close_action": cfg.get("ui", {}).get("close_action", "tray"),
            },
            "log_level": cfg.get("log_level", "INFO"),
        }
        return json.dumps(data, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def saveSettings(self, payload: str) -> str:
        """설정 UI에서 받은 patch를 저장한다. 결과를 {"ok": bool, "error": str} JSON으로 반환."""
        try:
            patch = json.loads(payload)
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid JSON"})

        from knowmate.config import update_settings
        try:
            update_settings(patch)
            return json.dumps({"ok": True})
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("설정 저장 실패: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)})

    @pyqtSlot(result=str)
    def testConnection(self) -> str:
        """LLM·임베딩 서버 연결을 각각 테스트해 결과를 JSON으로 반환한다."""
        from knowmate.config import get_config
        from knowmate.rag.embedding import get_embedding_client
        from knowmate.llm.client import get_llm_client

        cfg = get_config()
        result: dict[str, dict] = {}

        try:
            llm = get_llm_client(cfg)
            llm.answer("연결 테스트", ["ping"])
            result["llm"] = {"ok": True, "detail": "정상 연결"}
        except Exception as exc:
            result["llm"] = {"ok": False, "detail": str(exc)}

        try:
            embed = get_embedding_client(cfg)
            embed.embed(["연결 테스트"])
            result["embedding"] = {"ok": True, "detail": "정상 연결"}
        except Exception as exc:
            result["embedding"] = {"ok": False, "detail": str(exc)}

        return json.dumps(result, ensure_ascii=False)

    @pyqtSlot(result=str)
    def openConfigFile(self) -> str:
        """config.yaml을 OS 기본 편집기로 연다."""
        from knowmate.config import get_data_dir
        path = get_data_dir() / "config.yaml"
        try:
            os.startfile(path)
            return "ok"
        except Exception as exc:
            return f"error: {exc}"

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

        mail_count = 0
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

        try:
            if self._worker and hasattr(self._worker, "_email_indexer") and self._worker._email_indexer:
                edf = self._worker._email_indexer.table.to_arrow().to_pandas()
                mail_count = int(edf[~edf["is_deleted"]]["mail_uid"].nunique())
        except Exception:
            pass

        status = {
            "status":        "running" if running else "idle",
            "last_indexed":  last_indexed,
            "doc_count":     self._doc_count,
            "local_count":   local_count,
            "shared_count":  shared_count,
            "mail_count":    mail_count,
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
        mail_count = 0
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

        try:
            if self._worker and hasattr(self._worker, "_email_indexer") and self._worker._email_indexer:
                edf = self._worker._email_indexer.table.to_arrow().to_pandas()
                mail_count = int(edf[~edf["is_deleted"]]["mail_uid"].nunique())
        except Exception:
            pass

        status = {
            "last_indexed":  self._last_indexed,
            "doc_count":     self._doc_count,
            "local_count":   local_count,
            "shared_count":  shared_count,
            "mail_count":    mail_count,
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
