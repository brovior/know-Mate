"""Phase 3 수집기 pytest 테스트 - tmp_path 기반, 사외 환경 전부 통과."""
import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from knowmate.collector.state import load_state, save_state
from knowmate.collector.scanner import scan_folder, classify_changes, get_scope
from knowmate.collector.cleanup import CleanupManager, CleanupReport

_HAS_PYQT6 = bool(importlib.util.find_spec("PyQt6"))


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    """predicate()가 True가 될 때까지 짧게 폴링한다(비동기 daemon 스레드 결과 확인용)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ============================================================
# TestState
# ============================================================

class TestState:
    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        """없는 파일 load_state -> {} 반환."""
        result = load_state(tmp_path / "nonexistent.json")
        assert result == {}

    def test_save_load_roundtrip(self, tmp_path: Path):
        """save_state -> load_state 왕복 일치."""
        state_file = tmp_path / "state.json"
        data = {
            "C:/file.docx": {
                "mtime": 1234567890.0,
                "size": 12345,
                "indexed_at": "2026-06-21T00:00:00+00:00",
                "chunk_ids": ["uuid1", "uuid2"],
            }
        }
        save_state(state_file, data)
        loaded = load_state(state_file)
        assert loaded == data

    def test_atomic_save_uses_tmp_then_replace(self, tmp_path: Path):
        """save_state가 .tmp 파일을 거쳐 replace로 완료됨을 확인한다."""
        state_file = tmp_path / "state.json"
        save_state(state_file, {"key": "val"})
        # 저장 후 .tmp는 없어야 함
        tmp_file = state_file.with_suffix(".tmp")
        assert not tmp_file.exists()
        assert state_file.exists()

    def test_load_corrupted_file_returns_empty(self, tmp_path: Path):
        """손상된 JSON 파일 -> {} 반환 (예외 삼키지 않음)."""
        state_file = tmp_path / "state.json"
        state_file.write_text("NOT_JSON", encoding="utf-8")
        result = load_state(state_file)
        assert result == {}


# ============================================================
# TestScanner
# ============================================================

class TestScanner:
    def test_scan_folder_supported_ext_only(self, tmp_path: Path):
        """scan_folder: 지원 확장자만 수집."""
        (tmp_path / "doc1.docx").write_bytes(b"x")
        (tmp_path / "img.png").write_bytes(b"x")
        (tmp_path / "note.txt").write_bytes(b"x")
        (tmp_path / "data.csv").write_bytes(b"x")

        result = scan_folder(tmp_path)
        names = {Path(k).name for k in result}
        assert "doc1.docx" in names
        assert "note.txt" in names
        assert "img.png" not in names
        assert "data.csv" not in names

    def test_scan_folder_returns_mtime_size(self, tmp_path: Path):
        """scan_folder 반환값에 mtime, size 포함."""
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello")
        result = scan_folder(tmp_path)
        key = str(f)
        assert key in result
        assert "mtime" in result[key]
        assert "size" in result[key]
        assert result[key]["size"] == 5

    def test_classify_new_files(self, tmp_path: Path):
        """classify_changes: saved 없는 파일 -> new."""
        current = {str(tmp_path / "new.docx"): {"mtime": 1.0, "size": 100}}
        new, mod, deleted = classify_changes({}, current)
        assert str(tmp_path / "new.docx") in new
        assert mod == []
        assert deleted == []

    def test_classify_modified_files(self, tmp_path: Path):
        """classify_changes: mtime 변경된 파일 -> modified."""
        path = str(tmp_path / "mod.docx")
        saved = {path: {"mtime": 1.0, "size": 100}}
        current = {path: {"mtime": 2.0, "size": 100}}
        new, mod, deleted = classify_changes(saved, current)
        assert path in mod
        assert new == []
        assert deleted == []

    def test_classify_deleted_files(self, tmp_path: Path):
        """classify_changes: saved 에만 있는 파일 -> deleted."""
        path = str(tmp_path / "old.docx")
        saved = {path: {"mtime": 1.0, "size": 100}}
        current = {}
        new, mod, deleted = classify_changes(saved, current)
        assert path in deleted
        assert new == []
        assert mod == []

    def test_classify_unchanged_not_returned(self, tmp_path: Path):
        """classify_changes: mtime/size 동일 파일은 어디에도 포함되지 않음."""
        path = str(tmp_path / "same.docx")
        saved = {path: {"mtime": 1.0, "size": 100}}
        current = {path: {"mtime": 1.0, "size": 100}}
        new, mod, deleted = classify_changes(saved, current)
        assert path not in new
        assert path not in mod
        assert path not in deleted

    def test_get_scope_local_drives(self):
        """get_scope: C:/D:/E: -> local."""
        assert get_scope("C:/Users/doc.txt") == "local"
        assert get_scope("D:/data/file.docx") == "local"
        assert get_scope("E:/backup/note.txt") == "local"

    def test_get_scope_mapped_drive_shared(self):
        """get_scope: Z:, F: 등 매핑 드라이브 -> shared."""
        assert get_scope("Z:/share/file.docx") == "shared"
        assert get_scope("F:/shared/doc.txt") == "shared"

    def test_get_scope_unc_shared(self):
        """get_scope: UNC 경로 -> shared."""
        assert get_scope("//server/share/file.docx") == "shared"


# ============================================================
# TestCleanup
# ============================================================

def _make_mock_indexer(tmp_path: Path):
    """테스트용 Indexer mock을 생성한다."""
    from knowmate.rag.embedding import EmbeddingClient
    from knowmate.rag.indexer import Indexer
    from knowmate.secure.fake_reader import FakeReader

    embed = EmbeddingClient(base_url="http://localhost", host_header="embed.internal", fake=True)
    indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
    return indexer


class TestCleanup:
    def test_dry_run_no_actual_delete(self, tmp_path: Path):
        """dry_run=True 이면 실제 삭제 없음."""
        folder = tmp_path / "docs"
        folder.mkdir()
        f = folder / "test.docx"
        f.write_bytes(b"x")

        indexer = _make_mock_indexer(tmp_path)
        from knowmate.secure.fake_reader import FakeReader
        reader = FakeReader()
        chunk_ids = indexer.index_file(str(f), reader.extract(str(f)), f.stat().st_mtime, "local")

        f.unlink()  # 파일 삭제 -> orphan

        state = {str(f): {"mtime": 0, "size": 0, "indexed_at": "", "chunk_ids": chunk_ids}}
        mgr = CleanupManager(indexer=indexer, dry_run=True)
        report = mgr.run([str(folder)], state)

        # dry_run 이므로 newly_marked=0, physically_deleted=0
        assert report.newly_marked == 0
        assert report.physically_deleted == 0
        # state 에 항목은 그대로
        assert str(f) in state

    def test_inaccessible_folder_skipped(self, tmp_path: Path):
        """존재하지 않는 폴더 -> skipped_folders에 포함."""
        nonexist = str(tmp_path / "nonexistent_folder")
        indexer = _make_mock_indexer(tmp_path)
        state = {}
        mgr = CleanupManager(indexer=indexer, dry_run=False)
        report = mgr.run([nonexist], state)
        assert nonexist in report.skipped_folders

    def test_bulk_delete_guard(self, tmp_path: Path):
        """orphan 비율 50% -> 30% 임계값 초과 -> skipped_folders에 포함."""
        folder = tmp_path / "docs"
        folder.mkdir()

        indexer = _make_mock_indexer(tmp_path)
        from knowmate.secure.fake_reader import FakeReader
        reader = FakeReader()

        # 파일 2개 인덱싱
        files = []
        for i in range(2):
            f = folder / f"file{i}.docx"
            f.write_bytes(b"x")
            files.append(f)

        state = {}
        chunk_ids_map = {}
        for f in files:
            ids = indexer.index_file(str(f), reader.extract(str(f)), f.stat().st_mtime, "local")
            state[str(f)] = {"mtime": 0, "size": 0, "indexed_at": "", "chunk_ids": ids}
            chunk_ids_map[str(f)] = ids

        # 1개 삭제 -> 50% orphan
        files[0].unlink()

        mgr = CleanupManager(indexer=indexer, max_delete_ratio=0.30, dry_run=False)
        report = mgr.run([str(folder)], state)
        assert str(folder) in report.skipped_folders

    def test_normal_orphan_soft_delete(self, tmp_path: Path):
        """orphan < 30% -> soft delete 마킹 확인."""
        folder = tmp_path / "docs"
        folder.mkdir()

        indexer = _make_mock_indexer(tmp_path)
        from knowmate.secure.fake_reader import FakeReader
        reader = FakeReader()

        # 파일 10개 인덱싱
        files = []
        state = {}
        for i in range(10):
            f = folder / f"file{i}.docx"
            f.write_bytes(b"x")
            files.append(f)
            ids = indexer.index_file(str(f), reader.extract(str(f)), f.stat().st_mtime, "local")
            state[str(f)] = {"mtime": 0, "size": 0, "indexed_at": "", "chunk_ids": ids}

        # 1개 삭제 -> 10% orphan (< 30%)
        orphan_path = str(files[0])
        files[0].unlink()

        mgr = CleanupManager(indexer=indexer, max_delete_ratio=0.30, dry_run=False)
        report = mgr.run([str(folder)], state)

        # 폴더 스킵 없음
        assert str(folder) not in report.skipped_folders
        # DB에서 해당 청크가 is_deleted=True 이거나 miss_count>=1
        ids = state.get(orphan_path, {}).get("chunk_ids", [])
        # soft delete 이후 DB 조회
        if ids:
            id_list = ", ".join(f"'{cid}'" for cid in ids)
            df = indexer.table.search().where(f"chunk_id IN ({id_list})").limit(100).to_arrow().to_pandas()
            if not df.empty:
                assert all(df["is_deleted"])

    def test_report_fields(self, tmp_path: Path):
        """CleanupReport 필드 초기값 검증."""
        report = CleanupReport()
        assert report.scanned == 0
        assert report.newly_marked == 0
        assert report.physically_deleted == 0
        assert report.skipped_folders == []


# ============================================================
# TestCollectorWorker
# ============================================================

def _make_config(watch_folder: str) -> dict:
    """테스트용 config dict를 반환한다."""
    return {
        "collector": {"watch_folders": [watch_folder], "idle_seconds": 60},
        "cleanup": {"dry_run": True, "max_delete_ratio": 0.30},
        "chunking": {"chunk_size": 400, "overlap": 80},
    }


def _make_worker(tmp_path: Path, watch_folder: str):
    """테스트용 CollectorWorker를 반환한다."""
    from knowmate.rag.embedding import EmbeddingClient
    from knowmate.rag.indexer import Indexer
    from knowmate.secure.fake_reader import FakeReader
    from knowmate.collector.scheduler import CollectorWorker

    embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
    indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
    extractor = FakeReader()
    config = _make_config(watch_folder)
    state_file = tmp_path / "state.json"
    return CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file, purge_meta_file=tmp_path / "purge_meta.json"), indexer, state_file


class TestCollectorWorker:
    def test_new_file_indexed(self, tmp_path: Path):
        """파일 생성 후 run() -> state에 chunk_ids 존재."""
        folder = tmp_path / "docs"
        folder.mkdir()
        f = folder / "doc.docx"
        f.write_bytes(b"hello docx content")

        worker, _, state_file = _make_worker(tmp_path, str(folder))
        worker.run()

        from knowmate.collector.state import load_state
        state = load_state(state_file)
        assert str(f) in state
        assert len(state[str(f)]["chunk_ids"]) >= 1

    def test_modified_file_reindexed(self, tmp_path: Path):
        """파일 수정 후 run() -> 변경 파일 재인덱싱."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.secure.fake_reader import FakeReader
        from knowmate.collector.scheduler import CollectorWorker

        folder = tmp_path / "docs"
        folder.mkdir()
        f = folder / "doc.txt"
        f.write_bytes(b"original content")

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        extractor = FakeReader()
        config = _make_config(str(folder))
        state_file = tmp_path / "state.json"

        worker = CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file, purge_meta_file=tmp_path / "purge_meta.json")
        worker.run()

        from knowmate.collector.state import load_state
        state1 = load_state(state_file)
        assert str(f) in state1

        # 파일 수정 (mtime 변경을 위해 충분한 시간 후 재작성)
        time.sleep(0.05)
        f.write_bytes(b"modified content - different from original")

        # 같은 indexer 재사용 (DB 재생성 충돌 방지)
        worker2 = CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file, purge_meta_file=tmp_path / "purge_meta.json")
        worker2.run()

        state2 = load_state(state_file)
        new_ids = set(state2[str(f)]["chunk_ids"])
        # 재인덱싱 후 chunk_ids가 갱신됨
        assert len(new_ids) >= 1

    def test_deleted_file_orphan_soft_delete(self, tmp_path: Path):
        """파일 삭제 후 run() -> orphan soft delete (dry_run=False)."""
        folder = tmp_path / "docs"
        folder.mkdir()

        # 파일 5개 인덱싱 (orphan 비율 20% 유지)
        files = []
        for i in range(5):
            f = folder / f"file{i}.docx"
            f.write_bytes(b"content")
            files.append(f)

        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.secure.fake_reader import FakeReader
        from knowmate.collector.scheduler import CollectorWorker

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        extractor = FakeReader()
        # dry_run=False 로 설정
        config = {
            "collector": {"watch_folders": [str(folder)], "idle_seconds": 60},
            "cleanup": {"dry_run": False, "max_delete_ratio": 0.30},
            "chunking": {"chunk_size": 400, "overlap": 80},
        }
        state_file = tmp_path / "state.json"
        worker = CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file, purge_meta_file=tmp_path / "purge_meta.json")
        worker.run()

        from knowmate.collector.state import load_state
        state = load_state(state_file)

        # 1개 파일 삭제 (20% orphan < 30%)
        del_path = str(files[0])
        files[0].unlink()

        worker2 = CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file, purge_meta_file=tmp_path / "purge_meta.json")
        worker2.run()

        # orphan 파일의 청크가 soft delete 마킹되었는지 확인
        old_ids = state.get(del_path, {}).get("chunk_ids", [])
        if old_ids:
            id_list = ", ".join(f"'{cid}'" for cid in old_ids)
            df = indexer.table.search().where(f"chunk_id IN ({id_list})").limit(100).to_arrow().to_pandas()
            if not df.empty:
                assert all(df["is_deleted"])

    def test_cancel_stops_processing(self, tmp_path: Path):
        """cancel() 호출 -> 중단 후 finished 시그널 발행."""
        folder = tmp_path / "docs"
        folder.mkdir()
        for i in range(5):
            (folder / f"file{i}.docx").write_bytes(b"content")

        worker, _, state_file = _make_worker(tmp_path, str(folder))

        finished_msgs = []
        worker.finished.connect(finished_msgs.append)

        # cancel 플래그를 사전에 설정
        worker._cancelled = True
        worker.run()

        # cancelled 상태에서 즉시 종료
        assert len(finished_msgs) == 1
        assert "취소" in finished_msgs[0] or "완료" in finished_msgs[0]

    def test_single_file_failure_does_not_stop_others(self, tmp_path: Path):
        """파싱 불가 파일 1개가 있어도 나머지 처리 계속."""
        folder = tmp_path / "docs"
        folder.mkdir()

        # 정상 파일 3개
        good_files = []
        for i in range(3):
            f = folder / f"good{i}.docx"
            f.write_bytes(b"good content")
            good_files.append(f)

        # 실패 파일 1개
        bad_file = folder / "bad.txt"
        bad_file.write_bytes(b"bad")

        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker

        class FailingExtractor:
            """bad.txt는 예외, 나머지는 정상 반환."""
            def extract(self, path: str) -> str:
                if "bad" in path:
                    raise ValueError("파싱 불가")
                return "정상 문서 내용입니다."

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        config = _make_config(str(folder))
        state_file = tmp_path / "state.json"
        worker = CollectorWorker(config=config, indexer=indexer, extractor=FailingExtractor(), state_file=state_file, purge_meta_file=tmp_path / "purge_meta.json")

        finished_msgs = []
        worker.finished.connect(finished_msgs.append)
        worker.run()

        from knowmate.collector.state import load_state
        state = load_state(state_file)

        # 정상 파일 3개는 인덱싱됨
        indexed_good = sum(1 for f in good_files if str(f) in state)
        assert indexed_good == 3
        # 실패 파일은 state에 없음
        assert str(bad_file) not in state
        # finished 시그널은 1회 발행됨
        assert len(finished_msgs) == 1
        assert "실패 1건" in finished_msgs[0]


