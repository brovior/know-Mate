"""메일 경로 제외의 UID 복사본·삭제 대기·상태 안전성 테스트."""
from __future__ import annotations

class _FakeEmailIndexer:
    def __init__(self, refs, *, fail_deletes=0):
        self.refs = list(refs)
        self.fail_deletes = fail_deletes
        self.delete_calls = []

    def get_source_chunk_refs(self, _paths):
        return list(self.refs)

    def delete_chunk_ids(self, chunk_ids):
        ids = tuple(chunk_ids)
        self.delete_calls.append(ids)
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise RuntimeError("delete failed")
        return ids


def _state(files):
    return {"schema_version": 3, "cursor": None, "files": files, "pending_deletes": []}


def _entry(uid):
    from knowmate.rag.email_indexer import EMAIL_INDEX_VERSION
    return {
        "mtime": 1.0, "size": 1, "mail_uid": uid,
        "index_version": EMAIL_INDEX_VERSION, "uid_resolution_version": 2,
    }


def test_excluding_current_source_invalidates_same_uid_caches_and_retries_delete(tmp_path):
    from knowmate.collector.mail_exclusion import reconcile_mail_exclusions
    from knowmate.collector.mail_scan_state import (
        load_mail_scan_state, normalize_path_key, save_mail_scan_state,
    )

    source = str(tmp_path / "A.mysingle")
    alias = str(tmp_path / "B.mysingle")
    state_file = tmp_path / "mail_scan_state.json"
    assert save_mail_scan_state(state_file, _state({
        normalize_path_key(source): _entry("knox:one"),
        normalize_path_key(alias): _entry("knox:one"),
    }))
    indexer = _FakeEmailIndexer([
        {"chunk_id": "old-1", "mail_uid": "knox:one", "source_file": source},
    ], fail_deletes=1)
    reconciled = set()

    first = reconcile_mail_exclusions(indexer, state_file, [source], reconciled)

    assert first.ok and first.pending_chunks == 1
    saved = load_mail_scan_state(state_file)
    assert saved["files"] == {}
    assert saved["pending_deletes"] == ["old-1"]

    second = reconcile_mail_exclusions(indexer, state_file, [source], reconciled)

    assert second.ok and second.pending_chunks == 0
    assert load_mail_scan_state(state_file)["pending_deletes"] == []
    assert indexer.delete_calls == [("old-1",), ("old-1",)]


def test_excluding_alias_without_rows_keeps_other_uid_cache(tmp_path):
    from knowmate.collector.mail_exclusion import reconcile_mail_exclusions
    from knowmate.collector.mail_scan_state import (
        load_mail_scan_state, normalize_path_key, save_mail_scan_state,
    )

    source = str(tmp_path / "A.mysingle")
    alias = str(tmp_path / "B.mysingle")
    state_file = tmp_path / "mail_scan_state.json"
    assert save_mail_scan_state(state_file, _state({
        normalize_path_key(source): _entry("knox:one"),
        normalize_path_key(alias): _entry("knox:one"),
    }))

    report = reconcile_mail_exclusions(
        _FakeEmailIndexer([]), state_file, [alias], set(),
    )

    assert report.ok
    assert load_mail_scan_state(state_file)["files"] == {
        normalize_path_key(source): _entry("knox:one"),
    }


def test_state_save_failure_never_deletes_mail_chunks(tmp_path, monkeypatch):
    from knowmate.collector import mail_exclusion
    from knowmate.collector.mail_scan_state import normalize_path_key, save_mail_scan_state

    source = str(tmp_path / "A.mysingle")
    state_file = tmp_path / "mail_scan_state.json"
    assert save_mail_scan_state(state_file, _state({
        normalize_path_key(source): _entry("knox:one"),
    }))
    indexer = _FakeEmailIndexer([
        {"chunk_id": "old-1", "mail_uid": "knox:one", "source_file": source},
    ])
    monkeypatch.setattr(mail_exclusion, "save_mail_scan_state", lambda *_args: False)

    report = mail_exclusion.reconcile_mail_exclusions(
        indexer, state_file, [source], set(),
    )

    assert not report.ok
    assert indexer.delete_calls == []


def test_corrupt_state_blocks_destructive_mail_cleanup(tmp_path):
    from knowmate.collector.mail_exclusion import reconcile_mail_exclusions

    state_file = tmp_path / "mail_scan_state.json"
    state_file.write_text("{broken", encoding="utf-8")
    indexer = _FakeEmailIndexer([])

    report = reconcile_mail_exclusions(
        indexer, state_file, [str(tmp_path / "A.mysingle")], set(),
    )

    assert not report.ok
    assert state_file.read_text(encoding="utf-8") == "{broken"


def test_mail_scanner_excludes_path_before_attempt_budget(tmp_path, monkeypatch):
    from knowmate.collector import mail_scanner
    from knowmate.collector.failure_state import BackoffPolicy
    from knowmate.collector.mail_scan_state import normalize_path_key

    excluded = str(tmp_path / "excluded.mysingle")
    active = str(tmp_path / "active.mysingle")
    items = [
        {"path": excluded, "path_key": normalize_path_key(excluded), "mtime": 2.0, "size": 1},
        {"path": active, "path_key": normalize_path_key(active), "mtime": 1.0, "size": 1},
    ]
    monkeypatch.setattr(mail_scanner, "_iter_scanned_mail_items", lambda *_args: iter(items))
    state = {"files": {}}

    candidates, seen, *_rest = mail_scanner._collect_actionable_candidates(
        [], [".mysingle"], state, {}, 0.0, BackoffPolicy(),
        excluded_keys=frozenset({normalize_path_key(excluded)}),
    )

    assert [item["path"] for item in candidates] == [active]
    assert seen == {normalize_path_key(excluded), normalize_path_key(active)}


def test_retriever_filters_excluded_mail_before_decryption(tmp_path):
    import pyarrow as pa
    from knowmate.collector.mail_scan_state import normalize_path_key
    from knowmate.rag.retriever import Retriever

    excluded = str(tmp_path / "excluded.mysingle")
    allowed = str(tmp_path / "allowed.mysingle")

    class Query:
        def where(self, _where):
            return self

        def limit(self, _limit):
            return self

        def to_arrow(self):
            return pa.table({
                "chunk_id": ["excluded", "allowed"],
                "source_file": [excluded, allowed],
                "text": ["cipher-excluded", "cipher-allowed"],
                "_distance": [0.1, 0.2],
            })

    class Table:
        def search(self, *_args):
            return Query()

    class Crypto:
        def __init__(self):
            self.values = []

        def decrypt(self, value):
            self.values.append(value)
            return value

    crypto = Crypto()
    indexer = type("Indexer", (), {"table": Table()})()
    retriever = Retriever(
        indexer=indexer,
        embed_client=object(),
        top_k=10,
        score_threshold=0.0,
        crypto=crypto,
    )

    rows = retriever._search_table(
        Table(), [0.0], None, False,
        excluded_path_keys={normalize_path_key(excluded)},
        path_column="source_file",
    )

    assert [row["chunk_id"] for row in rows] == ["allowed"]
    assert crypto.values == ["cipher-allowed"]
