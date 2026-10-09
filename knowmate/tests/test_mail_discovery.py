"""Metadata snapshot reuse and collector request boundary regressions."""
import os
from types import SimpleNamespace

import pytest

from knowmate.collector import failure_state, mail_scanner
from knowmate.collector.mail_discovery import MailDiscoverySnapshot
from knowmate.collector.mail_scan_state import load_mail_scan_state, normalize_path_key


@pytest.fixture
def discovery_scan(tmp_path, monkeypatch):
    """Run real metadata discovery with a body-free fake indexer."""
    watch = tmp_path / "watch"
    watch.mkdir()
    checks, commits, traversals = [], [], []
    current = set()
    indexer = SimpleNamespace(table_was_recreated=False, table_is_empty=False)

    def check(uid, mtime):
        checks.append((uid, mtime))
        return SimpleNamespace(
            state=SimpleNamespace(name="CURRENT" if (uid, mtime) in current else "MISSING"),
            old_chunk_ids=(),
        )

    def commit(parsed, mtime, **kwargs):
        commits.append((parsed["mail_uid"], mtime))
        current.add((parsed["mail_uid"], mtime))
        return ["chunk"]

    indexer.get_index_state, indexer.index_mail = check, commit
    indexer.delete_chunk_ids = lambda ids: ids
    monkeypatch.setattr(
        "knowmate.secure.mysingle_reader.parse_mail_file",
        lambda path: {"mail_uid": "eml:" + path, "source_file": path},
    )
    original = mail_scanner._iter_mail_files

    def enumerate_files(root, extensions):
        traversals.append(root)
        yield from original(root, extensions)

    monkeypatch.setattr(mail_scanner, "_iter_mail_files", enumerate_files)
    cfg = {"mail": {"max_mails_per_scan": 1, "discovery_refresh_seconds": 100}}
    snapshot = MailDiscoverySnapshot()
    clock = [0.0]
    state_file, failure_file = tmp_path / "state.json", tmp_path / "failures.json"

    def scan(**kwargs):
        return mail_scanner.run_mail_scan(
            kwargs.pop("roots", [str(watch)]), indexer, cfg,
            state_file=state_file, failure_file=failure_file,
            discovery_snapshot=snapshot, discovery_get_now=lambda: clock[0],
            **kwargs,
        )

    def add(name, mtime):
        path = watch / name
        path.touch()
        os.utime(path, (mtime, mtime))
        return path

    return SimpleNamespace(**locals())


def test_reuse_drains_backlog_without_enumeration_or_repeating_success(discovery_scan):
    h = discovery_scan
    h.add("a.eml", 20)
    h.add("b.eml", 10)
    assert h.scan()[0] == 1
    assert h.indexer.last_mail_scan_completion == mail_scanner.MailScanCompletion.REMAINING
    assert h.scan()[0] == 1
    assert h.scan()[0] == 0
    assert len(h.traversals) == 1
    assert len(h.commits) == 2
    assert h.indexer.last_mail_scan_completion == mail_scanner.MailScanCompletion.UNKNOWN
    assert all(set(item) == {"path", "path_key", "mtime", "size"} for item in h.snapshot.items)
    h.clock[0] = 100
    assert h.scan()[0] == 0
    assert len(h.traversals) == 2
    assert h.indexer.last_mail_scan_completion == mail_scanner.MailScanCompletion.EXHAUSTED


def test_selected_live_stat_updates_db_check_and_snapshot(discovery_scan):
    h = discovery_scan
    h.add("a.eml", 20)
    changed = h.add("b.eml", 10)
    h.scan()
    changed.write_bytes(b"metadata-test")
    os.utime(changed, (30, 30))
    h.scan()
    assert h.checks[-1][1] == 30
    saved = load_mail_scan_state(h.state_file)["files"][normalize_path_key(str(changed))]
    assert (saved["mtime"], saved["size"]) == (30, 13)
    h.scan()
    assert len(h.commits) == 2
    assert len(h.traversals) == 1