# ============================================================
# TestQueuePriority — COM 우선순위 캐싱 (PyQt6 필요)
# ============================================================

@pytest.mark.skipif(not _HAS_PYQT6, reason="PyQt6 미설치 — 폐쇄망 외 환경")
class TestQueuePriority:
    """state.json에 캐시된 method(com/plain)로 다음 사이클 처리 순서를 보정한다."""

    class _RecordingExtractor:
        """extract() 호출 순서를 기록하는 래퍼(FakeReader 위임)."""

        def __init__(self):
            from knowmate.secure.fake_reader import FakeReader
            self._inner = FakeReader()
            self.order: list[str] = []

        def extract(self, path: str) -> str:
            self.order.append(path)
            return self._inner.extract(path)

    def test_priority_queue_orders_new_before_modified(self):
        """정렬 키(action우선순위, com_rank)로 NEW가 MODIFIED보다 먼저 나온다.

        생산자·소비자가 별도 스레드로 동시에 도는 실제 사이클에서는 아직
        큐에 없는 항목까지 재정렬할 수 없으므로(스트리밍 구조의 본질적
        한계), 정렬 메커니즘 자체는 스레드 경합 없이 직접 검증한다.
        """
        import queue as _queue
        from knowmate.collector.scheduler import IndexTask, PRIORITY_NEW, PRIORITY_MODIFIED, _PLAIN_RANK

        q: "_queue.PriorityQueue" = _queue.PriorityQueue()
        # 일부러 반대 순서로 삽입 — 우선순위 정렬이 실제로 동작함을 검증
        q.put(((PRIORITY_MODIFIED, _PLAIN_RANK), 0, IndexTask(PRIORITY_MODIFIED, "old.txt", "modified")))
        q.put(((PRIORITY_NEW, _PLAIN_RANK), 1, IndexTask(PRIORITY_NEW, "new.txt", "new")))

        first = q.get()[2]
        second = q.get()[2]
        assert first.path == "new.txt"
        assert second.path == "old.txt"

    def test_priority_queue_orders_com_before_plain_in_same_tier(self):
        """같은 action(modified) 내에서 com_rank=0(COM 캐시)이 plain보다 먼저 나온다."""
        import queue as _queue
        from knowmate.collector.scheduler import IndexTask, PRIORITY_MODIFIED, _COM_RANK, _PLAIN_RANK

        q: "_queue.PriorityQueue" = _queue.PriorityQueue()
        q.put(((PRIORITY_MODIFIED, _PLAIN_RANK), 0, IndexTask(PRIORITY_MODIFIED, "plain.txt", "modified", _PLAIN_RANK)))
        q.put(((PRIORITY_MODIFIED, _COM_RANK), 1, IndexTask(PRIORITY_MODIFIED, "com.txt", "modified", _COM_RANK)))

        first = q.get()[2]
        assert first.path == "com.txt"

    def test_sentinel_always_sorts_last(self):
        """종료 신호(_SENTINEL_KEY)는 어떤 실제 우선순위보다도 낮다(항상 마지막에 소비)."""
        import queue as _queue
        from knowmate.collector.scheduler import (
            IndexTask, PRIORITY_ORPHAN, _SENTINEL_KEY, _PLAIN_RANK,
        )

        q: "_queue.PriorityQueue" = _queue.PriorityQueue()
        q.put((_SENTINEL_KEY, 0, None))
        q.put(((PRIORITY_ORPHAN, _PLAIN_RANK), 1, IndexTask(PRIORITY_ORPHAN, "last_real.txt", "orphan")))

        first = q.get()[2]
        second = q.get()[2]
        assert first is not None and first.path == "last_real.txt"
        assert second is None

    def test_classify_extract_method(self, tmp_path: Path):
        """확장자·zip 서명으로 다음 사이클 우선순위 힌트(method)를 분류한다."""
        from knowmate.collector.scheduler import _classify_extract_method
        import zipfile

        doc = tmp_path / "a.doc"
        doc.write_bytes(b"\xd0\xcf\x11\xe0")  # OLE2 매직 — 확장자만으로도 com
        assert _classify_extract_method(str(doc)) == "com"

        drm_xlsx = tmp_path / "b.xlsx"
        drm_xlsx.write_bytes(b"<## " + b"\x00" * 20)  # zip 아님 → com
        assert _classify_extract_method(str(drm_xlsx)) == "com"

        real_xlsx = tmp_path / "c.xlsx"
        with zipfile.ZipFile(real_xlsx, "w") as zf:
            zf.writestr("dummy.txt", "content")
        assert _classify_extract_method(str(real_xlsx)) == "plain"

        txt = tmp_path / "d.txt"
        txt.write_bytes(b"hello")
        assert _classify_extract_method(str(txt)) == "plain"

    def test_classify_xls_by_ole2_signature(self, tmp_path: Path):
        """.xls는 xlrd 대응이 있어 정상 OLE2면 plain, 아니면(DRM 등) com으로 분류된다."""
        from knowmate.collector.scheduler import _classify_extract_method, _is_drm_suspected

        real_xls = tmp_path / "a.xls"
        real_xls.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16)  # 정상 OLE2
        assert _classify_extract_method(str(real_xls)) == "plain"
        assert _is_drm_suspected(str(real_xls)) is False

        drm_xls = tmp_path / "b.xls"
        drm_xls.write_bytes(b"<## " + b"\x00" * 20)  # OLE2 아님 → DRM 의심
        assert _classify_extract_method(str(drm_xls)) == "com"
        assert _is_drm_suspected(str(drm_xls)) is True

    def test_method_recorded_in_state_after_success(self, tmp_path: Path):
        """인덱싱 성공 후 state에 method(com/plain)가 기록된다."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker
        from knowmate.collector.state import load_state

        folder = tmp_path / "docs"
        folder.mkdir()
        f = folder / "plain.docx"
        import zipfile
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("dummy.txt", "zip content")  # 실제 zip 서명 → plain 분류

        state_file = tmp_path / "state.json"
        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        from knowmate.secure.fake_reader import FakeReader
        worker = CollectorWorker(
            config=_make_config(str(folder)), indexer=indexer,
            extractor=FakeReader(), state_file=state_file,
            purge_meta_file=tmp_path / "purge_meta.json",
        )
        worker.run()

        state = load_state(state_file)
        assert state[str(f)]["method"] == "plain"


# ============================================================
# TestDrmIdleSkip — 유휴 장기화 시 DRM 의심 문서 스킵 (PyQt6 필요)
# ============================================================

@pytest.mark.skipif(not _HAS_PYQT6, reason="PyQt6 미설치 — 폐쇄망 외 환경")
class TestDrmIdleSkip:
    def test_is_drm_suspected_classification(self, tmp_path: Path):
        """정상 레거시(OLE2)·정상 OOXML(zip)은 스킵 대상 아님, 그 외만 의심."""
        import zipfile
        from knowmate.collector.scheduler import _is_drm_suspected

        real_doc = tmp_path / "a.doc"
        real_doc.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")  # 정상 OLE2
        assert _is_drm_suspected(str(real_doc)) is False

        drm_doc = tmp_path / "b.doc"
        drm_doc.write_bytes(b"<## " + b"\x00" * 20)  # OLE2도 zip도 아님 → 의심
        assert _is_drm_suspected(str(drm_doc)) is True

        real_xlsx = tmp_path / "c.xlsx"
        with zipfile.ZipFile(real_xlsx, "w") as zf:
            zf.writestr("x.txt", "y")
        assert _is_drm_suspected(str(real_xlsx)) is False  # 정상 zip

        drm_xlsx = tmp_path / "d.xlsx"
        drm_xlsx.write_bytes(b"<## " + b"\x00" * 20)
        assert _is_drm_suspected(str(drm_xlsx)) is True

        txt = tmp_path / "e.txt"
        txt.write_bytes(b"hello")
        assert _is_drm_suspected(str(txt)) is False  # office 확장자 아님 → 대상 아님

    def test_drm_suspected_file_skipped_when_idle_exceeds_threshold(self, tmp_path: Path):
        """실시간 유휴가 임계를 넘으면 DRM 의심 파일은 큐잉되지 않고(state 불변) 넘어간다."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker
        from knowmate.collector.state import load_state
        from knowmate.secure.fake_reader import FakeReader

        folder = tmp_path / "docs"
        folder.mkdir()
        drm_file = folder / "drm.xlsx"
        drm_file.write_bytes(b"<## " + b"\x00" * 20)  # DRM 의심
        normal_file = folder / "normal.txt"
        normal_file.write_bytes(b"hello")

        config = _make_config(str(folder))
        config["collector"]["drm_idle_threshold_sec"] = 100
        state_file = tmp_path / "state.json"
        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        worker = CollectorWorker(
            config=config, indexer=indexer, extractor=FakeReader(), state_file=state_file,
            purge_meta_file=tmp_path / "purge_meta.json",
            get_idle_seconds=lambda: 200.0,  # 임계(100) 초과 상태를 실시간으로 보고
        )
        worker.run()

        state = load_state(state_file)
        assert str(normal_file) in state          # 일반 파일은 정상 인덱싱
        assert str(drm_file) not in state          # DRM 의심 파일은 스킵(state 불변)

    def test_drm_suspected_file_processed_when_idle_below_threshold(self, tmp_path: Path):
        """실시간 유휴가 임계 미만(사용자 활동 중)이면 DRM 의심 파일도 정상 처리된다."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker
        from knowmate.collector.state import load_state
        from knowmate.secure.fake_reader import FakeReader

        folder = tmp_path / "docs"
        folder.mkdir()
        drm_file = folder / "drm.xlsx"
        drm_file.write_bytes(b"<## " + b"\x00" * 20)

        config = _make_config(str(folder))
        config["collector"]["drm_idle_threshold_sec"] = 480
        state_file = tmp_path / "state.json"
        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        worker = CollectorWorker(
            config=config, indexer=indexer, extractor=FakeReader(), state_file=state_file,
            purge_meta_file=tmp_path / "purge_meta.json",
            get_idle_seconds=lambda: 5.0,  # 방금 활동함 → 스킵 안 함
        )
        worker.run()

        state = load_state(state_file)
        assert str(drm_file) in state

    def test_drm_skip_kicks_in_mid_cycle_when_session_expires(self, tmp_path: Path):
        """사이클 도중 유휴가 임계를 넘어서면 그 시점부터 DRM 의심 파일이 스킵된다.

        시작 시엔 유휴가 낮아 앞쪽 DRM 파일은 처리되지만, 유휴가 임계를 넘긴
        뒤 도달한 DRM 파일은 스킵된다(수동 재인덱싱 후 퇴근 시나리오의 핵심)."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker
        from knowmate.collector.state import load_state
        from knowmate.secure.fake_reader import FakeReader

        folder = tmp_path / "docs"
        folder.mkdir()
        # 여러 DRM 의심 파일 — 유휴가 파일 처리 도중 임계를 넘어가도록 시뮬레이션
        drm_files = []
        for i in range(6):
            f = folder / f"drm{i}.xlsx"
            f.write_bytes(b"<## " + b"\x00" * 20)
            drm_files.append(f)

        config = _make_config(str(folder))
        config["collector"]["drm_idle_threshold_sec"] = 100
        state_file = tmp_path / "state.json"
        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)

        # 처음 몇 번은 낮은 유휴(50) → 이후 높은 유휴(300)로 전환
        readings = iter([50.0, 50.0])

        def _idle():
            try:
                return next(readings)
            except StopIteration:
                return 300.0  # 세션 만료 추정 구간

        worker = CollectorWorker(
            config=config, indexer=indexer, extractor=FakeReader(), state_file=state_file,
            purge_meta_file=tmp_path / "purge_meta.json",
            get_idle_seconds=_idle,
        )
        worker.run()

        state = load_state(state_file)
        indexed = [f for f in drm_files if str(f) in state]
        skipped = [f for f in drm_files if str(f) not in state]
        # 초반(낮은 유휴)엔 처리, 후반(높은 유휴)엔 스킵 — 둘 다 존재해야 한다
        assert len(indexed) >= 1
        assert len(skipped) >= 1

    def test_non_drm_never_skipped_regardless_of_idle(self, tmp_path: Path):
        """유휴가 아무리 길어도 DRM 의심이 아닌 일반 파일은 절대 스킵되지 않는다."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker
        from knowmate.collector.state import load_state
        from knowmate.secure.fake_reader import FakeReader

        folder = tmp_path / "docs"
        folder.mkdir()
        normal = folder / "normal.txt"
        normal.write_bytes(b"hello")

        config = _make_config(str(folder))
        config["collector"]["drm_idle_threshold_sec"] = 100
        state_file = tmp_path / "state.json"
        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        worker = CollectorWorker(
            config=config, indexer=indexer, extractor=FakeReader(), state_file=state_file,
            purge_meta_file=tmp_path / "purge_meta.json",
            get_idle_seconds=lambda: 99999.0,  # 아주 긴 유휴
        )
        worker.run()

        assert str(normal) in load_state(state_file)

        state = load_state(state_file)
        assert str(drm_file) in state  # 기본값 0.0이라 스킵되지 않음


# ============================================================
# TestPurgeRemovedFolders — _purge_removed_folders 안전장치 (베타 배포 전 발견된 이슈)
# ============================================================

class TestPurgeRemovedFolders:
    """watch_folders가 비정상적으로 비거나 축소될 때 인덱스가 통째로 삭제되지
    않도록 하는 안전장치를 검증한다. 실제 발단: config 시드 로직이 최초 실행 시
    watch_folders를 []로 초기화하는데, 이 상태로 유휴 스케줄러가 돌면
    dry_run=false인 사용자의 전체 인덱스가 삭제될 수 있었다.
    """

    def test_purge_skips_when_watch_folders_empty(self, tmp_path: Path):
        """watch_folders가 비어 있으면 아무것도 삭제하지 않는다(전체 삭제 오판 방지)."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker

        folder = tmp_path / "docs"
        folder.mkdir()
        f = folder / "doc.docx"
        f.write_bytes(b"content")

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        chunk_ids = indexer.index_file(path=str(f), text="본문 내용", mtime=f.stat().st_mtime, scope="local")

        state = {str(f): {"mtime": f.stat().st_mtime, "size": f.stat().st_size, "chunk_ids": chunk_ids}}
        worker = CollectorWorker(
            config=_make_config(str(folder)), indexer=indexer,
            extractor=None, state_file=tmp_path / "state.json",
        )

        worker._purge_removed_folders([], state, dry_run=False, max_delete_ratio=0.30)

        # state 불변
        assert str(f) in state
        # DB에도 그대로 남아있음
        df = indexer.table.to_arrow().to_pandas()
        active = df[~df["is_deleted"]]
        assert str(f) in active["file_path"].values

    def test_purge_dry_run_does_not_touch_state(self, tmp_path: Path):
        """dry_run=True이면 DB뿐 아니라 state도 전혀 건드리지 않는다(완전한 예행연습)."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker

        removed_folder = tmp_path / "removed_docs"
        removed_folder.mkdir()
        f = removed_folder / "doc.docx"
        f.write_bytes(b"content")

        kept_folder = tmp_path / "kept_docs"
        kept_folder.mkdir()

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        chunk_ids = indexer.index_file(path=str(f), text="본문 내용", mtime=f.stat().st_mtime, scope="local")

        state = {str(f): {"mtime": f.stat().st_mtime, "size": f.stat().st_size, "chunk_ids": chunk_ids}}
        worker = CollectorWorker(
            config=_make_config(str(kept_folder)), indexer=indexer,
            extractor=None, state_file=tmp_path / "state.json",
        )

        # removed_folder는 watch_folders에 없으므로 doc.docx는 stale 대상이지만 dry_run=True
        worker._purge_removed_folders([str(kept_folder)], state, dry_run=True, max_delete_ratio=0.30)

        # dry_run이므로 state에서 제거되면 안 됨 (기존 버그: dry_run이어도 state.pop 실행됨)
        assert str(f) in state
        # DB에서도 삭제되지 않아야 함
        df = indexer.table.to_arrow().to_pandas()
        active = df[~df["is_deleted"]]
        assert str(f) in active["file_path"].values

    def test_purge_blocks_mass_deletion(self, tmp_path: Path):
        """삭제 대상이 max_delete_ratio를 초과하면 실제 삭제를 차단한다."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker

        stale_folder = tmp_path / "stale_docs"
        stale_folder.mkdir()
        kept_folder = tmp_path / "kept_docs"
        kept_folder.mkdir()

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)

        state = {}
        # stale_folder에 4개 인덱싱 (kept_folder엔 아무것도 없음 -> 전부 stale, ratio=100%)
        for i in range(4):
            f = stale_folder / f"doc{i}.docx"
            f.write_bytes(b"content")
            chunk_ids = indexer.index_file(path=str(f), text="본문", mtime=f.stat().st_mtime, scope="local")
            state[str(f)] = {"mtime": f.stat().st_mtime, "size": f.stat().st_size, "chunk_ids": chunk_ids}

        worker = CollectorWorker(
            config=_make_config(str(kept_folder)), indexer=indexer,
            extractor=None, state_file=tmp_path / "state.json",
        )

        alerts = []
        worker.indexing_needed.connect(alerts.append)

        # watch_folders=[kept_folder] (비어있지 않음) 이지만 전부 stale -> ratio 100% > 30% 차단
        worker._purge_removed_folders([str(kept_folder)], state, dry_run=False, max_delete_ratio=0.30)

        # 차단되었으므로 DB에 그대로 남아있어야 함
        df = indexer.table.to_arrow().to_pandas()
        active = df[~df["is_deleted"]]
        assert len(active) == 4
        # state도 그대로
        assert len(state) == 4
        # UI 알림 발행됨
        assert len(alerts) == 1
        assert "대량 삭제" in alerts[0]

    def test_projection_api_missing_returns_unsupported(self, tmp_path: Path):
        """table.search()에 select()가 없는(lancedb 버전 비호환) 경우 "failed"(일시적
        장애)가 아니라 "unsupported"(영구 장애)로 구분된다(설계 리뷰 11차 M-2) —
        재시도로 복구되지 않으므로 30분 백오프를 반복하지 않고 장기 억제해야 한다."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector.scheduler import CollectorWorker

        folder = tmp_path / "docs"
        folder.mkdir()

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)

        class _NoSelectQueryBuilder:
            def select(self, *a, **kw):
                raise AttributeError("이 lancedb 버전엔 select()가 없음(시뮬레이션)")

        class _TableWithoutSelect:
            def search(self):
                return _NoSelectQueryBuilder()

        worker = CollectorWorker(
            config=_make_config(str(folder)), indexer=indexer,
            extractor=None, state_file=tmp_path / "state.json",
        )
        worker._indexer._table = _TableWithoutSelect()  # projection 미지원 시뮬레이션(table은 읽기전용 프로퍼티)

        result = worker._purge_removed_folders([str(folder)], {}, dry_run=False, max_delete_ratio=0.30)
        assert result == "unsupported"


# ============================================================
# TestPurgeMetaIntegration — CollectorWorker + purge_meta 스킵/강제 reconciliation 통합
# ============================================================

class TestPurgeMetaIntegration:
    """유휴 방치 중(변경 0건) purge DB 조회가 실제로 스킵되는지, 구성 변경 시
    즉시 재실행되는지를 full-cycle(run())로 검증한다(설계 A-0002 AC-2)."""

    def _make_worker_with_meta(self, tmp_path: Path, indexer, watch_folder: str, meta_file: Path):
        from knowmate.secure.fake_reader import FakeReader
        from knowmate.collector.scheduler import CollectorWorker

        config = _make_config(watch_folder)
        state_file = tmp_path / "state.json"
        return CollectorWorker(
            config=config, indexer=indexer, extractor=FakeReader(), state_file=state_file,
            purge_meta_file=meta_file,
        )

    def test_first_cycle_runs_purge_no_prior_meta(self, tmp_path: Path):
        """메타가 없는 첫 사이클은 purge를 실행하고(스킵하지 않고) 메타를 남긴다."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from unittest.mock import patch as _patch

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "doc.txt").write_bytes(b"hello")

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        meta_file = tmp_path / "meta.json"
        worker = self._make_worker_with_meta(tmp_path, indexer, str(folder), meta_file)

        with _patch.object(worker, "_purge_removed_folders", wraps=worker._purge_removed_folders) as spy:
            worker.run()
        assert spy.call_count == 1
        assert meta_file.exists()

    def test_second_cycle_skips_purge_when_unchanged_and_zero_processed(self, tmp_path: Path):
        """구성 불변 + 두 번째 사이클에 처리할 신규/변경 파일이 없으면 purge DB 조회가
        스킵된다(AC-2)."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from unittest.mock import patch as _patch

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "doc.txt").write_bytes(b"hello")

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        meta_file = tmp_path / "meta.json"

        worker1 = self._make_worker_with_meta(tmp_path, indexer, str(folder), meta_file)
        worker1.run()  # 1차: 신규 파일 1건 처리 + purge 실행 → 메타 기록

        worker2 = self._make_worker_with_meta(tmp_path, indexer, str(folder), meta_file)
        with _patch.object(worker2, "_purge_removed_folders", wraps=worker2._purge_removed_folders) as spy:
            worker2.run()  # 2차: 변경 파일 없음(처리 0건) + 구성 동일 → purge 스킵
        assert spy.call_count == 0

    def test_purge_runs_again_when_watch_folders_change(self, tmp_path: Path):
        """watch_folders 구성이 바뀌면(op_sig 변경) 처리 0건이어도 purge가 즉시 재실행된다."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from unittest.mock import patch as _patch

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "doc.txt").write_bytes(b"hello")
        other_folder = tmp_path / "other"
        other_folder.mkdir()

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        meta_file = tmp_path / "meta.json"

        worker1 = self._make_worker_with_meta(tmp_path, indexer, str(folder), meta_file)
        worker1.run()

        # watch_folders를 다른 폴더로 변경 (op_sig 변경)
        worker2 = self._make_worker_with_meta(tmp_path, indexer, str(other_folder), meta_file)
        with _patch.object(worker2, "_purge_removed_folders", wraps=worker2._purge_removed_folders) as spy:
            worker2.run()
        assert spy.call_count == 1

    def test_meta_persists_across_worker_instances(self, tmp_path: Path):
        """sidecar 파일이 원자 저장되어 다음 워커 인스턴스(프로세스 재시작 시뮬레이션)가
        읽을 수 있다."""
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.collector import purge_meta

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "doc.txt").write_bytes(b"hello")

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        meta_file = tmp_path / "meta.json"

        worker1 = self._make_worker_with_meta(tmp_path, indexer, str(folder), meta_file)
        worker1.run()

        loaded = purge_meta.load_purge_meta(meta_file)
        assert loaded.reconciled_sig is not None
        assert loaded.last_purge_ts is not None


