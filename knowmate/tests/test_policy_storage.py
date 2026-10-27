"""Real temporary LanceDB checks for policy cleanup and mail re-exclusion."""
from __future__ import annotations

from knowmate.collector import failure_state
from knowmate.collector.mail_scanner import run_mail_scan
from knowmate.collector.mail_scan_state import load_mail_scan_state
from knowmate.rag.embedding import EmbeddingClient
from knowmate.rag.indexer import Indexer
from knowmate.rag.email_indexer import EmailIndexer
from knowmate.tests.test_mail_single_retry import _mail
from knowmate.tests.test_policy_worker import make_worker


def test_policy_deletes_only_selected_real_document_rows(tmp_path, monkeypatch):
    """Real metadata counts and selective deletes leave other document rows intact."""
    worker, paths, _ = make_worker(tmp_path, monkeypatch, 10)
    worker._indexer = Indexer(tmp_path / "db", EmbeddingClient(base_url="", host_header="", fake=True))
    for path in paths:
        worker._indexer.index_file(path, "synthetic document", mtime=1, scope="local")
    worker.request_policy_job("job", "exclude", tuple(paths[:7]))
    worker.start()
    assert worker.wait(15000)
    snapshot = worker.policy_job_snapshot()
    assert snapshot["status"] == "success"
    assert snapshot["counts"] == {"local_count": 3, "shared_count": 0, "doc_count": 3, "mail_count": 0}
    rows = worker._indexer.table.search().select(["file_path"]).to_arrow().to_pylist()
    assert {row["file_path"] for row in rows} == set(paths[7:])


def test_real_mail_exclude_unexclude_direct_retry_reexclude_preserves_other_uid(tmp_path, monkeypatch):
    """Re-exclusion must clean a newly indexed generation despite the old proof."""
    worker, _, cfg = make_worker(tmp_path, monkeypatch, 0)
    cfg["collector"]["watch_folders"] = [str(tmp_path)]
    cfg["mail"] = {"enabled": True, "max_mails_per_scan": 500}
    config_path = tmp_path / "config.yaml"
    from knowmate import config
    config._save_config(cfg)
    worker._email_indexer = EmailIndexer(tmp_path / "mail-db", EmbeddingClient(base_url="", host_header="", fake=True))
    selected = _mail(tmp_path / "selected.eml", "shared", "synthetic selected", 2000)
    alias = _mail(tmp_path / "alias.eml", "shared", "synthetic alias", 1000)
    other = _mail(tmp_path / "other.eml", "other", "synthetic other", 1500)
    originals = {path: path.read_bytes() for path in (selected, alias, other)}
    run_mail_scan([str(tmp_path)], worker._email_indexer, cfg,
                  state_file=worker._mail_state_file, failure_file=worker._failure_file)
    before = worker._email_indexer.table.search().select(["chunk_id", "source_file"]).to_arrow().to_pylist()
    other_ids = {row["chunk_id"] for row in before if row["source_file"] == str(other)}
    assert other_ids
    assert worker.request_policy_job("exclude", "exclude", (str(selected),)) == "ok"
    worker.run()
    assert worker.policy_job_snapshot()["status"] == "success"
    assert worker.acknowledge_policy_job("exclude")
    state = load_mail_scan_state(worker._mail_state_file)
    assert all(entry["mail_uid"] != "eml:<shared@example.invalid>" for entry in state["files"].values())

    assert worker.request_policy_job("unexclude", "unexclude", (str(selected),)) == "ok"
    worker.run()
    assert worker.acknowledge_policy_job("unexclude")
    records = {}
    failure_state.note_failure(records, str(selected), failure_state.KIND_UNKNOWN_TRANSIENT,
                               "parse", None, 2000, selected.stat().st_size, 1000)
    assert failure_state.save_failures(worker._failure_file, records)
    assert worker.request_single_retry(str(selected)) == "ok"
    worker.run()
    assert worker._email_indexer.table.count_rows() > len(other_ids)
    assert worker.request_policy_job("exclude-again", "exclude", (str(selected),)) == "ok"
    worker.run()
    assert worker.policy_job_snapshot()["status"] == "success"
    after = worker._email_indexer.table.search().select(["chunk_id"]).to_arrow().to_pylist()
    assert {row["chunk_id"] for row in after} == other_ids
    assert {path: path.read_bytes() for path in originals} == originals
    assert config_path.is_file()
