"""Policy mutations use one background writer and durable exclusion settings."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowmate import config
from knowmate.collector.scheduler import CollectorWorker
from knowmate.collector.state import load_state, save_state
from knowmate.collector.mail_scan_state import normalize_path_key as mail_key


class MetadataTable:
    """Offer only a projected metadata query and record requested columns."""

    def __init__(self, rows: list[dict]) -> None:
        """Hold synthetic metadata with no document body or embedding."""
        self.rows = rows
        self.columns: list[str] = []

    def search(self) -> MetadataTable:
        """Return the query model used by the production aggregate."""
        return self

    def select(self, columns: list[str]) -> MetadataTable:
        """Reject unprojected text or vector access."""
        assert "text" not in columns and "vector" not in columns
        self.columns = columns
        return self

    def to_arrow(self) -> SimpleNamespace:
        """Return a detached scalar row projection."""
        rows = [{key: row[key] for key in self.columns} for row in self.rows]
        return SimpleNamespace(to_pylist=lambda: rows)


class DocumentIndex:
    """Model a path-specific deletion without touching original files."""

    def __init__(self, paths: list[str]) -> None:
        """Create one active metadata row per fake document."""
        self.table = MetadataTable([dict(file_path=path, scope="local", is_deleted=False) for path in paths])
        self.deleted: list[str] = []

    def delete_file_chunks(self, path: str) -> None:
        """Delete only the exact selected path's stored rows."""
        self.deleted.append(path)
        self.table.rows = [row for row in self.table.rows if row["file_path"] != path]


def make_worker(tmp_path: Path, monkeypatch, count: int = 3) -> tuple[CollectorWorker, list[str], dict]:
    """Prepare isolated settings, state and in-memory index metadata."""
    paths = [str(tmp_path / f"document-{index}.txt") for index in range(count)]
    cfg = {"collector": {"exclude_files": []}, "mail": {"enabled": True}}
    monkeypatch.setattr(config, "_cache", cfg)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    config._save_config(cfg)
    worker = CollectorWorker(
        config=cfg, indexer=DocumentIndex(paths), extractor=None,
        state_file=tmp_path / "state.json", failure_file=tmp_path / "failure.json",
        purge_meta_file=tmp_path / "purge.json",
    )
    save_state(worker._state_file, {path: {"chunk_ids": ["synthetic"]} for path in paths})
    return worker, paths, cfg


@pytest.mark.parametrize("count", [10, 150, 1000])
def test_bulk_policy_is_consumed_once_without_normal_scan_or_failure_write(tmp_path, monkeypatch, count):
    """A policy job cannot fall back to normal indexing on a second start."""
    worker, paths, cfg = make_worker(tmp_path, monkeypatch, count)
    monkeypatch.setattr(worker, "_run_cycle", lambda: pytest.fail("normal scan"))
    assert worker.request_policy_job("job", "exclude", tuple(paths)) == "ok"
    worker.start()
    assert worker.wait(10000)
    snapshot = worker.policy_job_snapshot()
    assert snapshot["terminal"] and snapshot["status"] == "success"
    assert snapshot["result"]["cleanup_succeeded"] == count
    assert snapshot["counts"]["doc_count"] == 0
    assert cfg["collector"]["exclude_files"] == paths
    assert config.read_exclude_files() == paths
    assert load_state(worker._state_file) == {}
    assert worker.policy_job_pending
    assert worker.request_single_retry(paths[0]) == "busy"
    worker.start()
    assert worker.wait(5000)
    assert worker._indexer.deleted == paths
    assert worker.policy_job_snapshot()["revision"] == snapshot["revision"]
    assert not worker._failure_file.exists()
    assert worker.acknowledge_policy_job("job")
    assert not worker.policy_job_pending
    assert len(json.dumps(snapshot)) < 2500