class TestMaxDeleteRatioFailClosed:
    """config.yaml은 사용자가 직접 편집 가능하므로 max_delete_ratio에 NaN·범위 밖 값이
    들어와도 대량삭제 차단기가 우회되지 않아야 한다(설계 리뷰 10차 B-1) — 삭제 안전장치는
    fail-closed(무효 값 → 사실상 전체 차단)여야지, 조용히 기본값으로 폴백해 정상처럼 동작하면
    안 된다."""

    def test_nan_max_delete_ratio_blocks_deletion_and_alerts(self, tmp_path: Path):
        from knowmate.rag.embedding import EmbeddingClient
        from knowmate.rag.indexer import Indexer
        from knowmate.secure.fake_reader import FakeReader
        from knowmate.collector.scheduler import CollectorWorker

        stale_folder = tmp_path / "stale_docs"
        stale_folder.mkdir()
        kept_folder = tmp_path / "kept_docs"
        kept_folder.mkdir()

        embed = EmbeddingClient(base_url="http://localhost", host_header="e", fake=True)
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)

        f = stale_folder / "doc.docx"
        f.write_bytes(b"content")
        chunk_ids = indexer.index_file(path=str(f), text="본문", mtime=f.stat().st_mtime, scope="local")

        # kept_docs만 watch_folders에 남기고(stale_docs는 제거됨) dry_run=False +
        # max_delete_ratio=NaN — config.yaml에서 사용자가 실수로 넣을 수 있는 값을 재현.
        config = {
            "collector": {"watch_folders": [str(kept_folder)], "idle_seconds": 60},
            "cleanup": {"dry_run": False, "max_delete_ratio": float("nan")},
            "chunking": {"chunk_size": 400, "overlap": 80},
        }
        worker = CollectorWorker(
            config=config, indexer=indexer, extractor=FakeReader(),
            state_file=tmp_path / "state.json", purge_meta_file=tmp_path / "purge_meta.json",
        )
        alerts = []
        worker.indexing_needed.connect(alerts.append)

        worker.run()

        # fail-closed: 삭제되지 않고 그대로 남아 있어야 함
        df = indexer.table.to_arrow().to_pandas()
        active = df[~df["is_deleted"]]
        assert str(f) in active["file_path"].values
        # 설정 이상 알림이 발행됨
        assert any("max_delete_ratio" in a for a in alerts)


