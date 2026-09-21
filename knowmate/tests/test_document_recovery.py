"""Crash-safe document generation and batched state checkpoint regressions."""
from __future__ import annotations

from pathlib import Path

from knowmate.collector.state import load_state
from knowmate.collector import failure_state
from knowmate.collector.scheduler import CollectorWorker
from knowmate.collector.cleanup import CleanupManager
from knowmate.rag.embedding import EmbeddingClient
from knowmate.rag.indexer import DOC_INDEX_VERSION, Indexer
from knowmate.secure.fake_reader import FakeReader


def _config(folder: Path, *, flush_docs: int = 50) -> dict:
    return {
        "collector": {
            "watch_folders": [str(folder)], "idle_seconds": 60,
            "state_flush_docs": flush_docs, "state_flush_seconds": 3600,
        },
        "cleanup": {"dry_run": True, "max_delete_ratio": 0.30},
        "chunking": {"chunk_size": 400, "overlap": 80},
    }


def _indexer(tmp_path: Path) -> Indexer:
    return Indexer(
        db_path=tmp_path / "db",
        embed_client=EmbeddingClient(base_url="http://localhost", host_header="test", fake=True),
    )


def _worker(tmp_path: Path, folder: Path, indexer: Indexer) -> CollectorWorker:
    return CollectorWorker(
        config=_config(folder), indexer=indexer, extractor=FakeReader(),
        state_file=tmp_path / "index_state.json", purge_meta_file=tmp_path / "purge.json",
        failure_file=tmp_path / "failure.json",
    )


def test_post_add_exception_is_recovered_without_duplicate_rows(tmp_path: Path, monkeypatch) -> None:
    """An add which committed before raising is recovered before another add."""
    folder = tmp_path / "docs"
    folder.mkdir()
    source = folder / "one.txt"
    source.write_text("committed before state", encoding="utf-8")
    indexer = _indexer(tmp_path)
    worker = _worker(tmp_path, folder, indexer)
    original = indexer.index_file

    def commit_then_raise(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated uncertain append outcome")

    monkeypatch.setattr(indexer, "index_file", commit_then_raise)
    worker.run()
    committed_rows = indexer.table.count_rows()
    assert committed_rows > 0
    assert (tmp_path / "index_state.recovery.json").exists()

    monkeypatch.setattr(indexer, "index_file", original)
    recovered_worker = _worker(tmp_path, folder, indexer)
    recovered_worker.request_failure_retry()  # emulate restart after abrupt termination
    recovered_worker.run()

    assert indexer.table.count_rows() == committed_rows
    state = load_state(tmp_path / "index_state.json")
    assert state[str(source)]["revision"]
    assert state[str(source)]["generation_id"]
    assert not (tmp_path / "index_state.recovery.json").exists()
    assert str(source) not in failure_state.load_failures(tmp_path / "failure.json")

    # Marker retirement means an unchanged idle cycle does not repeat the
    # full metadata reconciliation.
    calls = []
    original_recovery = indexer.recover_document_state
    monkeypatch.setattr(indexer, "recover_document_state", lambda: calls.append(1) or original_recovery())
    modified_worker = _worker(tmp_path, folder, indexer)
    modified_worker._config["collector"]["state_flush_docs"] = 10
    modified_worker.run()
    assert calls == []


def test_multiple_current_generations_choose_one_and_remove_loser(tmp_path: Path) -> None:
    """Recovery picks a deterministic complete generation and never deletes it."""
    source = tmp_path / "same.txt"
    source.write_text("same", encoding="utf-8")
    indexer = _indexer(tmp_path)
    stat = source.stat()
    uid = indexer.document_uid(str(source))
    revision = indexer.document_revision(
        str(source), mtime=stat.st_mtime, mtime_ns=stat.st_mtime_ns, size=stat.st_size,
    )
    first = indexer.index_file(str(source), "same", stat.st_mtime, "local", doc_uid=uid, revision=revision)
    second = indexer.index_file(str(source), "same", stat.st_mtime, "local", doc_uid=uid, revision=revision)

    recovered = indexer.recover_generation(uid, revision)
    assert recovered is not None
    assert set(recovered["chunk_ids"]) in (set(first), set(second))
    assert set(recovered["old_chunk_ids"]) == (set(first) | set(second)) - set(recovered["chunk_ids"])


def test_soft_deleted_or_incomplete_rows_are_not_recovered_as_current(tmp_path: Path) -> None:
    """Completeness needs every active index exactly once."""
    source = tmp_path / "bad.txt"
    source.write_text("bad", encoding="utf-8")
    indexer = _indexer(tmp_path)
    stat = source.stat()
    uid = indexer.document_uid(str(source))
    revision = indexer.document_revision(
        str(source), mtime=stat.st_mtime, mtime_ns=stat.st_mtime_ns, size=stat.st_size,
    )
    ids = indexer.index_file(str(source), "bad", stat.st_mtime, "local", doc_uid=uid, revision=revision)
    indexer.table.update(where=f"chunk_id = '{ids[0]}'", values={"is_deleted": True})
    assert indexer.recover_generation(uid, revision) is None


def test_document_state_is_batched_and_flushes_at_cycle_end(tmp_path: Path, monkeypatch) -> None:
    """A 60-document run writes state at the configured 50-document boundary and end."""
    folder = tmp_path / "docs"
    folder.mkdir()
    for number in range(60):
        (folder / f"{number}.txt").write_text(str(number), encoding="utf-8")
    indexer = _indexer(tmp_path)
    worker = _worker(tmp_path, folder, indexer)
    worker._config["collector"]["state_flush_docs"] = 50

    import knowmate.collector.scheduler as scheduler
    original_save = scheduler.save_state
    writes: list[Path] = []

    def recording_save(path, state):
        writes.append(Path(path))
        return original_save(path, state)

    monkeypatch.setattr(scheduler, "save_state", recording_save)
    worker.run()

    main_writes = [path for path in writes if path.name == "index_state.json"]
    assert len(main_writes) == 2
    assert len(load_state(tmp_path / "index_state.json")) == 60


def test_modified_documents_use_the_same_bounded_state_checkpointing(tmp_path: Path, monkeypatch) -> None:
    """Replacement generations do not restore a per-modification JSON write."""
    folder = tmp_path / "docs"
    folder.mkdir()
    for number in range(12):
        (folder / f"{number}.txt").write_text(f"old {number}", encoding="utf-8")
    indexer = _indexer(tmp_path)
    worker = _worker(tmp_path, folder, indexer)
    worker._config["collector"]["state_flush_docs"] = 10
    worker.run()
    for number in range(12):
        (folder / f"{number}.txt").write_text(f"new content {number}", encoding="utf-8")

    import knowmate.collector.scheduler as scheduler
    original_save = scheduler.save_state
    writes: list[Path] = []
    monkeypatch.setattr(
        scheduler, "save_state",
        lambda path, state: (writes.append(Path(path)), original_save(path, state))[1],
    )
    modified_worker = _worker(tmp_path, folder, indexer)
    modified_worker._config["collector"]["state_flush_docs"] = 10
    modified_worker.run()
    assert len([path for path in writes if path.name == "index_state.json"]) == 2


def test_db_completeness_query_failure_never_calls_add(tmp_path: Path, monkeypatch) -> None:
    """Unknown DB state fails closed before extraction or a new generation."""
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "fail.txt").write_text("data", encoding="utf-8")
    indexer = _indexer(tmp_path)
    monkeypatch.setattr(indexer, "recover_generation", lambda *_args: (_ for _ in ()).throw(RuntimeError("db down")))
    _worker(tmp_path, folder, indexer).run()
    assert indexer.table.count_rows() == 0