def test_config_save_failure_has_no_deletion_and_retains_original_checkpoint(tmp_path, monkeypatch):
    """An unsuccessful durable policy cannot authorize deletion."""
    worker, paths, cfg = make_worker(tmp_path, monkeypatch)
    before = worker._state_file.read_bytes()
    monkeypatch.setattr(config, "update_exclude_files", lambda _paths: (_ for _ in ()).throw(OSError("synthetic")))
    worker.request_policy_job("job", "exclude", tuple(paths))
    worker.run()
    result = worker.policy_job_snapshot()["result"]
    assert result["settings_saved"] is False and not result["policy_unknown"]
    assert result["error_code"] == "config_save_failed"
    assert worker._indexer.deleted == []
    assert cfg["collector"]["exclude_files"] == []
    assert worker._state_file.read_bytes() == before


def test_unknown_save_stays_blocked_after_join_then_readonly_reconcile(tmp_path, monkeypatch):
    """A detached thread ending does not clear uncertain durable settings."""
    worker, paths, _ = make_worker(tmp_path, monkeypatch)
    original_read = config.read_exclude_files
    original_save = config.update_exclude_files
    monkeypatch.setattr(config, "update_exclude_files", lambda _paths: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(config, "read_exclude_files", lambda: (_ for _ in ()).throw(OSError()))
    worker.request_policy_job("job", "exclude", tuple(paths))
    worker.start()
    assert worker.wait(5000)
    assert worker.policy_unknown
    assert worker.acknowledge_policy_job("job")
    assert worker.policy_unknown
    assert worker.request_single_retry(paths[0]) == "policy_unconfirmed"
    assert worker.request_policy_job("blocked", "exclude", tuple(paths)) == "policy_unconfirmed"
    monkeypatch.setattr(config, "read_exclude_files", original_read)
    monkeypatch.setattr(config, "update_exclude_files", original_save)
    assert worker.request_policy_reconcile("read") == "ok"
    worker.run()
    assert worker.policy_job_snapshot()["status"] == "reconciled"
    assert not worker.policy_unknown
    assert worker._indexer.deleted == []


def test_cancel_during_save_keeps_applied_policy_without_deletion(tmp_path, monkeypatch):
    """A cancellation cannot undo a replace already in progress."""
    worker, paths, cfg = make_worker(tmp_path, monkeypatch)
    save = config.update_exclude_files

    def save_then_cancel(items: list[str]) -> None:
        """Publish durable settings before the cancellation can be observed."""
        save(items)
        worker.cancel()

    monkeypatch.setattr(config, "update_exclude_files", save_then_cancel)
    worker.request_policy_job("job", "exclude", tuple(paths))
    worker.run()
    result = worker.policy_job_snapshot()["result"]
    assert result["cancelled"] and result["settings_saved"] is True
    assert result["not_attempted"] == len(paths)
    assert worker._indexer.deleted == []
    assert cfg["collector"]["exclude_files"] == paths


def test_partial_document_failure_retains_failed_state(tmp_path, monkeypatch):
    """Only successfully removed paths may be removed from durable state."""
    worker, paths, cfg = make_worker(tmp_path, monkeypatch)
    delete = worker._indexer.delete_file_chunks

    def fail_one(path: str) -> None:
        """Fail one mutation while allowing other document deletions."""
        if path == paths[1]:
            raise OSError("synthetic")
        delete(path)

    monkeypatch.setattr(worker._indexer, "delete_file_chunks", fail_one)
    worker.request_policy_job("job", "exclude", tuple(paths))
    worker.run()
    result = worker.policy_job_snapshot()["result"]
    assert result["status"] == "partial"
    assert result["delete_failed"] == 1 and result["succeeded"] == 2
    assert list(load_state(worker._state_file)) == [paths[1]]
    assert cfg["collector"]["exclude_files"] == paths


def test_noop_unexclude_still_invalidates_reconciled_mail_key(tmp_path, monkeypatch):
    """A stale reconciliation proof cannot survive an unexclude no-op."""
    worker, _, _ = make_worker(tmp_path, monkeypatch)
    mail = str(tmp_path / "one.eml")
    worker._mail_exclusions_reconciled.add(mail_key(mail))
    worker.request_policy_job("job", "unexclude", (mail,))
    worker.run()
    assert worker.policy_job_snapshot()["result"]["changed"] == 0
    assert mail_key(mail) not in worker._mail_exclusions_reconciled


def test_active_native_policy_cannot_be_acknowledged(tmp_path, monkeypatch):
    """A blocked delete keeps ownership even while GUI can inspect metadata."""
    worker, paths, _ = make_worker(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    delete = worker._indexer.delete_file_chunks

    def slow_delete(path: str) -> None:
        """Hold native work at a controllable barrier."""
        entered.set()
        assert release.wait(5)
        delete(path)

    monkeypatch.setattr(worker._indexer, "delete_file_chunks", slow_delete)
    worker.request_policy_job("job", "exclude", tuple(paths))
    worker.start()
    try:
        assert entered.wait(3)
        assert not worker.acknowledge_policy_job("job")
        assert not worker.clear_policy_command("job")
        assert worker.policy_job_snapshot()["stage"] == "documents"
        assert worker.take_policy_cache()["failure_records"] == {}
        assert worker.take_policy_cache() is None
    finally:
        release.set()
        assert worker.wait(5000)


def test_save_raises_after_replace_but_readback_confirms_applied_policy(tmp_path, monkeypatch):
    """A verified replacement is not reported as an unapplied exclusion."""
    worker, paths, cfg = make_worker(tmp_path, monkeypatch)
    save = config.update_exclude_files

    def saved_then_failed(items: list[str]) -> None:
        """Model an uncertain I/O return after durable data is already visible."""
        save(items)
        raise OSError("synthetic")

    monkeypatch.setattr(config, "update_exclude_files", saved_then_failed)
    worker.request_policy_job("job", "exclude", tuple(paths))
    worker.run()
    result = worker.policy_job_snapshot()["result"]
    assert result["settings_saved"] is True
    assert result["status"] == "success"
    assert worker._indexer.deleted == paths
    assert cfg["collector"]["exclude_files"] == paths


def test_cancel_before_save_and_empty_request_make_no_policy_mutation(tmp_path, monkeypatch):
    """Early cancellation and empty work cannot write settings or delete rows."""
    worker, paths, _ = make_worker(tmp_path, monkeypatch)
    before = (tmp_path / "config.yaml").read_bytes()
    worker.request_policy_job("empty", "exclude", ())
    worker.run()
    assert worker.policy_job_snapshot()["result"]["changed"] == 0
    assert worker.acknowledge_policy_job("empty")
    worker.request_policy_job("cancel", "exclude", tuple(paths))
    worker.cancel()
    worker.run()
    assert worker.policy_job_snapshot()["result"]["cancelled"]
    assert worker._indexer.deleted == []
    assert (tmp_path / "config.yaml").read_bytes() == before


def test_join_confirmation_is_required_even_after_running_flag_clears(tmp_path, monkeypatch):
    """Native TLS teardown may outlive the running flag; wait(0) remains authority."""
    worker, paths, _ = make_worker(tmp_path, monkeypatch)
    worker.request_policy_job("job", "exclude", tuple(paths))
    worker.run()
    monkeypatch.setattr(worker, "wait", lambda _ms: False)
    assert not worker.acknowledge_policy_job("job")
    assert worker.policy_job_pending


def test_config_publication_preserves_root_and_unrelated_sections(tmp_path, monkeypatch):
    """Readers retain either complete old or complete newly saved collector data."""
    _, paths, cfg = make_worker(tmp_path, monkeypatch)
    before_collector, before_mail, root_id = cfg["collector"], cfg["mail"], id(cfg)
    seen = []

    def save(updated: dict) -> None:
        """Observe the shared cache before the durable operation returns."""
        seen.append(list(cfg["collector"]["exclude_files"]))
        assert updated["collector"]["exclude_files"] == paths

    monkeypatch.setattr(config, "_save_config", save)
    config.update_exclude_files(paths)
    assert seen == [[]]
    assert id(cfg) == root_id
    assert cfg["mail"] is before_mail
    assert cfg["collector"] is not before_collector
    assert before_collector["exclude_files"] == []
    assert cfg["collector"]["exclude_files"] == paths