# ============================================================
# TestIdleUtil — GetLastInputInfo 읽기전용 조회 (Qt 무관, 순수 로직)
# ============================================================

class TestIdleUtil:
    def test_non_windows_returns_zero(self, monkeypatch):
        """비Windows에서는 항상 0.0(안전 기본값)."""
        import sys
        from knowmate.collector import idle_util
        monkeypatch.setattr(sys, "platform", "linux")
        assert idle_util.get_idle_seconds() == 0.0

    def test_windows_computes_elapsed_from_ticks(self, monkeypatch):
        """last_input_tick과 tick_count 차이로 경과초를 계산한다."""
        import sys
        from knowmate.collector import idle_util
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(idle_util, "_query_last_input_tick", lambda: 10_000)
        monkeypatch.setattr(idle_util, "_tick_count", lambda: 130_000)  # 120초 경과
        assert idle_util.get_idle_seconds() == pytest.approx(120.0)

    def test_windows_query_failure_returns_zero(self, monkeypatch):
        """API 조회 실패(None) 시 0.0."""
        import sys
        from knowmate.collector import idle_util
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(idle_util, "_query_last_input_tick", lambda: None)
        monkeypatch.setattr(idle_util, "_tick_count", lambda: 1000)
        assert idle_util.get_idle_seconds() == 0.0

    def test_tick_wraparound_returns_zero(self, monkeypatch):
        """GetTickCount 랩어라운드로 음수 차이가 나오면 0.0(방금 활동함으로 간주)."""
        import sys
        from knowmate.collector import idle_util
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(idle_util, "_query_last_input_tick", lambda: 4_294_960_000)
        monkeypatch.setattr(idle_util, "_tick_count", lambda: 100)  # 랩어라운드 직후
        assert idle_util.get_idle_seconds() == 0.0


