"""JS <-> Python QWebChannel 브리지."""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal

logger = logging.getLogger(__name__)

# IFileOpenDialog 옵션 플래그 (shobjidl.h)
_FOS_PICKFOLDERS = 0x00000020       # 폴더 선택 모드
_FOS_FORCEFILESYSTEM = 0x00000040   # 파일시스템 경로만 허용
_FOS_PATHMUSTEXIST = 0x00000800     # 존재하는 경로만
_FOS_NOTESTFILECREATE = 0x00010000  # 쓰기 테스트 생략 → 읽기전용/네트워크 드라이브 지원


def _native_pick_folder_win(hwnd: int = 0) -> str:
    """Windows IFileOpenDialog로 폴더를 선택해 경로를 반환한다 (취소 시 빈 문자열).

    FOS_NOTESTFILECREATE로 쓰기 테스트를 건너뛰어, 읽기전용·네트워크 매핑
    드라이브(예: M:\\)에서 발생하는 'ERROR_WRITE_PROTECT'(쓰기 방지된 미디어) 오류를
    피한다. 외형은 표준 모던 폴더 picker와 동일하다.
    """
    import pythoncom
    from win32com.shell import shell, shellcon

    dialog = pythoncom.CoCreateInstance(
        shell.CLSID_FileOpenDialog,
        None,
        pythoncom.CLSCTX_INPROC_SERVER,
        shell.IID_IFileOpenDialog,
    )
    opts = dialog.GetOptions()
    dialog.SetOptions(
        opts | _FOS_PICKFOLDERS | _FOS_FORCEFILESYSTEM
        | _FOS_PATHMUSTEXIST | _FOS_NOTESTFILECREATE
    )
    dialog.SetTitle("폴더 선택")
    try:
        dialog.Show(hwnd)
    except pythoncom.com_error:
        return ""  # 사용자 취소(ERROR_CANCELLED)
    item = dialog.GetResult()
    return item.GetDisplayName(shellcon.SIGDN_FILESYSPATH) or ""


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
        """네이티브 폴더 선택 다이얼로그를 열고 선택된 경로를 반환한다. 취소 시 빈 문자열.

        Windows에서는 IFileOpenDialog(FOS_NOTESTFILECREATE)를 직접 사용해
        읽기전용·네트워크 매핑 드라이브에서도 정상 동작하게 한다. win32가 아니거나
        실패하면 Qt 기본 다이얼로그로 폴백한다.
        """
        if sys.platform == "win32":
            try:
                hwnd = int(self._win.winId()) if self._win else 0
                return _native_pick_folder_win(hwnd)
            except Exception as exc:  # noqa: BLE001 — 폴백이 있으므로 치명적이지 않음
                logger.warning("네이티브 폴더 선택 실패, Qt 다이얼로그로 폴백: %s", exc)

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
    def addFolderByPath(self, raw_path: str) -> str:
        """사용자가 직접 입력한 경로를 검증 후 watch_folders에 추가한다.

        네트워크·DMS 드라이브(M:\\ 등)는 폴더 선택 다이얼로그가 셸 탐색에 실패하므로,
        경로를 직접 입력받아 우회한다. 인덱싱은 os.walk로 정상 동작한다.

        반환 JSON: {"ok": bool, "folders": [...], "error": str}
        경로가 비었거나 존재하지 않거나 폴더가 아니면 ok=false + error 메시지.
        """
        from knowmate.config import get_config
        current = get_config().get("collector", {}).get("watch_folders", [])

        def _fail(msg: str) -> str:
            return json.dumps({"ok": False, "folders": current, "error": msg}, ensure_ascii=False)

        raw = (raw_path or "").strip().strip('"').strip("'")
        if not raw:
            return _fail("경로를 입력하세요.")
        try:
            p = Path(raw)
            if not p.exists():
                return _fail(f"경로를 찾을 수 없습니다: {raw}")
            if not p.is_dir():
                return _fail(f"폴더가 아닙니다: {raw}")
        except OSError as exc:
            return _fail(f"경로 접근 실패: {exc}")

        folders = json.loads(self.addWatchFolder(raw))
        return json.dumps({"ok": True, "folders": folders, "error": ""}, ensure_ascii=False)

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