def test_current_recovery_removes_mixed_legacy_rows(tmp_path: Path) -> None:
    """Exact current recovery also cleans NULL-generation legacy path rows."""
    folder = tmp_path / "docs"
    folder.mkdir()
    source = folder / "mixed.txt"
    source.write_text("current", encoding="utf-8")
    indexer = _indexer(tmp_path)
    _worker(tmp_path, folder, indexer).run()
    row = indexer.table.to_arrow().to_pylist()[0]
    legacy = dict(row)
    legacy["chunk_id"] = "legacy-row"
    legacy["doc_uid"] = None
    legacy["revision"] = None
    legacy["generation_id"] = None
    indexer.table.add([legacy])
    (tmp_path / "index_state.json").unlink()

    _worker(tmp_path, folder, indexer).run()
    rows = indexer.table.to_arrow().to_pylist()
    assert [item["chunk_id"] for item in rows] != ["legacy-row"]
    assert all(item["chunk_id"] != "legacy-row" for item in rows)


def test_mixed_soft_orphan_keeps_remaining_ids_for_second_cleanup(tmp_path: Path) -> None:
    """Partial hard deletion must not drop first-miss rows from recovered state."""
    folder = tmp_path / "docs"
    folder.mkdir()
    source = folder / "gone.txt"
    source.write_text("gone", encoding="utf-8")
    indexer = _indexer(tmp_path)
    stat = source.stat()
    uid = indexer.document_uid(str(source))
    revision = indexer.document_revision(str(source), mtime=stat.st_mtime, mtime_ns=stat.st_mtime_ns, size=stat.st_size)
    old = indexer.index_file(str(source), "old", stat.st_mtime, "local", doc_uid=uid, revision=revision)
    newer = indexer.index_file(str(source), "new", stat.st_mtime, "local", doc_uid=uid, revision=revision)
    indexer.delete_chunks(old)  # old is now on its second miss for cleanup
    source.unlink()
    state = {str(source): {"chunk_ids": [*old, *newer]}}
    cleanup = CleanupManager(indexer=indexer, max_delete_ratio=1.0, dry_run=False)

    cleanup.run([str(folder)], state)
    assert state[str(source)]["chunk_ids"] == newer
    cleanup.run([str(folder)], state)
    assert str(source) not in state


def test_empty_table_repair_retries_after_state_and_marker_save_failure(tmp_path: Path, monkeypatch) -> None:
    """A failed empty-table checkpoint leaves reset flags armed for next cycle."""
    folder = tmp_path / "docs"
    folder.mkdir()
    source = folder / "reset.txt"
    source.write_text("reindex me", encoding="utf-8")
    first = _indexer(tmp_path)
    _worker(tmp_path, folder, first).run()
    state_file = tmp_path / "index_state.json"
    assert load_state(state_file)[str(source)]["chunk_ids"]
    first.table.delete("true")
    reopened = _indexer(tmp_path)
    assert reopened.table_is_empty

    import knowmate.collector.scheduler as scheduler
    original_save = scheduler.save_state
    monkeypatch.setattr(scheduler, "save_state", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    _worker(tmp_path, folder, reopened).run()
    assert reopened.table.count_rows() == 0
    assert reopened.table_is_empty

    monkeypatch.setattr(scheduler, "save_state", original_save)
    _worker(tmp_path, folder, reopened).run()
    assert reopened.table.count_rows() > 0
    assert load_state(state_file)[str(source)]["chunk_ids"]