# ============================================================
# TestIdleScheduler — 실제 OS 유휴 시간 기반 트리거 (PyQt6 필요)
# ============================================================

@pytest.mark.skipif(not _HAS_PYQT6, reason="PyQt6 미설치 — 폐쇄망 외 환경")
class TestIdleScheduler:
    def _make_scheduler(
        self, get_idle_seconds, idle_seconds=60, trigger=None, is_busy=None,
        drm_idle_threshold_sec=480.0,
    ):
        from knowmate.collector.scheduler import IdleScheduler
        return IdleScheduler(
            trigger=trigger or MagicMock(),
            is_busy=is_busy or (lambda: False),
            idle_seconds=idle_seconds,
            get_idle_seconds=get_idle_seconds,
            drm_idle_threshold_sec=drm_idle_threshold_sec,
        )

    def test_triggers_when_actual_idle_meets_threshold(self):
        """실제 유휴 시간이 임계 이상이면 트리거하고 다음 사이클을 재예약한다."""
        trigger = MagicMock()
        sched = self._make_scheduler(get_idle_seconds=lambda: 90.0, idle_seconds=60, trigger=trigger)
        sched._on_idle()
        trigger.assert_called_once()
        assert sched._timer.isActive()
        assert sched._timer.interval() == 60_000

    def test_trigger_called_without_idle_argument(self):
        """트리거 콜백은 인자 없이 호출된다(DRM 스킵은 워커가 실시간 유휴로 판정)."""
        trigger = MagicMock()
        sched = self._make_scheduler(get_idle_seconds=lambda: 123.0, idle_seconds=60, trigger=trigger)
        sched._on_idle()
        trigger.assert_called_once_with()

    def test_does_not_trigger_when_user_active(self):
        """실제 유휴 시간이 임계 미만(사용자 활동 중)이면 트리거하지 않고 재확인만 예약한다."""
        trigger = MagicMock()
        sched = self._make_scheduler(get_idle_seconds=lambda: 5.0, idle_seconds=60, trigger=trigger)
        sched._on_idle()
        trigger.assert_not_called()
        assert sched._timer.isActive()
        # 남은 시간(55s) 만큼 재예약 (최소 재확인 간격 5s보다 크므로 remaining 사용)
        assert sched._timer.interval() == 55_000

    def test_recheck_interval_has_minimum_floor(self):
        """임계에 아주 근접해 남은 시간이 짧아도 최소 재확인 간격 이상으로 예약한다."""
        trigger = MagicMock()
        sched = self._make_scheduler(get_idle_seconds=lambda: 59.0, idle_seconds=60, trigger=trigger)
        sched._on_idle()
        trigger.assert_not_called()
        from knowmate.collector.scheduler import _MIN_RECHECK_SECONDS
        assert sched._timer.interval() == int(_MIN_RECHECK_SECONDS * 1000)

    def test_busy_skips_trigger_even_if_idle(self):
        """인덱싱 진행 중이면 유휴 조건을 만족해도 트리거하지 않는다."""
        trigger = MagicMock()
        sched = self._make_scheduler(
            get_idle_seconds=lambda: 999.0, idle_seconds=60, trigger=trigger, is_busy=lambda: True,
        )
        sched._on_idle()
        trigger.assert_not_called()
        assert sched._timer.isActive()

    # ── 복귀 감지 워처 (Phase D) ──────────────────────────────────

    def test_recovery_triggers_catchup_after_long_idle(self):
        """장시간 유휴 뒤 활동 재개를 감지하면 즉시 트리거한다(인자 없이)."""
        trigger = MagicMock()
        readings = iter([500.0, 2.0])  # 1틱: 장시간 유휴 / 2틱: 방금 활동
        sched = self._make_scheduler(
            get_idle_seconds=lambda: next(readings), trigger=trigger, drm_idle_threshold_sec=480.0,
        )
        sched._on_recovery_check()  # 장시간 유휴 감지 — 아직 트리거 안 함
        trigger.assert_not_called()
        sched._on_recovery_check()  # 활동 재개 감지 — 캐치업 트리거
        trigger.assert_called_once_with()

    def test_recovery_does_not_trigger_without_prior_long_idle(self):
        """장시간 유휴가 선행되지 않았으면 유휴가 짧아져도 캐치업을 트리거하지 않는다."""
        trigger = MagicMock()
        sched = self._make_scheduler(get_idle_seconds=lambda: 2.0, trigger=trigger)
        sched._on_recovery_check()
        trigger.assert_not_called()

    def test_recovery_does_not_trigger_while_still_idle(self):
        """유휴가 임계를 넘은 채 유지되면(아직 복귀 아님) 캐치업을 트리거하지 않는다."""
        trigger = MagicMock()
        sched = self._make_scheduler(
            get_idle_seconds=lambda: 500.0, trigger=trigger, drm_idle_threshold_sec=480.0,
        )
        sched._on_recovery_check()
        sched._on_recovery_check()
        trigger.assert_not_called()

    def test_recovery_skips_check_when_busy(self):
        """인덱싱 진행 중이면 복귀 판정 자체를 보류한다(was_long_idle 상태 유지)."""
        trigger = MagicMock()
        readings = iter([500.0, 2.0])
        sched = self._make_scheduler(
            get_idle_seconds=lambda: next(readings), trigger=trigger,
            drm_idle_threshold_sec=480.0, is_busy=lambda: True,
        )
        sched._on_recovery_check()  # busy — 500.0 소비했지만 판정 보류
        sched._on_recovery_check()  # busy — 2.0도 소비, 여전히 보류
        trigger.assert_not_called()

    def test_start_resets_recovery_state_and_starts_watcher(self):
        """start() 호출 시 복귀 워처가 시작되고 상태가 초기화된다."""
        sched = self._make_scheduler(get_idle_seconds=lambda: 0.0)
        sched._was_long_idle = True
        sched.start()
        assert sched._was_long_idle is False
        assert sched._recovery_timer.isActive()

    def test_stop_stops_recovery_watcher(self):
        """stop() 호출 시 복귀 워처도 함께 멈춘다."""
        sched = self._make_scheduler(get_idle_seconds=lambda: 0.0)
        sched.start()
        sched.stop()
        assert not sched._recovery_timer.isActive()


