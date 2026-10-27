"""JS <-> Python QWebChannel 브리지."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from PyQt6.QtCore import QObject, QTimer, pyqtSlot, pyqtSignal


class Bridge(QObject):
    """JS에서 호출하는 슬롯과 JS로 내보내는 시그널을 모두 여기에 둔다."""

    # Python -> JS
    queryStateChanged = pyqtSignal(str)
    responseReady  = pyqtSignal(str)  # JSON 문자열
    indexProgress  = pyqtSignal(str)  # 인덱싱 진행률 JSON
    indexFinished  = pyqtSignal(str)  # 인덱싱 완료 메시지
    indexAlert     = pyqtSignal(str)  # 대량삭제 차단 등 UI 알림
    statusUpdated  = pyqtSignal(str)  # 인덱싱 완료 후 건수 현황 JSON
    failuresChanged = pyqtSignal()  # coalesced failure metadata revision
    failurePolicyJobChanged = pyqtSignal(str)

    def __init__(self, main_window=None, collector_worker=None, parent=None, *,
                 query_config: dict | None = None, query_data_dir: Path | None = None,
                 query_worker_factory=None):
        super().__init__(parent)
        from knowmate.app.query_controller import QueryController
        kwargs = {} if query_worker_factory is None else {"worker_factory": query_worker_factory}
        self._query = QueryController(self, **kwargs)
        self._query.responseReady.connect(self.responseReady)
        self._query.stateChanged.connect(self.queryStateChanged)
        self._query_config = query_config
        self._query_data_dir = query_data_dir
        if query_config is not None:
            from knowmate.config import query_timeout_ms
            query_timeout_ms(query_config)  # Warm bundled defaults before interactive queries.
        self._win = main_window
        self._worker = collector_worker
        self._last_indexed: str = ""
        self._doc_count: int = 0
        self._local_count: int = 0
        self._shared_count: int = 0
        self._mail_count: int = 0
        from knowmate.app.failure_policy_controller import FailurePolicyController
        self._policy = FailurePolicyController()
        self._config_view: dict = {}
        self._failure_records_cache: dict | None = None
        self._policy_notify_pending = False
        self._policy_last_notify = float("-inf")
        from knowmate.app.failure_view import FailureView
        self._failure_view = FailureView()
        self._failure_notify_pending = False
        self._failure_last_notify = float("-inf")
        self._failure_timer = QTimer(self)
        self._failure_timer.setInterval(500)
        self._failure_timer.timeout.connect(self._poll_failure_updates)
        self._failure_timer.start()

    def _poll_failure_updates(self) -> None:
        """Publish one coalesced update and finalize only after native thread exit."""
        if self._worker is None:
            return
        self._poll_policy_updates()
        if self._policy.busy:
            return
        changed, terminal = self._failure_view.poll(self._worker)
        if changed:
            self._failure_notify_pending = True
        now = time.monotonic()
        if self._failure_notify_pending and (terminal is not None or now - self._failure_last_notify >= 0.5):
            self._failure_notify_pending = False
            self._failure_last_notify = now
            self.failuresChanged.emit()
        if terminal is not None:
            if not terminal.get("persisted", True):
                self.indexAlert.emit("실패 이력을 저장하지 못했습니다. 저장 후 다시 시도해 주세요.")
            self._finish_worker_status(terminal.get("message", "인덱싱이 종료되었습니다."),
                                       update_time=terminal.get("scope") == "all")

    def _worker_write_guard(self) -> str | None:
        """Reject overlapping writes and schedule a persistence-only retry when needed."""
        if self._policy.blocked:
            return "policy_busy"
        self._poll_failure_updates()
        if self._policy.blocked:
            return "policy_busy"
        if self._worker is not None and self._worker.isRunning():
            return "busy"
        if self._failure_view.active:
            return "busy"
        if self._worker is not None and getattr(self._worker, "failure_persistence_pending", False):
            reserve = getattr(self._worker, "request_failure_persist", None)
            if reserve is not None and reserve():
                self._worker.start()
            return "persistence_pending"
        return None

    @property
    def policy_shutdown_active(self) -> bool:
        """Expose the GUI lease classification through actual native join."""
        return self._policy.busy

    def _publish_policy_state(self, *, force: bool = False) -> None:
        """Send at most two metadata updates per second plus terminal delivery."""
        now = time.monotonic()
        if force or (self._policy_notify_pending and now - self._policy_last_notify >= 0.5):
            self._policy_notify_pending = False
            self._policy_last_notify = now
            self.failurePolicyJobChanged.emit(self.getFailurePolicyJobStatus())

    def _poll_policy_updates(self) -> None:
        """Apply background policy results without querying the database."""
        changed, terminal = self._policy.poll()
        records = self._policy.take_records()
        if records is not None:
            self._failure_records_cache = records
            self._failure_view.records = records
        if changed:
            self._policy_notify_pending = True
        if terminal is not None:
            counts = terminal.get("counts")
            if isinstance(counts, dict):
                self._local_count = counts.get("local_count", self._local_count)
                self._shared_count = counts.get("shared_count", self._shared_count)
                self._mail_count = counts.get("mail_count", self._mail_count)
                self._doc_count = counts.get("doc_count", self._local_count + self._shared_count)
            self.statusUpdated.emit(json.dumps(self._cached_index_status(), ensure_ascii=False))
            self.failuresChanged.emit()
        self._publish_policy_state(force=terminal is not None)

    def _read_config_view(self) -> dict:
        """Use only warmed shared settings while a policy owns storage access."""
        from knowmate.config import get_config, get_cached_config
        if self._policy.cache_only:
            return get_cached_config() or self._config_view
        self._config_view = get_config()
        return self._config_view

    def _cached_index_status(self) -> dict:
        """Return scalar counts without database projection on the GUI thread."""
        return dict(status="policy_running" if self._policy.busy else "idle",
                    last_indexed=self._last_indexed, doc_count=self._doc_count,
                    local_count=self._local_count, shared_count=self._shared_count,
                    mail_count=self._mail_count)

    # ------------------------------------------------------------------
    # 에이전트 질의
    # ------------------------------------------------------------------

    @pyqtSlot(str)
    def sendQuery(self, payload: str) -> None:
        """Validate and submit a request without running search on the GUI thread."""
        data: dict = {}
        try:
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                raise ValueError("object required")
            data = parsed
            if not isinstance(data.get("query"), str) or not data["query"].strip():
                raise ValueError("query required")
            if data.get("mode") not in {"knowledge", "mes"}:
                raise ValueError("unsupported mode")
            if any(not isinstance(data.get(key), str) or not data[key].strip()
                   for key in ("request_id", "thread_id")):
                raise ValueError("identifiers required")
            generation = data.get("view_generation")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                raise ValueError("invalid generation")
            scopes = data.get("scopes")
            if (not isinstance(scopes, list) or
                    any(not isinstance(scope, str) or scope not in {"local", "shared", "mail"}
                        for scope in scopes)):
                raise ValueError("invalid scopes")
            if data["mode"] == "knowledge" and not scopes:
                self._query.reject(data, "검색 범위를 하나 이상 선택해 주세요.")
                return
        except (ValueError, TypeError):
            self._query.reject(data, "질문 요청 형식이 올바르지 않습니다.")
            return
        if self._policy.blocked:
            self._query.reject(data, "문서 제외 작업이 진행 중입니다. 완료 후 다시 시도해 주세요.")
            return
        if self._query.busy or self._query.closing:
            self._query.reject(data, "이전 질문 처리가 끝난 뒤 다시 시도해 주세요.")
            return
        try:
            from knowmate.config import get_config, get_data_dir, query_timeout_ms
            config = self._query_config if self._query_config is not None else get_config()
            data_dir = self._query_data_dir if self._query_data_dir is not None else get_data_dir()
            self._query.submit(data, config, data_dir, query_timeout_ms(config))
        except Exception as exc:
            logging.getLogger(__name__).warning("[query] admission failed: error_type=%s", type(exc).__name__)
            self._query.reject(data, "질문을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.")

    @pyqtSlot(result=str)
    def getQueryState(self) -> str:
        """Return the current execution lease and its timeout budget."""
        return json.dumps(self._query.state(), ensure_ascii=False)

    @pyqtSlot(str)
    def cancelQuery(self, request_id: str) -> None:
        """Retire a matching query without allowing another worker to overlap."""
        self._query.cancel(request_id)

    def begin_query_shutdown(self) -> None:
        """Seal admission and suppress late UI deliveries on actual app exit."""
        self._policy.begin_shutdown()
        self._query.begin_shutdown()

    def wait_query_shutdown(self, timeout_ms: int = 8000) -> bool:
        """Confirm actual query termination within the application-exit grace."""
        return self._query.wait_for_shutdown(timeout_ms)

    def _query_policy_busy(self) -> bool:
        return self._query.busy or self._query.closing

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
        """앱 버전과 빌드 커밋 식별자를 반환한다."""
        from knowmate.version import get_version_label
        return get_version_label()

    # ------------------------------------------------------------------
    # 설정 패널
    # ------------------------------------------------------------------

    @pyqtSlot(result=str)
    def getSettings(self) -> str:
        """설정 UI에 필요한 값만 추려 JSON으로 반환한다."""
        from knowmate.rag.embedding import EMBEDDING_MODEL
        cfg = self._read_config_view()
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
            "cleanup": {
                # dry_run(부정형) 대신 auto_delete(긍정형)로 노출 — 사용자 혼동 방지.
                # dry_run=true(기본, 안전) -> auto_delete=false
                "auto_delete": not cfg.get("cleanup", {}).get("dry_run", True),
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
        if self._policy.blocked:
            return json.dumps({"ok": False, "error": "문서 제외 작업이 진행 중입니다. 완료 후 다시 시도해 주세요.", "code": "policy_busy"})
        if self._query_policy_busy():
            return json.dumps({"ok": False, "error": "질문 처리가 끝난 뒤 다시 시도해 주세요.", "code": "query_busy"})
        try:
            patch = json.loads(payload)
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid JSON"})

        if not isinstance(patch, dict):
            return json.dumps({"ok": False, "error": "invalid settings"})
        collector = patch.get("collector")
        if isinstance(collector, dict) and "idle_seconds" in collector:
            idle_seconds = collector["idle_seconds"]
            if (
                isinstance(idle_seconds, bool)
                or not isinstance(idle_seconds, int)
                or idle_seconds not in {minutes * 60 for minutes in range(30, 181, 30)}
            ):
                return json.dumps({"ok": False, "error": "자동 인덱싱 시간은 30·60·90·120·150·180분 중 선택하세요."})

        # cleanup.auto_delete(UI 긍정형) -> cleanup.dry_run(실제 config 키, 부정형) 변환
        if "cleanup" in patch and isinstance(patch["cleanup"], dict) and "auto_delete" in patch["cleanup"]:
            auto_delete = patch["cleanup"].pop("auto_delete")
            patch["cleanup"]["dry_run"] = not auto_delete

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
        if self._policy.blocked:
            detail = "문서 제외 작업이 진행 중입니다. 완료 후 다시 시도해 주세요."
            return json.dumps({"llm": {"ok": False, "detail": detail},
                               "embedding": {"ok": False, "detail": detail}})
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
        if self._policy.blocked:
            return "policy_busy"
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
        folders = self._read_config_view().get("collector", {}).get("watch_folders", [])
        return json.dumps(folders, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def addWatchFolder(self, path: str) -> str:
        """폴더를 watch_folders에 추가하고 갱신된 목록을 JSON으로 반환한다."""
        if self._policy.blocked:
            self.indexAlert.emit("문서 제외 작업이 진행 중입니다. 완료 후 다시 시도해 주세요.")
            return self.getFolders()
        if self._query_policy_busy():
            self.indexAlert.emit("질문 처리가 끝난 뒤 다시 시도해 주세요.")
            return self.getFolders()
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
        if self._policy.blocked:
            self.indexAlert.emit("문서 제외 작업이 진행 중입니다. 완료 후 다시 시도해 주세요.")
            return self.getFolders()
        if self._query_policy_busy():
            self.indexAlert.emit("질문 처리가 끝난 뒤 다시 시도해 주세요.")
            return self.getFolders()
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

    @pyqtSlot(str, result=str)
    def revealFile(self, path: str) -> str:
        """[확인 필요한 문서] 화면의 「파일 위치 열기」— 탐색기에서 해당 파일을
        선택된 상태로 연다(파일 자체를 실행하지 않는다). 결과를 문자열로 반환."""
        import pathlib
        import subprocess
        p = pathlib.Path(path)
        if not p.exists():
            return "not_found"
        subprocess.Popen(["explorer", "/select,", str(p)])
        return "ok"

    # ------------------------------------------------------------------
    # 수집기 슬롯
    # ------------------------------------------------------------------

    @pyqtSlot(result=str)
    def startReindex(self) -> str:
        """증분 재인덱싱을 시작한다."""
        gate = getattr(self._win, "_request_index_start", None)
        if self._worker is None and gate is None:
            self.indexAlert.emit("수집기가 초기화되지 않았습니다.")
            return "error"
        blocked = self._worker_write_guard()
        if blocked:
            message = {"persistence_pending": "실패 이력을 저장하지 못했습니다. 저장 후 다시 시도해 주세요.",
                       "policy_busy": "문서 제외 작업이 진행 중입니다. 완료 후 다시 시도해 주세요."}
            self.indexAlert.emit(message.get(blocked, "인덱싱이 이미 진행 중입니다."))
            return blocked
        if gate is not None:
            return gate(retry_failures=True) or "ok"
        else:
            if hasattr(self._worker, "request_failure_retry"):
                self._worker.request_failure_retry()
            self._worker.start()
        return "ok"

    @pyqtSlot()
    def cancelReindex(self) -> None:
        """진행 중인 재인덱싱을 취소한다."""
        if self._policy.blocked:
            self.indexAlert.emit("문서 제외 작업이 진행 중입니다. 완료 후 다시 시도해 주세요.")
            return
        preparation = getattr(self._win, "_ppt_preparation", None)
        queued = preparation is not None and preparation.start_pending
        if preparation is not None:
            preparation.cancel_start()
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        elif queued:
            self.indexFinished.emit("인덱싱 시작 요청을 취소했습니다.")

    @pyqtSlot(result=str)
    def getIndexStatus(self) -> str:
        """현재 인덱싱 상태를 JSON으로 반환한다. LanceDB를 직접 조회해 실제 건수를 반환."""
        if self._policy.cache_only:
            return json.dumps(self._cached_index_status(), ensure_ascii=False)
        running = bool(self._worker and self._worker.isRunning())
        local_count, shared_count, mail_count, last_indexed = self._compute_doc_mail_counts(
            want_last_indexed=True
        )

        status = {
            "status":        "running" if running else "idle",
            "last_indexed":  last_indexed,
            "doc_count":     self._doc_count,
            "local_count":   local_count,
            "shared_count":  shared_count,
            "mail_count":    mail_count,
        }
        return json.dumps(status, ensure_ascii=False)

    def _compute_doc_mail_counts(self, want_last_indexed: bool = False) -> tuple[int, int, int, str]:
        """문서·메일 건수를 벡터·암호문 없이 필요한 컬럼만 projection 조회해 계산한다.

        이전에는 `table.to_arrow().to_pandas()`로 chunks·emails 테이블 **전체**
        (1024차원 벡터·AES 암호화 원문 포함)를 로드했다 — 유휴 자동 인덱싱이 60초마다
        도는 동안 변경 파일이 0건이어도 매번 호출돼, 상주 메모리가 유휴 방치 중에도
        계속 쌓이는 원인이었다(purge 경량화에서 고친 것과 동일한 안티패턴이 다른
        위치에 있었음). 필요한 건 고유 file_path/mail_uid 개수와 최근 인덱싱 시각뿐이라
        `search().select([...])`로 projection한다(purge 경량화에서 실측 검증된 방식과 동일).

        want_last_indexed: True면 chunks 테이블에서 최근 인덱싱 시각도 함께 계산한다
            (getIndexStatus는 필요, 완료 콜백은 datetime.now()를 쓰므로 불필요).
        반환: (local_count, shared_count, mail_count, last_indexed) — 조회 실패 시
            직전에 계산된 값으로 폴백한다(일시적 조회 실패로 화면이 0으로 튀지 않도록).
        """
        local_count = self._local_count
        shared_count = self._shared_count
        mail_count = self._mail_count
        last_indexed = self._last_indexed

        try:
            if self._worker and hasattr(self._worker, "_indexer"):
                cols = ["file_path", "scope", "is_deleted"]
                if want_last_indexed:
                    cols.append("indexed_at")
                tbl = self._worker._indexer.table.search().select(cols).to_arrow()
                df = tbl.to_pandas()
                active = df[~df["is_deleted"]]
                # 문서 수 = 고유 file_path 개수 (청크 행 수가 아님)
                local_count  = int(active.loc[active["scope"] == "local", "file_path"].nunique())
                shared_count = int(active.loc[active["scope"] == "shared", "file_path"].nunique())
                self._doc_count = local_count + shared_count
                if want_last_indexed and not active.empty and "indexed_at" in active.columns:
                    raw_ts = active["indexed_at"].max()
                    if raw_ts:
                        from datetime import datetime
                        dt = datetime.fromisoformat(str(raw_ts))
                        last_indexed = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "문서 건수 조회 실패: error_type=%s", type(exc).__name__
            )

        try:
            if self._worker and hasattr(self._worker, "_email_indexer") and self._worker._email_indexer:
                etbl = self._worker._email_indexer.table.search().select(["mail_uid", "is_deleted"]).to_arrow()
                edf = etbl.to_pandas()
                mail_count = int(edf[~edf["is_deleted"]]["mail_uid"].nunique())
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "메일 건수 조회 실패: error_type=%s", type(exc).__name__
            )

        self._local_count, self._shared_count, self._mail_count = local_count, shared_count, mail_count
        return local_count, shared_count, mail_count, last_indexed

    # ------------------------------------------------------------------
    # 실패 파일 관리 (5차: [확인 필요한 문서] 화면)
    # ------------------------------------------------------------------

    _FAIL_KIND_BADGE = {
        "TEMPORARY_BUSY":       ("사용 중", "b-info"),
        "OPEN_TIMEOUT":         ("열기 시간초과", "b-warn"),
        "READ_TIMEOUT":         ("읽기 시간초과", "b-warn"),
        "OPEN_ERROR":           ("열기 오류", "b-warn"),
        "READ_ERROR":           ("읽기 오류", "b-warn"),
        "NEEDS_USER_ACTION":    ("조치 필요", "b-action"),
        "FILE_CHANGED":         ("변경 감지", "b-gray"),
        "UNKNOWN_TRANSIENT":    ("원인 미확인", "b-gray"),
    }
    _FAIL_STAGE_LABEL = {
        "parse": "메일 파싱",
        "dispatch": "오피스 실행",
        "open": "파일 열기",
        "sheets": "데이터 읽기",
        "cell_read": "데이터 읽기",
        "read": "본문 읽기",
    }

    @pyqtSlot(result=str)
    def getFailures(self) -> str:
        """실패 이력 + 인덱싱 제외 목록을 병합해 카드 JSON 배열로 반환한다.

        시각·잔여시간 계산은 클라이언트(JS)가 epoch 초를 받아 로컬 시각 기준으로
        표시한다 — 표시 형식(오늘/어제/N일 전 등)은 UI 관심사라 여기선 원시값만 준다.
        """
        from knowmate.collector import failure_state
        from knowmate.config import get_data_dir
        from knowmate.collector.scanner import normalize_path_key

        collector_cfg = self._read_config_view().get("collector", {})
        excluded_raw = collector_cfg.get("exclude_files", [])
        excluded_keys = {normalize_path_key(p) for p in excluded_raw if isinstance(p, str)}

        records = self._failure_view.records
        if records is None:
            if self._policy.cache_only:
                records = self._failure_records_cache or {}
            else:
                failure_file = getattr(self._worker, "_failure_file", None) or (get_data_dir() / "index_failure.json")
                records = failure_state.load_failures(failure_file)
        self._failure_records_cache = records
        policy = failure_state.BackoffPolicy.from_config(collector_cfg)
        now = self._get_now()

        cards = []
        for path, rec in records.items():
            badge_label, badge_class = self._FAIL_KIND_BADGE.get(rec.kind, ("원인 미확인", "b-gray"))
            stage_label = self._FAIL_STAGE_LABEL.get(rec.stage or "", "-")
            is_excluded = normalize_path_key(path) in excluded_keys
            next_retry_ts = None
            if not is_excluded and not rec.force_retry:
                wait = failure_state.backoff_seconds(rec, path, policy)
                next_retry_ts = rec.last_failed_ts + wait
            cards.append({
                "path": path,
                "name": os.path.basename(path),
                "dir": os.path.dirname(path),
                "kind": rec.kind,
                "badge_label": badge_label,
                "badge_class": badge_class,
                "stage_label": stage_label,
                "consecutive_failures": rec.consecutive_failures,
                "last_failed_ts": rec.last_failed_ts,
                "next_retry_ts": next_retry_ts,
                "excluded": is_excluded,
                # 6a: "반복 중"/"조치 필요"는 kind와 독립적인 단일 판정
                # (failure_state.escalation_state) — UI·백오프가 같은 함수를
                # 공유해 판정이 갈리지 않는다(2차 리뷰 M-2).
                "escalation": failure_state.escalation_state(rec, policy),
            })
        # 제외됐지만 실패 기록이 이미 지워진 파일(오래 전 실패 후 자연 정리)도
        # 목록에서 사라지면 안 되므로 exclude_files에만 있는 경로도 카드로 만든다.
        seen_keys = {normalize_path_key(p) for p in records}
        for raw in excluded_raw:
            if not isinstance(raw, str) or normalize_path_key(raw) in seen_keys:
                continue
            cards.append({
                "path": raw, "name": os.path.basename(raw), "dir": os.path.dirname(raw),
                "kind": "UNKNOWN_TRANSIENT", "badge_label": "원인 미확인", "badge_class": "b-gray",
                "stage_label": "-", "consecutive_failures": 0, "last_failed_ts": None,
                "next_retry_ts": None, "excluded": True, "escalation": "NORMAL",
            })
        cards.sort(key=lambda c: (c["excluded"], -(c["last_failed_ts"] or 0)))
        return json.dumps(cards, ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def retryFile(self, path: str) -> str:
        """파일 1건에 대해 「지금 다시 시도」— 이번 사이클만 백오프를 무시하게
        force_retry를 세우고 재인덱싱 사이클을 시작한다. 이력은 보존된다."""
        gate = getattr(self._win, "_request_index_start", None)
        if self._worker is None and (
            gate is None or getattr(self._win, "_ppt_preparation", None) is None
        ):
            self.indexAlert.emit("수집기가 초기화되지 않았습니다.")
            return "error"
        if not isinstance(path, str) or not path.strip():
            return "error"
        blocked = self._worker_write_guard()
        if blocked:
            return blocked
        if gate is not None:
            return gate(target_path=path) or "ok"
        reserve = getattr(self._worker, "request_single_retry", None)
        if reserve is None:
            return "error"
        result = reserve(path)
        if result == "ok":
            self._worker.start()
        return result

    @pyqtSlot(str, str, result=str)
    def startFailurePolicyJob(self, action: str, payload: str) -> str:
        """Accept one exclusion command without GUI-thread storage work."""
        if action not in {"exclude", "unexclude", "reconcile"}:
            return json.dumps({"ok": False, "accepted": False, "error": "invalid_action"})
        paths, error = self._parse_failure_paths(payload)
        if error:
            return json.dumps({"ok": False, "accepted": False, "error": error})
        if action == "reconcile" and paths:
            return json.dumps({"ok": False, "accepted": False, "error": "invalid_paths"})
        if self._query.busy or self._query.closing:
            return json.dumps({"ok": False, "accepted": False, "error": "query_busy"})
        if self._policy.busy or (self._policy.blocked and action != "reconcile"):
            return json.dumps({"ok": False, "accepted": False, "error": "policy_busy"})
        preparation = getattr(self._win, "_ppt_preparation", None)
        if preparation is not None and preparation.start_pending:
            return json.dumps({"ok": False, "accepted": False, "error": "busy"})
        if self._worker is None:
            return json.dumps({"ok": False, "accepted": False, "error": "not_ready"})
        unseen_cycle = getattr(self._worker, "failure_cycle_id", 0) != self._failure_view.cycle_id
        if self._worker.isRunning() or self._failure_view.active or unseen_cycle:
            return json.dumps({"ok": False, "accepted": False, "error": "busy"})
        if getattr(self._worker, "failure_persistence_pending", False):
            return json.dumps({"ok": False, "accepted": False, "error": "persistence_pending"})
        if not paths and action != "reconcile":
            return json.dumps({"ok": True, "accepted": False, "job_id": "", "no_op": True})
        result = self._policy.start(self._worker, action, tuple(paths))
        self._publish_policy_state(force=True)
        return json.dumps(result, ensure_ascii=False)

    @pyqtSlot(result=str)
    def getFailurePolicyJobStatus(self) -> str:
        """Return cached progress for panel reopening without I/O or toast delivery."""
        return json.dumps(self._policy.status(), ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def excludeFile(self, path: str) -> str:
        """Accept an asynchronous exclusion for one selected file."""
        return self.startFailurePolicyJob("exclude", json.dumps([path]))

    @pyqtSlot(str, result=str)
    def unexcludeFile(self, path: str) -> str:
        """Accept an asynchronous unexclude without starting indexing."""
        return self.startFailurePolicyJob("unexclude", json.dumps([path]))

    @pyqtSlot(str, result=str)
    def excludeFiles(self, payload: str) -> str:
        """Accept an asynchronous exclusion for the current selected list."""
        return self.startFailurePolicyJob("exclude", payload)

    @pyqtSlot(str, result=str)
    def unexcludeFiles(self, payload: str) -> str:
        """Accept an asynchronous unexclude for the current selected list."""
        return self.startFailurePolicyJob("unexclude", payload)

    @staticmethod
    def _parse_failure_paths(payload: str) -> tuple[list[str], str | None]:
        """배치 실패 문서 경로 JSON을 검증·정규화·중복 제거한다."""
        try:
            raw_paths = json.loads(payload)
        except json.JSONDecodeError:
            return [], "invalid JSON"
        if not isinstance(raw_paths, list) or any(not isinstance(path, str) or not path for path in raw_paths):
            return [], "paths must be a JSON array of non-empty strings"

        from knowmate.collector.scanner import normalize_path_key
        paths: list[str] = []
        seen_keys: set[str] = set()
        for path in raw_paths:
            key = normalize_path_key(path)
            if key not in seen_keys:
                paths.append(path)
                seen_keys.add(key)
        return paths, None

    def _get_now(self) -> float:
        """failure_state 계산용 현재 시각(초). 워커가 있으면 그 시계를 따른다
        (테스트에서 워커 시계를 고정해 검증하는 경우와 표시값이 어긋나지 않도록)."""
        get_now = getattr(self._worker, "_get_now", None)
        if callable(get_now):
            return get_now()
        import time
        return time.time()

    def set_worker(self, worker) -> None:
        """단일 수집기 워커를 등록하고 시그널을 바인딩한다 (수동·유휴 인덱싱 공유)."""
        if self._worker is not None and self._worker is not worker:
            if self._worker_write_guard() is not None:
                raise RuntimeError("cannot replace a busy or unpersisted collector")
            from knowmate.app.failure_view import FailureView
            self._failure_view = FailureView()
            self._worker.progress.disconnect(self._on_worker_progress)
            self._worker.finished.disconnect(self._on_worker_finished)
            self._worker.indexing_needed.disconnect(self.indexAlert)
        self._worker = worker
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(self._on_worker_finished)
        worker.indexing_needed.connect(self.indexAlert)
        # The web UI may have queried before asynchronous PPT preparation
        # created this worker. Seed the count cache now, even if the first
        # indexing cycle has no changes and therefore skips count queries.
        self.statusUpdated.emit(self.getIndexStatus())

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _on_worker_progress(self, current: int, total: int, filename: str) -> None:
        """워커 진행률 시그널을 JSON으로 변환해 JS에 전달한다."""
        self._policy.invalidate_cache()
        payload = json.dumps({"current": current, "total": total, "filename": filename}, ensure_ascii=False)
        self.indexProgress.emit(payload)

    def _on_worker_finished(self, message: str) -> None:
        """워커 완료 시그널 처리."""
        if hasattr(self._worker, "drain_failure_updates"):
            return
        self._finish_worker_status(message)

    def _finish_worker_status(self, message: str, *, update_time: bool = True) -> None:
        """Refresh aggregate status after the collector has actually stopped."""
        from datetime import datetime
        if update_time:
            self._last_indexed = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.indexFinished.emit(message)

        # 문서·메일 개수를 바꿀 수 있는 변경이 이번 사이클에 하나도 없었으면(유휴
        # 방치 중 대부분의 사이클) DB를 열지 않고 직전 값을 그대로 재사용한다 —
        # CollectorWorker._run_cycle이 처리 건수·orphan 정리·메일 인덱싱 여부로
        # 판정해 남긴 신호(worker.last_cycle_changed)를 읽는다. 워커가 아직 이
        # 속성을 갖지 않은(구버전 테스트 더블 등) 경우는 안전하게 재계산한다.
        if not getattr(self._worker, "last_cycle_changed", True):
            local_count, shared_count, mail_count = self._local_count, self._shared_count, self._mail_count
        else:
            local_count, shared_count, mail_count, _ = self._compute_doc_mail_counts()

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

    @pyqtSlot(str, str, result=str)
    def getThread(self, mode: str, thread_id: str) -> str:
        """Read a current conversation by identity without creating it."""
        from knowmate.app.threads import get_thread
        return json.dumps(get_thread(mode, thread_id), ensure_ascii=False)

    @pyqtSlot(str, str, result=str)
    def saveThread(self, mode: str, thread_json: str) -> str:
        """Acknowledge conversation persistence, preserving caller state on failure."""
        from knowmate.app.threads import upsert_thread
        if self._query.closing:
            return json.dumps({"ok": False, "error": "closing"})
        try:
            thread = json.loads(thread_json)
            if (mode not in {"knowledge", "mes"} or not isinstance(thread, dict)
                    or not isinstance(thread.get("id"), str) or not thread["id"]
                    or thread.get("mode") != mode or not isinstance(thread.get("messages"), list)):
                raise ValueError("invalid conversation")
            upsert_thread(mode, thread)
            return json.dumps({"ok": True})
        except Exception as exc:
            logging.getLogger(__name__).warning("대화 저장 실패: error_type=%s", type(exc).__name__)
            return json.dumps({"ok": False, "error": "대화를 저장하지 못했습니다."}, ensure_ascii=False)

    @pyqtSlot(str, str, result=str)
    def deleteThread(self, mode: str, thread_id: str) -> str:
        """Acknowledge a conversation deletion without masking storage failures."""
        from knowmate.app.threads import delete_thread
        if self._query.closing:
            return json.dumps({"ok": False, "error": "closing"})
        try:
            if mode not in {"knowledge", "mes"} or not isinstance(thread_id, str) or not thread_id:
                raise ValueError("invalid conversation")
            delete_thread(mode, thread_id)
            return json.dumps({"ok": True})
        except Exception as exc:
            logging.getLogger(__name__).warning("대화 삭제 실패: error_type=%s", type(exc).__name__)
            return json.dumps({"ok": False, "error": "대화를 삭제하지 못했습니다."}, ensure_ascii=False)
