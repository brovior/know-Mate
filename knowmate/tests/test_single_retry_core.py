"""Targeted collector scope and failure mailbox regression tests."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowmate.collector import failure_state
from knowmate.collector.failure_updates import FailureUpdates
from knowmate.collector.scanner import validate_retry_target
from knowmate.collector.scheduler import CollectorWorker
from knowmate.collector.state import load_state, save_state
from knowmate.rag.embedding import EmbeddingClient
from knowmate.rag.indexer import Indexer
from knowmate.secure.fake_reader import FakeReader


def _record(path: Path) -> failure_state.FailureRecord:
    """Make bounded metadata for a synthetic failed document."""
    records = {}
    failure_state.note_failure(records, str(path), failure_state.KIND_UNKNOWN_TRANSIENT,
                               "read", None, 1.0, 1, 1000.0)
    return records[str(path)]


def _worker(tmp_path: Path, folder: Path, indexer=None, **kwargs) -> CollectorWorker:
    """Create an isolated worker with no real Office cleanup."""
    return CollectorWorker(
        config={"collector": {"watch_folders": [str(folder)]},
                "cleanup": {"dry_run": True, "max_delete_ratio": 0.3}},
        indexer=indexer or SimpleNamespace(), extractor=FakeReader(),
        state_file=tmp_path / "state.json", failure_file=tmp_path / "failure.json",
        purge_meta_file=tmp_path / "purge.json", com_cleanup_fn=lambda **_kw: None,
        **kwargs,
    )


def test_direct_retry_preserves_other_documents_pending_deletes_and_purge(tmp_path, monkeypatch):
    """A selected retry cannot process new, changed, failed or orphan siblings."""
    folder = tmp_path / "docs"
    folder.mkdir()
    sibling = folder / "existing.txt"
    sibling.write_text("existing synthetic document", encoding="utf-8")
    indexer = Indexer(db_path=tmp_path / "db", embed_client=EmbeddingClient(
        base_url="http://localhost", host_header="test", fake=True))
    worker = _worker(tmp_path, folder, indexer)
    worker.run()
    state = load_state(worker._state_file)
    state[str(sibling)]["pending_delete_chunk_ids"] = ["unrelated-pending-id"]
    save_state(worker._state_file, state)
    sibling.write_text("modified sibling must remain untouched", encoding="utf-8")
    new_file = folder / "new.txt"
    new_file.write_text("new sibling must remain untouched", encoding="utf-8")
    selected = folder / "selected.txt"
    selected.write_text("selected synthetic source", encoding="utf-8")
    records = {str(selected): _record(selected), str(sibling): _record(sibling)}
    failure_state.save_failures(worker._failure_file, records)
    before_purge = worker._purge_meta_file.read_bytes() if worker._purge_meta_file.exists() else None
    calls = []
    original = worker._extractor.extract

    def extract(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(worker._extractor, "extract", extract)
    monkeypatch.setattr("knowmate.collector.scheduler.iter_scan_folder", lambda *_a, **_kw: pytest.fail("global scan"))
    monkeypatch.setattr(worker, "_purge_removed_folders", lambda *_a, **_kw: pytest.fail("global purge"))
    assert worker.request_single_retry(str(selected)) == "ok"
    worker.run()
    assert calls == [str(selected)]
    after = load_state(worker._state_file)
    assert after[str(sibling)] == state[str(sibling)]
    assert str(new_file) not in after
    assert str(selected) in after
    assert failure_state.load_failures(worker._failure_file) == {str(sibling): records[str(sibling)]}
    assert (worker._purge_meta_file.read_bytes() if worker._purge_meta_file.exists() else None) == before_purge
    assert worker.drain_failure_updates()["terminal"]["scope"] == "single"


def test_mailbox_coalesces_ten_thousand_mutations_and_copies_records(tmp_path):
    """Fast outcomes occupy one pending entry per path without Qt emissions."""
    source = tmp_path / "a.txt"
    session = FailureUpdates(tmp_path / "failures.json")
    records = session.begin("all")
    for count in range(10000):
        records[str(source)] = replace(_record(source), consecutive_failures=count)
    first = session.drain()
    assert first["revision"] == 10000
    assert len(first["changes"]) == 1
    first["changes"][str(source)].consecutive_failures = -1
    assert records[str(source)].consecutive_failures == 9999
    assert session.drain()["changes"] == {}


def test_dirty_failure_map_is_not_reloaded_and_persist_only_skips_office(tmp_path, monkeypatch):
    """A failed write retains state and a later save-only run never indexes."""
    worker = _worker(tmp_path, tmp_path)
    source = tmp_path / "failed.txt"
    monkeypatch.setattr(failure_state, "save_failures", lambda *_a: False)

    def cycle():
        worker._failure_updates.records[str(source)] = _record(source)

    monkeypatch.setattr(worker, "_run_cycle", cycle)
    worker.run()
    assert worker.failure_persistence_pending
    assert worker.request_single_retry(str(source)) == "persistence_pending"
    assert worker.request_failure_persist()
    monkeypatch.setattr(worker, "_run_cycle", lambda: pytest.fail("indexing in persist-only"))
    monkeypatch.setattr(worker, "_cleanup_com_apps", lambda *_a: pytest.fail("Office in persist-only"))
    monkeypatch.setattr(failure_state, "save_failures", lambda _path, records: str(source) in records)
    worker.run()
    envelope = worker.drain_failure_updates()
    assert envelope["baseline"][str(source)] == _record(source)
    assert envelope["terminal"] == {"status": "success", "scope": "persist",
                                     "message": "실패 이력 저장 완료", "persisted": True}
    assert not worker.failure_persistence_pending


@pytest.mark.parametrize("failure", ["scope", "cycle"])
def test_outer_failure_still_has_one_terminal(tmp_path, monkeypatch, failure):
    """Initialization and cycle exceptions both leave a drainable terminal."""
    worker = _worker(tmp_path, tmp_path)

    def fail(*_args):
        raise RuntimeError("synthetic failure")

    if failure == "scope":
        monkeypatch.setattr("knowmate.secure.com_scope.ComScope.__enter__", fail)
        worker.run()
    else:
        monkeypatch.setattr(worker, "_run_cycle", fail)
        worker.run()
    assert worker.drain_failure_updates()["terminal"]["status"] == "error"
    assert not worker._run_session_active
    assert worker._active_target is None


@pytest.mark.parametrize("failure", ["load", "scope", "persist", "flush"])
def test_native_thread_contains_initialization_and_finalization_errors(tmp_path, monkeypatch, failure):
    """No Python exception may escape QThread.run and abort the process."""
    worker = _worker(tmp_path, tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic native worker failure")

    if failure == "load":
        monkeypatch.setattr(failure_state, "load_failures", fail)
    elif failure == "scope":
        monkeypatch.setattr("knowmate.secure.com_scope.ComScope.__enter__", fail)
    elif failure == "persist":
        records = worker._failure_updates.begin("all")
        records[str(tmp_path / "a.txt")] = _record(tmp_path / "a.txt")
        assert worker.request_failure_persist()
        monkeypatch.setattr(failure_state, "save_failures", fail)
    else:
        def cycle():
            worker._document_state_flush = fail
        monkeypatch.setattr(worker, "_run_cycle", cycle)
    worker.start()
    assert worker.wait(5000)
    terminal = worker.drain_failure_updates()["terminal"]
    assert terminal["status"] == "error"
    if failure == "persist":
        assert "저장하지 못했습니다" in terminal["message"]
        assert "저장 완료" not in terminal["message"]
    assert not worker._run_session_active
    assert not worker.isRunning()


def test_target_drm_idle_rejection_does_not_set_retry_or_touch_db(tmp_path, monkeypatch):
    """The direct producer obeys the ordinary DRM idle admission gate."""
    source = tmp_path / "private.pptx"
    source.write_bytes(b"synthetic wrapped source")
    worker = _worker(tmp_path, tmp_path, get_idle_seconds=lambda: 1000)
    records = {str(source): _record(source)}
    failure_state.save_failures(worker._failure_file, records)
    monkeypatch.setattr("knowmate.collector.scheduler._is_drm_suspected", lambda _path: True)
    assert worker.request_single_retry(str(source)) == "ok"
    worker.run()
    assert worker.drain_failure_updates()["terminal"]["status"] == "drm_idle"
    assert failure_state.load_failures(worker._failure_file) == records


def test_target_recovery_marker_requires_general_cycle_without_flag_change(tmp_path):
    """A partial prior add cannot trigger global recovery from a single retry."""
    source = tmp_path / "selected.txt"
    source.write_text("synthetic", encoding="utf-8")
    worker = _worker(tmp_path, tmp_path, SimpleNamespace(table=SimpleNamespace(count_rows=lambda: 0)))
    records = {str(source): _record(source)}
    failure_state.save_failures(worker._failure_file, records)
    marker = tmp_path / "state.recovery.json"
    marker.write_text('{"active": true}', encoding="utf-8")
    assert worker.request_single_retry(str(source)) == "ok"
    worker.run()
    assert worker.drain_failure_updates()["terminal"]["status"] == "recovery_required"
    assert failure_state.load_failures(worker._failure_file) == records
    assert marker.exists()


def test_direct_admission_uses_path_components_and_rechecks_exclusion(tmp_path):
    """A neighboring root prefix and excluded targets are not admitted."""
    folder = tmp_path / "docs"
    neighbor = tmp_path / "docs2"
    folder.mkdir()
    neighbor.mkdir()
    source = neighbor / "a.txt"
    source.write_text("synthetic", encoding="utf-8")
    assert validate_retry_target(str(source), [str(folder)])[0] == "out_of_scope"
    target = folder / "a.txt"
    target.write_text("synthetic", encoding="utf-8")
    assert validate_retry_target(str(target), [str(folder)])[0] == "ok"
    assert validate_retry_target(str(target), [str(folder)], exclude_files=frozenset({str(target).replace('\\', '/').casefold()}))[0] == "excluded"