# ============================================================
# TestSingleInstance — 중복 실행 방지 (PyQt6 필요)
# ============================================================

@pytest.mark.skipif(not _HAS_PYQT6, reason="PyQt6 미설치 — 폐쇄망 외 환경")
class TestSingleInstance:
    """QLocalServer/Socket 기반 단일 인스턴스 가드. 두 인스턴스가 같은
    LanceDB/state 파일에 동시에 쓰는 것을 막기 위함(원칙8과 같은 이유)."""

    @pytest.fixture(autouse=True)
    def _no_leftover_server(self):
        """테스트 전후로 서버 이름이 남아있지 않도록 정리한다(테스트 간 격리)."""
        from PyQt6.QtNetwork import QLocalServer
        from knowmate.app.single_instance import _SERVER_NAME
        QLocalServer.removeServer(_SERVER_NAME)
        yield
        QLocalServer.removeServer(_SERVER_NAME)

    def test_first_instance_acquires(self):
        """서버가 없으면 True(내가 1등 인스턴스)를 반환한다."""
        from knowmate.app.single_instance import try_acquire_or_notify_existing
        assert try_acquire_or_notify_existing() is True

    def test_second_instance_detects_and_notifies(self):
        """서버가 이미 떠 있으면 False를 반환하고 기존 서버의 show_requested가 발동된다."""
        from PyQt6.QtWidgets import QApplication
        from knowmate.app.single_instance import (
            SingleInstanceServer, try_acquire_or_notify_existing,
        )

        server = SingleInstanceServer()
        received = []
        server.show_requested.connect(lambda: received.append(True))
        try:
            assert try_acquire_or_notify_existing() is False

            app = QApplication.instance()
            for _ in range(50):
                app.processEvents()
                if received:
                    break
                time.sleep(0.02)
            assert received == [True]
        finally:
            server.close()

    def test_server_close_allows_new_acquisition(self):
        """서버를 닫으면 이후 다시 첫 인스턴스로 획득할 수 있다."""
        from knowmate.app.single_instance import (
            SingleInstanceServer, try_acquire_or_notify_existing,
        )
        server = SingleInstanceServer()
        assert try_acquire_or_notify_existing() is False
        server.close()
        assert try_acquire_or_notify_existing() is True


# ============================================================
# TestStopWorker — 종료 시 워커 정리 에스컬레이션 (PyQt6 무관)
# ============================================================