@pytest.mark.parametrize("change", ["manual", "roots", "extensions", "excluded", "config", "reset", "version", "schema", "disabled"])
def test_collection_changes_invalidate_snapshot(discovery_scan, monkeypatch, change):
    h = discovery_scan
    h.add("a.eml", 20)
    h.scan()
    kwargs = {}
    if change == "manual":
        kwargs["retry_failures"] = True
    elif change == "roots":
        other = h.tmp_path / "other"
        other.mkdir()
        kwargs["roots"] = [str(other)]
    elif change == "extensions":
        h.cfg["mail"]["extensions"] = [".mysingle"]
    elif change == "excluded":
        h.cfg["collector"] = {"exclude_files": [str(h.watch / "a.eml")]}
    elif change == "config":
        h.cfg["mail"]["max_mails_per_scan"] = 2
    elif change == "reset":
        h.indexer.table_was_recreated = True
    elif change == "version":
        monkeypatch.setattr("knowmate.rag.email_indexer.EMAIL_INDEX_VERSION", "changed")
    elif change == "schema":
        monkeypatch.setattr("knowmate.rag.email_indexer.EMAIL_SCHEMA", "changed")
    else:
        h.cfg["mail"]["discovery_refresh_seconds"] = 0
    h.scan(**kwargs)
    assert len(h.traversals) == 2


def test_deferred_failure_becomes_actionable_in_reused_snapshot(discovery_scan):
    h = discovery_scan
    path = h.add("failed.eml", 10)
    failures = {}
    failure_state.note_failure(failures, str(path), failure_state.KIND_UNKNOWN_TRANSIENT, "parse", None, 10, 0, 1000)
    failure_state.save_failures(h.failure_file, failures)
    assert h.scan(get_now=lambda: 1001)[0] == 0
    assert h.scan(get_now=lambda: 10**9)[0] == 1
    assert len(h.traversals) == 1


def test_partial_or_cancelled_discovery_is_not_reused_or_pruned(discovery_scan, monkeypatch):
    h = discovery_scan
    path = h.add("a.eml", 10)
    h.scan()
    saved = load_mail_scan_state(h.state_file)
    h.snapshot.invalidate()

    def inaccessible(root, _extensions):
        yield mail_scanner._MailTraversalError(root, root)

    monkeypatch.setattr(mail_scanner, "_iter_mail_files", inaccessible)
    h.scan()
    assert h.snapshot.refreshed_at is None
    assert load_mail_scan_state(h.state_file)["files"] == saved["files"]
    assert h.indexer.last_mail_scan_completion == mail_scanner.MailScanCompletion.UNKNOWN
    monkeypatch.setattr(mail_scanner, "_iter_mail_files", h.enumerate_files)
    h.scan(cancel_check=lambda: True)
    assert h.snapshot.refreshed_at is None
    assert normalize_path_key(str(path)) in load_mail_scan_state(h.state_file)["files"]


def test_missing_selected_file_preserves_state_until_complete_refresh(discovery_scan):
    h = discovery_scan
    h.add("a.eml", 20)
    deleted = h.add("b.eml", 10)
    h.scan()
    deleted.unlink()
    h.scan()
    assert h.indexer.last_mail_scan_completion == mail_scanner.MailScanCompletion.UNKNOWN
    assert len(h.commits) == 1
    assert len(h.traversals) == 1


def test_missing_selection_advances_cursor_to_later_valid_mail(discovery_scan):
    h = discovery_scan
    first = h.add("first.eml", 30)
    second = h.add("second.eml", 20)
    h.add("valid.eml", 10)
    h.cfg["mail"]["max_mails_per_scan"] = 2
    h.scan()
    h.commits.clear()
    # Lost success state makes all discovered entries actionable again.
    h.state_file.unlink()
    first.unlink()
    second.unlink()
    h.scan()
    assert h.scan()[0] == 1
    assert len(h.traversals) == 1


def test_worker_manual_request_invalidates_without_clearing_retry_history(discovery_scan):
    from knowmate.collector.scheduler import CollectorWorker

    h = discovery_scan
    h.add("a.eml", 20)
    h.scan()
    worker = CollectorWorker(h.cfg, h.indexer, object(), state_file=h.tmp_path / "docs.json")
    worker._mail_discovery = h.snapshot
    worker.request_failure_retry()
    assert worker._retry_requested is True
    assert h.snapshot.refreshed_at is None


def test_failed_state_save_recovers_from_current_db_on_reuse(discovery_scan, monkeypatch):
    h = discovery_scan
    h.add("a.eml", 20)
    original = mail_scanner.save_mail_scan_state
    monkeypatch.setattr(mail_scanner, "save_mail_scan_state", lambda *_: False)
    h.scan()
    monkeypatch.setattr(mail_scanner, "save_mail_scan_state", original)
    h.scan()
    assert len(h.commits) == 1
    assert len(h.checks) == 2
    assert len(h.traversals) == 1
    assert len(load_mail_scan_state(h.state_file)["files"]) == 1
