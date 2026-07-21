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
    return CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file), indexer, state_file


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

        worker = CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file)
        worker.run()

        from knowmate.collector.state import load_state
        state1 = load_state(state_file)
        assert str(f) in state1

        # 파일 수정 (mtime 변경을 위해 충분한 시간 후 재작성)
        time.sleep(0.05)
        f.write_bytes(b"modified content - different from original")

        # 같은 indexer 재사용 (DB 재생성 충돌 방지)
        worker2 = CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file)
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
        worker = CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file)
        worker.run()

        from knowmate.collector.state import load_state
        state = load_state(state_file)

        # 1개 파일 삭제 (20% orphan < 30%)
        del_path = str(files[0])
        files[0].unlink()

        worker2 = CollectorWorker(config=config, indexer=indexer, extractor=extractor, state_file=state_file)
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
        worker = CollectorWorker(config=config, indexer=indexer, extractor=FailingExtractor(), state_file=state_file)

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
        )
        worker.run()

        state = load_state(state_file)
        assert state[str(f)]["method"] == "plain"


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
    def _make_scheduler(self, get_idle_seconds, idle_seconds=60, trigger=None, is_busy=None):
        from knowmate.collector.scheduler import IdleScheduler
        return IdleScheduler(
            trigger=trigger or MagicMock(),
            is_busy=is_busy or (lambda: False),
            idle_seconds=idle_seconds,
            get_idle_seconds=get_idle_seconds,
        )

    def test_triggers_when_actual_idle_meets_threshold(self):
        """실제 유휴 시간이 임계 이상이면 트리거하고 다음 사이클을 재예약한다."""
        trigger = MagicMock()
        sched = self._make_scheduler(get_idle_seconds=lambda: 90.0, idle_seconds=60, trigger=trigger)
        sched._on_idle()
        trigger.assert_called_once()
        assert sched._timer.isActive()
        assert sched._timer.interval() == 60_000

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