class TestDefaultHardExit:
    """하드 종료 경로가 logging.shutdown()을 기다리지 않는지 검증한다(설계 리뷰 9차 B-1).

    QThread.terminate()가 로깅 핸들러 락을 쥔 채로 스레드를 강제 중단시켰다면
    logging.shutdown()이 그 락을 영원히 기다려, 최후 안전망이어야 할 하드 종료 자체가
    멈추는 모순이 생긴다. 실제 os._exit는 프로세스를 죽이므로 pytest 안에서 직접
    호출할 수는 없어, os._exit와 logging.shutdown을 각각 스파이로 치환해 호출 여부·
    순서만 검증한다.
    """

    def test_does_not_call_logging_shutdown(self, monkeypatch):
        import knowmate.app.lifecycle as lifecycle

        shutdown_calls = []
        exit_calls = []
        monkeypatch.setattr(lifecycle.logging, "shutdown", lambda: shutdown_calls.append(1))
        monkeypatch.setattr(lifecycle.os, "_exit", lambda code: exit_calls.append(code))

        lifecycle._default_hard_exit(0)

        assert exit_calls == [0]
        assert shutdown_calls == []  # logging.shutdown()을 기다리지 않는다


class TestDirtyShutdownMarker:
    """강제 종료 사실을 저비용으로 기록·확인하는 표식(설계 리뷰 10차 M-1 → 11차 B-1로
    구현 방식 수정).

    LanceDB 쓰기 도중 강제 종료됐을 가능성을 자동으로 감지·복구하지는 않지만(검증
    불가능한 추측성 로직은 미구현 — docs/DESIGN.md 참조), "강제 종료가 있었다"는
    사실만은 사용자에게 알릴 수 있게 기록한다. 표식은 **시작 시 미리** 남기고
    **정상 quit에서만** 지운다 — hard-exit 경로는 파일 I/O를 전혀 거치지 않아야
    "하드 종료는 무조건·즉시" 불변식이 깨지지 않는다(리뷰11 B-1).
    """

    def test_first_run_not_dirty_but_marks_for_this_run(self, tmp_path: Path):
        """표식이 없던 상태(첫 실행) → False 반환하지만, 이번 실행을 위한 표식은 남긴다."""
        from knowmate.app.lifecycle import check_and_remark_dirty_shutdown

        marker = tmp_path / "dirty_shutdown.flag"
        assert not marker.exists()
        assert check_and_remark_dirty_shutdown(marker) is False
        assert marker.exists()  # 이번 실행 보호용 표식은 기록됨

    def test_stale_marker_reports_dirty_and_rewrites(self, tmp_path: Path):
        """이전 실행이 표식을 못 지우고 끝났다면(강제 종료) True를 반환하고, 이번
        실행을 위해 다시 기록한다."""
        from knowmate.app.lifecycle import check_and_remark_dirty_shutdown

        marker = tmp_path / "dirty_shutdown.flag"
        marker.write_text("1", encoding="utf-8")
        assert check_and_remark_dirty_shutdown(marker) is True
        assert marker.exists()  # 확인 후에도 이번 실행 보호용으로 다시 기록됨

    def test_clear_removes_marker(self, tmp_path: Path):
        """정상 quit 확정 시 clear_dirty_shutdown이 표식을 지운다(비동기 daemon 스레드,
        설계 리뷰 12차 B-1 — 짧은 폴링으로 최종 결과만 확인)."""
        from knowmate.app.lifecycle import check_and_remark_dirty_shutdown, clear_dirty_shutdown

        marker = tmp_path / "dirty_shutdown.flag"
        check_and_remark_dirty_shutdown(marker)  # 시작 시 기록
        assert marker.exists()
        clear_dirty_shutdown(marker)  # 정상 종료 시 제거 요청(즉시 반환)
        assert _wait_until(lambda: not marker.exists()), "marker not removed in time"

    def test_clear_returns_quickly_when_delete_is_fast(self, tmp_path: Path):
        """정상적인(빠른) 삭제라면 join 상한(1초, 리뷰14 M-2)을 기다리지 않고
        곧바로 반환한다."""
        import time
        from knowmate.app.lifecycle import check_and_remark_dirty_shutdown, clear_dirty_shutdown

        marker = tmp_path / "dirty_shutdown.flag"
        check_and_remark_dirty_shutdown(marker)

        start = time.monotonic()
        clear_dirty_shutdown(marker)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"clear_dirty_shutdown blocked for {elapsed}s"

    def test_clear_bounded_by_join_timeout_when_delete_hangs(self):
        """리뷰14 M-2: 삭제가 daemon 스레드에만 맡겨져 대기 없이 반환하면, 호출 직후
        인터프리터가 곧바로 종료될 때 스레드가 완료 전에 잘려 정상 종료에서도 삭제가
        보장되지 않는다는 지적을 받아, 최대 `_CLEAR_DIRTY_JOIN_TIMEOUT_SEC`(1초) 동안은
        join하도록 수정했다 — 삭제가 오래 걸려도 그 상한을 크게 넘겨 블록되지는
        않음(무기한 대기가 아님)을 확인한다."""
        import time
        from knowmate.app import lifecycle

        class _SlowPath:
            def unlink(self, missing_ok=True):
                time.sleep(5.0)

        start = time.monotonic()
        lifecycle.clear_dirty_shutdown(_SlowPath())
        elapsed = time.monotonic() - start
        assert lifecycle._CLEAR_DIRTY_JOIN_TIMEOUT_SEC <= elapsed < 3.0, (
            f"join 상한 근처에서 반환해야 하는데 {elapsed}s 걸림"
        )

    def test_clear_missing_marker_is_noop(self, tmp_path: Path):
        """표식이 없는 상태에서 clear를 호출해도 예외가 없다."""
        from knowmate.app.lifecycle import clear_dirty_shutdown
        clear_dirty_shutdown(tmp_path / "no_such_marker.flag")

    def test_full_cycle_dirty_then_clean(self, tmp_path: Path):
        """실행1: 시작(마크) → 강제종료(표식 안 지움). 실행2: 시작 시 dirty=True 확인
        → 정상 종료로 clear → 실행3: 시작 시 dirty=False."""
        from knowmate.app.lifecycle import check_and_remark_dirty_shutdown, clear_dirty_shutdown
        marker = tmp_path / "dirty_shutdown.flag"

        # 실행1 시작 (첫 실행이라 dirty=False), 강제 종료라고 가정 — clear 호출 안 함
        assert check_and_remark_dirty_shutdown(marker) is False

        # 실행2 시작 — 실행1이 표식을 못 지웠으므로 dirty=True
        assert check_and_remark_dirty_shutdown(marker) is True
        # 실행2는 정상 종료
        clear_dirty_shutdown(marker)
        assert _wait_until(lambda: not marker.exists()), "marker not removed in time"

        # 실행3 시작 — 실행2가 정상 종료했으므로 dirty=False
        assert check_and_remark_dirty_shutdown(marker) is False


class TestStopWorker:
    """정상 종료 → 강제 종료 → 하드 종료 단계적 강제. 워커가 COM에 멈춰도
    트레이 [종료]가 프로세스를 반드시 끝내도록 보장하는 로직."""

    class _FakeWorker:
        def __init__(self, running=True, wait_results=None):
            self._running = running
            self._wait_results = list(wait_results or [])
            self.cancelled = False
            self.terminated = False

        def isRunning(self):
            return self._running

        def cancel(self):
            self.cancelled = True

        def wait(self, ms):
            return self._wait_results.pop(0) if self._wait_results else True

        def terminate(self):
            self.terminated = True

    def test_graceful_stop_no_terminate_no_hardexit(self):
        """첫 wait에 정상 종료되면 terminate·하드종료 없이 끝나고 False(강제 아님)를 반환한다."""
        from knowmate.app.lifecycle import stop_worker
        w = self._FakeWorker(running=True, wait_results=[True])
        hard = []
        result = stop_worker(w, hard_exit=lambda c: hard.append(c))
        assert w.cancelled and not w.terminated and hard == []
        assert result is False

    def test_terminate_when_graceful_times_out(self):
        """정상 종료 실패 → terminate 후 성공하면 하드 종료는 안 하지만, terminate()가
        쓰였으므로 True(강제 중단됨)를 반환한다(설계 리뷰 15차 B-1 — 호출부가 이 값을
        finalize_shutdown(force_hard_exit=True)로 넘겨 quit 대신 hard_exit로 수렴시켜야
        한다. terminate()로 멈춘 스레드는 임의 지점에서 강제 중단된 것이라 락을 쥔 채
        죽었을 수 있어 "정상 종료"로 취급할 수 없다)."""
        from knowmate.app.lifecycle import stop_worker
        w = self._FakeWorker(running=True, wait_results=[False, True])
        hard = []
        result = stop_worker(w, hard_exit=lambda c: hard.append(c))
        assert w.terminated and hard == []
        assert result is True

    def test_hard_exit_when_terminate_also_fails(self):
        """정상·강제 모두 실패하면 프로세스 하드 종료(0)를 호출하고 True를 반환한다."""
        from knowmate.app.lifecycle import stop_worker
        w = self._FakeWorker(running=True, wait_results=[False, False])
        hard = []
        result = stop_worker(w, hard_exit=lambda c: hard.append(c))
        assert w.terminated and hard == [0]
        assert result is True

    def test_noop_when_not_running(self):
        """이미 멈춘 워커는 아무것도 하지 않고 False를 반환한다."""
        from knowmate.app.lifecycle import stop_worker
        w = self._FakeWorker(running=False)
        hard = []
        result = stop_worker(w, hard_exit=lambda c: hard.append(c))
        assert not w.cancelled and hard == []
        assert result is False

    def test_noop_when_none(self):
        """worker가 None이어도 예외 없이 통과하고 False를 반환한다."""
        from knowmate.app.lifecycle import stop_worker
        result = stop_worker(None, hard_exit=lambda c: (_ for _ in ()).throw(AssertionError("호출되면 안 됨")))
        assert result is False


class TestFinalizeShutdown:
    """종료 최종 판정 — 워커 비실행 확인 시 quit, 실행 중·판정 불가 시 hard_exit.
    quit과 hard_exit는 정확히 하나만 호출되어야 한다(설계 A-0001/ADR-0001)."""

    class _FakeWorker:
        def __init__(self, running=False, raise_on_is_running=False):
            self._running = running
            self._raise = raise_on_is_running

        def isRunning(self):
            if self._raise:
                raise RuntimeError("워커 상태 조회 실패")
            return self._running

    def test_quit_when_worker_none(self):
        """worker가 None이면 quit만 호출된다."""
        from knowmate.app.lifecycle import finalize_shutdown
        quit_calls, hard_calls = [], []
        finalize_shutdown(
            None, quit_fn=lambda: quit_calls.append(1),
            hard_exit=lambda c: hard_calls.append(c),
        )
        assert quit_calls == [1] and hard_calls == []

    def test_quit_when_worker_confirmed_stopped(self):
        """isRunning()이 False로 확인되면 quit만 호출된다."""
        from knowmate.app.lifecycle import finalize_shutdown
        w = self._FakeWorker(running=False)
        quit_calls, hard_calls = [], []
        finalize_shutdown(
            w, quit_fn=lambda: quit_calls.append(1),
            hard_exit=lambda c: hard_calls.append(c),
        )
        assert quit_calls == [1] and hard_calls == []

    def test_hard_exit_when_worker_still_running(self):
        """워커가 여전히 실행 중이면 hard_exit만 호출된다(quit 안 함)."""
        from knowmate.app.lifecycle import finalize_shutdown
        w = self._FakeWorker(running=True)
        quit_calls, hard_calls = [], []
        finalize_shutdown(
            w, quit_fn=lambda: quit_calls.append(1),
            hard_exit=lambda c: hard_calls.append(c),
        )
        assert hard_calls == [0] and quit_calls == []

    def test_hard_exit_when_is_running_raises(self):
        """isRunning() 조회 자체가 실패(판정 불가)하면 보수적으로 hard_exit만 호출된다
        (하드 종료 경로는 파일 I/O가 전혀 없어야 함, 리뷰11 B-1)."""
        from knowmate.app.lifecycle import finalize_shutdown
        w = self._FakeWorker(raise_on_is_running=True)
        quit_calls, hard_calls = [], []
        finalize_shutdown(
            w, quit_fn=lambda: quit_calls.append(1),
            hard_exit=lambda c: hard_calls.append(c),
        )
        assert hard_calls == [0] and quit_calls == []

    def test_force_hard_exit_overrides_confirmed_stopped(self):
        """리뷰15 B-1: isRunning()이 False로 확인돼도 force_hard_exit=True(stop_worker가
        terminate()를 사용했음을 의미)면 quit 대신 hard_exit로 수렴한다 — terminate()로
        강제 중단된 스레드는 락을 쥔 채 죽었을 수 있어 "정상 종료"로 취급할 수 없다."""
        from knowmate.app.lifecycle import finalize_shutdown
        w = self._FakeWorker(running=False)
        quit_calls, hard_calls = [], []
        finalize_shutdown(
            w, quit_fn=lambda: quit_calls.append(1),
            hard_exit=lambda c: hard_calls.append(c),
            force_hard_exit=True,
        )
        assert hard_calls == [0] and quit_calls == []

    def test_force_hard_exit_false_allows_normal_quit(self):
        """force_hard_exit=False(기본값, 기존 동작)면 워커 비실행 확인 시 여전히 quit된다."""
        from knowmate.app.lifecycle import finalize_shutdown
        w = self._FakeWorker(running=False)
        quit_calls, hard_calls = [], []
        finalize_shutdown(
            w, quit_fn=lambda: quit_calls.append(1),
            hard_exit=lambda c: hard_calls.append(c),
            force_hard_exit=False,
        )
        assert quit_calls == [1] and hard_calls == []

    def test_finalize_shutdown_never_clears_dirty_marker(self):
        """리뷰13 M-1: finalize_shutdown은 표식 해제를 전혀 하지 않는다 — quit()은
        종료를 요청할 뿐 완료를 보장하지 않으므로, 표식 해제는 app.exec() 정상 반환
        후 main()의 정상 반환 경로에서만 수행한다."""
        from knowmate.app.lifecycle import finalize_shutdown
        import inspect
        assert "clear_dirty" not in inspect.signature(finalize_shutdown).parameters


# ============================================================
# TestComWatchdog — COM 행오버 워치독 (PyQt6 무관)
# ============================================================

class TestComWatchdog:
    """세대 토큰·발화/해제 경합·daemon 타이머를 검증한다. 실제 COM/kill 없이
    terminate_fn·timer_factory를 주입해 로직만 확인."""

    class _FakeTimer:
        """start/cancel을 기록하고, fire()로 콜백을 수동 발화하는 가짜 타이머."""
        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback
            self.started = False
            self.cancelled = False

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def fire(self):
            self.callback()

    def _make(self, terminate_results=None):
        """ComWatchdog + 마지막 생성 타이머 캡처 + terminate 호출 기록."""
        from knowmate.collector.com_watchdog import ComWatchdog
        timers = []
        term_calls = []
        results = list(terminate_results or [])

        def _terminate(exe):
            term_calls.append(exe)
            return results.pop(0) if results else 1

        def _factory(interval, cb):
            t = self._FakeTimer(interval, cb)
            timers.append(t)
            return t

        wd = ComWatchdog(terminate_fn=_terminate, timer_factory=_factory)
        return wd, timers, term_calls

    def test_arm_starts_timer_with_timeout(self):
        wd, timers, _ = self._make()
        wd.arm("EXCEL.EXE", 300.0)
        assert len(timers) == 1
        assert timers[0].interval == 300.0
        assert timers[0].started

    def test_fire_terminates_when_active(self):
        wd, timers, term = self._make(terminate_results=[2])
        wd.arm("EXCEL.EXE", 300.0)
        timers[0].fire()
        assert term == ["EXCEL.EXE"]
        assert wd.timeout_count == 1

    def test_disarm_prevents_fire(self):
        """정상 완료(disarm) 후 타이머가 뒤늦게 발화해도 종료하지 않는다."""
        wd, timers, term = self._make()
        wd.arm("EXCEL.EXE", 300.0)
        wd.disarm()
        assert timers[0].cancelled
        timers[0].fire()  # 취소됐어야 하지만 강제로 콜백을 불러도
        assert term == []  # active=False라 무시

    def test_stale_generation_does_not_fire(self):
        """이전 파일의 타이머가 다음 파일 처리 중 발화해도 오사살하지 않는다."""
        wd, timers, term = self._make()
        wd.arm("EXCEL.EXE", 300.0)   # 파일1 (gen1)
        wd.disarm()
        wd.arm("WINWORD.EXE", 300.0)  # 파일2 (gen2)
        # 파일1의 타이머가 이제야 발화
        timers[0].fire()
        assert term == []  # gen 불일치 → 무시
        # 파일2 타이머는 정상 발화
        timers[1].fire()
        assert term == ["WINWORD.EXE"]

    def test_fire_only_once_per_arm(self):
        """한 번 발화 후에는 같은 세대에서 다시 발화해도 중복 종료하지 않는다."""
        wd, timers, term = self._make()
        wd.arm("EXCEL.EXE", 300.0)
        timers[0].fire()
        timers[0].fire()
        assert term == ["EXCEL.EXE"]  # 1회만

    def test_no_terminate_count_when_nothing_killed(self):
        """종료 대상이 0개면 timeout_count를 올리지 않는다."""
        wd, timers, term = self._make(terminate_results=[0])
        wd.arm("EXCEL.EXE", 300.0)
        timers[0].fire()
        assert term == ["EXCEL.EXE"]
        assert wd.timeout_count == 0

    def test_default_timer_is_daemon(self):
        """기본 타이머는 daemon이어야 한다(프로세스 종료를 막지 않도록)."""
        from knowmate.collector.com_watchdog import ComWatchdog
        t = ComWatchdog._default_timer(300.0, lambda: None)
        assert t.daemon is True
        t.cancel()
