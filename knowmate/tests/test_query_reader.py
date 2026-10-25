"""요청별 읽기 경로의 무쓰기·취소·오류 비노출·참조 해제를 검증한다."""
import getpass
import json
import sys
from types import SimpleNamespace

import lancedb
import pyarrow as pa
import pytest

from knowmate.agents.knowledge_agent import KnowledgeAgent, _build_pipeline
from knowmate.agents.registry import AgentRegistry
from knowmate.rag.email_indexer import EMAIL_SCHEMA
from knowmate.rag.embedding import VECTOR_DIM
from knowmate.rag.embedding_cancel import EmbeddingCancellation, EmbeddingCancelledError
from knowmate.rag.indexer import SCHEMA
from knowmate.rag.query_reader import QueryIndexUnavailable, QueryReader
from knowmate.rag.retriever import Retriever
from knowmate.secure.crypto import CryptoManager

CFG = {"extractor": "fake", "embedding": {"mode": "fake"},
       "llm": {"mode": "fake"}, "search": {"score_threshold": -1.0}}


def row(schema, **values):
    """스키마에 맞는 합성 청크 한 건을 만든다."""
    result = {}
    for field in schema:
        if pa.types.is_string(field.type):
            result[field.name] = ""
        elif pa.types.is_boolean(field.type):
            result[field.name] = False
        elif pa.types.is_fixed_size_list(field.type):
            result[field.name] = [1.0] + [0.0] * (VECTOR_DIM - 1)
        else:
            result[field.name] = 0
    result.update(chunk_id="1", text="allowed body", scope="local",
                  file_path="/report.txt", file_type="txt", owner=getpass.getuser())
    result.update(values)
    return {key: result[key] for key in schema.names}


def database(tmp_path, name="chunks", schema=SCHEMA, rows=None):
    """질문 이전에 생성된 임시 합성 DB를 반환한다."""
    db = lancedb.connect(str(tmp_path / "index"))
    table = db.create_table(name, schema=schema)
    if rows is not None:
        table.add(rows)
    return db, table


def test_missing_index_never_connects_or_creates(tmp_path, monkeypatch):
    monkeypatch.setattr("knowmate.rag.query_reader.lancedb.connect", lambda *_: pytest.fail("connect"))
    with pytest.raises(QueryIndexUnavailable):
        QueryReader(tmp_path / "index")
    assert list(tmp_path.iterdir()) == []


def test_missing_one_table_is_searchable_and_no_writer_constructed(tmp_path, monkeypatch):
    _, table = database(tmp_path, rows=[row(SCHEMA)])
    original_schema = table.schema
    for target in ("knowmate.rag.indexer.Indexer.__init__",
                   "knowmate.rag.email_indexer.EmailIndexer.__init__",
                   "knowmate.secure.get_extractor"):
        monkeypatch.setattr(target, lambda *_a, **_k: pytest.fail("writer/extractor"))
    monkeypatch.setattr("knowmate.config.get_config", lambda: pytest.fail("global config"))
    monkeypatch.setattr("knowmate.config.get_data_dir", lambda: pytest.fail("global path"))
    agent = KnowledgeAgent(CFG, tmp_path)
    result = agent.handle("report", {"scopes": ["local"]})
    assert "allowed body" in json.dumps(result)
    assert table.count_rows() == 1 and table.schema == original_schema
    assert not (tmp_path / "km.key").exists()
    agent.close()
    assert agent._pipeline is None


def test_email_only_and_request_exclusion_snapshot(tmp_path, monkeypatch):
    excluded = str(tmp_path / "excluded.mysingle")
    _, table = database(tmp_path, "emails", EMAIL_SCHEMA, [
        row(EMAIL_SCHEMA, chunk_id="excluded", source_file=excluded, source_type="knox", text="excluded marker"),
        row(EMAIL_SCHEMA, chunk_id="allowed", source_file=str(tmp_path / "ok.mysingle"), source_type="knox"),
    ])
    cfg = {**CFG, "collector": {"exclude_files": [excluded]}}
    agent = KnowledgeAgent(cfg, tmp_path)
    cfg["collector"]["exclude_files"].clear()
    monkeypatch.setattr("knowmate.config.get_config", lambda: pytest.fail("global config"))
    result = agent.handle("내용", {"scopes": ["local"]})
    assert "excluded marker" not in json.dumps(result)
    assert "allowed body" in json.dumps(result)
    assert table.count_rows() == 2
    agent.close()


@pytest.mark.parametrize("name,schema", [("chunks", SCHEMA), ("emails", EMAIL_SCHEMA)])
def test_old_schema_rejected_without_mutation(tmp_path, name, schema):
    old = pa.schema([field for field in schema if field.name != "text"])
    _, table = database(tmp_path, name, old)
    with pytest.raises(QueryIndexUnavailable, match="schema_not_ready"):
        QueryReader(tmp_path / "index")
    assert table.schema == old and table.count_rows() == 0


def test_bad_column_type_rejected(tmp_path):
    schema = pa.schema([pa.field("owner", pa.int32()) if f.name == "owner" else f for f in SCHEMA])
    database(tmp_path, schema=schema)
    with pytest.raises(QueryIndexUnavailable):
        QueryReader(tmp_path / "index")


def test_empty_existing_db_remains_empty(tmp_path):
    db, _ = database(tmp_path)
    db.drop_table("chunks")
    reader = QueryReader(tmp_path / "index")
    assert reader.table is None and reader.email_indexer is None
    assert db.list_tables().tables == []
    reader.close()


def test_catalog_preserves_mail_and_documents_among_many_tables(tmp_path):
    """Read all local table names without recreating mail data or missing documents."""
    from knowmate.rag.email_indexer import get_or_create_emails_table

    db, _ = database(tmp_path, name="aaa_00", schema=SCHEMA)
    for number in range(1, 12):
        db.create_table(f"aaa_{number:02d}", schema=SCHEMA)
    db.create_table("chunks", schema=SCHEMA)
    db.create_table("emails", data=[row(EMAIL_SCHEMA)], schema=EMAIL_SCHEMA)
    emails, recreated = get_or_create_emails_table(db, with_status=True)
    assert not recreated and emails.count_rows() == 1
    assert len(db.list_tables().tables) == 14
    reader = QueryReader(tmp_path / "index")
    try:
        assert reader.table.count_rows() == 0
        assert reader.email_indexer.table.count_rows() == 1
    finally:
        reader.close()


@pytest.mark.parametrize("fail_unprotect", [False, True])
def test_existing_key_only_no_generation_or_fake_fallback(tmp_path, monkeypatch, fail_unprotect):
    key_file = tmp_path / "km.key"
    key_file.write_bytes(b"DPAPI key")
    calls = []
    def unprotect(value):
        calls.append(value)
        if fail_unprotect:
            raise RuntimeError("secret marker")
        return "", b"k" * 32
    monkeypatch.setitem(sys.modules, "win32crypt", SimpleNamespace(CryptUnprotectData=unprotect))
    monkeypatch.setattr("knowmate.secure.crypto.secrets.token_bytes", lambda *_: pytest.fail("key created"))
    if fail_unprotect:
        with pytest.raises(RuntimeError):
            CryptoManager(key_file, create_if_missing=False)
    else:
        assert CryptoManager(key_file, create_if_missing=False)._key == b"k" * 32
    assert calls == [b"DPAPI key"] and key_file.read_bytes() == b"DPAPI key"


def test_missing_key_never_creates(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "win32crypt", SimpleNamespace())
    with pytest.raises(FileNotFoundError):
        CryptoManager(tmp_path / "km.key", create_if_missing=False)
    assert not (tmp_path / "km.key").exists()


def test_normal_crypto_creation_contract_unchanged(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "win32crypt", SimpleNamespace(CryptProtectData=lambda key: b"protected"))
    manager = CryptoManager(tmp_path / "km.key")
    assert len(manager._key) == 32
    assert (tmp_path / "km.key").read_bytes() == b"protected"


def test_scopes_empty_and_local_mode_do_not_initialize(tmp_path, monkeypatch):
    monkeypatch.setattr("knowmate.rag.query_reader.lancedb.connect", lambda *_: pytest.fail("connect"))
    assert "검색 범위" in KnowledgeAgent(CFG, tmp_path).handle("q", {"scopes": []})[0]["content"]
    assert "임베딩 설정" in KnowledgeAgent({**CFG, "embedding": {"mode": "local"}}, tmp_path).handle("q", {"scopes": ["local"]})[0]["content"]


def test_projection_bounded_no_vectors_and_current_user_filter(tmp_path):
    _, table = database(tmp_path, rows=[
        row(SCHEMA), row(SCHEMA, chunk_id="other", owner="other-user"),
        row(SCHEMA, chunk_id="deleted", is_deleted=True),
    ])
    pipeline = _build_pipeline(CFG, tmp_path)
    rows = pipeline["retriever"].search("내용", ["local"])
    assert [r["chunk_id"] for r in rows] == ["1"]
    assert "vector" not in rows[0] and "_distance" in rows[0]
    assert table.count_rows() == 3
    agent = KnowledgeAgent(CFG, tmp_path)
    agent._pipeline = pipeline
    agent.close()


def test_fresh_request_observes_finished_writes(tmp_path):
    _, table = database(tmp_path, rows=[row(SCHEMA)])
    old = QueryReader(tmp_path / "index")
    table.add([row(SCHEMA, chunk_id="new")])
    fresh = QueryReader(tmp_path / "index")
    assert old.table.count_rows() == 1
    assert fresh.table.count_rows() == 2
    fresh.close()
    old.close()


@pytest.mark.parametrize("stage", ["pipeline", "search", "llm"])
def test_internal_error_payload_not_returned_or_logged(tmp_path, monkeypatch, caplog, stage):
    marker = "PRIVATE-QUERY-CHUNK-TOKEN"
    def fail(*_a, **_k):
        raise RuntimeError(marker)
    if stage == "pipeline":
        monkeypatch.setattr("knowmate.agents.knowledge_agent._build_pipeline", fail)
    agent = KnowledgeAgent(CFG, tmp_path)
    if stage != "pipeline":
        agent._pipeline = {"retriever": SimpleNamespace(search=fail if stage == "search" else lambda *_a, **_k: []),
                           "llm": SimpleNamespace(answer=fail)}
    result = agent.handle("ordinary query", {"scopes": ["local"]})
    assert marker not in json.dumps(result) + caplog.text
    assert "Traceback" not in json.dumps(result) + caplog.text
    agent.close()


@pytest.mark.parametrize("stage", ["before", "after_search", "decrypt", "after_llm"])
def test_cancel_propagates_without_new_llm(tmp_path, stage):
    cancellation = EmbeddingCancellation()
    calls = []
    def search(*_a, **_k):
        if stage == "after_search":
            cancellation.cancel()
        return [{"text": "body"}]
    def answer(*_a):
        calls.append("llm")
        cancellation.cancel()
        return "late"
    agent = KnowledgeAgent(CFG, tmp_path)
    agent._pipeline = {"retriever": SimpleNamespace(search=search), "llm": SimpleNamespace(answer=answer)}
    if stage == "before":
        cancellation.cancel()
    if stage == "decrypt":
        _, table = database(tmp_path, rows=[row(SCHEMA), row(SCHEMA, chunk_id="2")])
        class Crypto:
            def decrypt(self, value):
                cancellation.cancel()
                return value
        agent._pipeline["retriever"] = Retriever(
            SimpleNamespace(table=table), SimpleNamespace(embed=lambda _: [[1.0] + [0.0] * (VECTOR_DIM - 1)]),
            crypto=Crypto(), score_threshold=-1.0, config=CFG,
        )
    with pytest.raises(EmbeddingCancelledError):
        agent.handle("내용", {"scopes": ["local"], "cancel_check": cancellation.check,
                              "embedding_cancellation": cancellation})
    assert calls == (["llm"] if stage == "after_llm" else [])
    agent.close()


def test_registry_closes_owned_resources_even_if_first_close_raises(tmp_path, caplog):
    calls = []
    def release(name):
        calls.append(name)
        if name == "retriever":
            raise RuntimeError("PRIVATE ERROR")
    registry = AgentRegistry(CFG, tmp_path)
    agent = registry.get("knowledge")
    agent._pipeline = {name: SimpleNamespace(close=lambda name=name: release(name))
                       for name in ("retriever", "embed_client", "reader")}
    registry.close()
    assert calls == ["retriever", "embed_client", "reader"]
    assert registry._agents == {} and agent._pipeline is None
    assert "PRIVATE ERROR" not in caplog.text
    registry.close()


def test_pipeline_connection_exposes_only_read_apis(tmp_path, monkeypatch):
    db, table = database(tmp_path, rows=[row(SCHEMA)])
    calls = []
    class ReadTable:
        schema = table.schema
        def search(self, *args):
            calls.append("search")
            return table.search(*args)
        def __getattr__(self, name):
            pytest.fail(f"non-read table API {name}")
    class ReadConnection:
        def list_tables(self, *, page_token=None):
            return SimpleNamespace(tables=["chunks"], page_token=None)
        def open_table(self, name):
            calls.append(name)
            return ReadTable()
        def __getattr__(self, name):
            pytest.fail(f"non-read connection API {name}")
    monkeypatch.setattr("knowmate.rag.query_reader.lancedb.connect", lambda _: ReadConnection())
    agent = KnowledgeAgent(CFG, tmp_path)
    assert "allowed body" in json.dumps(agent.handle("문서", {"scopes": ["local"]}))
    agent.close()
    assert calls == ["chunks", "search"]
    assert db.open_table("chunks").count_rows() == 1


def test_concurrent_writer_and_separate_request_reader(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    _, table = database(tmp_path, rows=[row(SCHEMA)])
    barrier = Barrier(2)
    def write():
        barrier.wait(timeout=10)
        table.add([row(SCHEMA, chunk_id="new", text="new body")])
        table.update(where="chunk_id = '1'", values={"text": "replaced body"})
        table.delete("chunk_id = 'new'")
        table.optimize()
    def read():
        reader = QueryReader(tmp_path / "index")
        retriever = Retriever(reader, SimpleNamespace(embed=lambda _: [[1.0] + [0.0] * (VECTOR_DIM - 1)]),
                              score_threshold=-1.0, config=CFG)
        barrier.wait(timeout=10)
        result = retriever.search("문서", ["local"])
        retriever.close()
        reader.close()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write)
        reading = pool.submit(read)
        writer.result(timeout=20)
        assert reading.result(timeout=20)
    agent = KnowledgeAgent(CFG, tmp_path)
    result = agent.handle("문서", {"scopes": ["local"]})
    agent.close()
    assert "replaced body" in json.dumps(result) and "new body" not in json.dumps(result)


def test_embedding_close_releases_current_request_connection():
    from knowmate.rag.embedding import EmbeddingClient
    client = EmbeddingClient("http://example", "example", fake=True)
    calls = []
    client._conn = SimpleNamespace(close=lambda: calls.append("closed"))
    client.close()
    client.close()
    assert calls == ["closed"] and client._conn is None


def test_document_exclusions_survive_failed_physical_delete_before_decryption(tmp_path):
    excluded = str(tmp_path / "excluded-report.txt")
    _, table = database(tmp_path, rows=[
        row(SCHEMA, chunk_id="excluded", file_path=excluded, text="DO-NOT-DECRYPT"),
        row(SCHEMA, chunk_id="allowed", file_path=str(tmp_path / "report.txt")),
    ])
    calls = []
    class Crypto:
        def decrypt(self, value):
            calls.append(value)
            return value
    cfg = {**CFG, "collector": {"exclude_files": [excluded]}}
    reader = QueryReader(tmp_path / "index")
    retriever = Retriever(reader, SimpleNamespace(embed=lambda _: [[1.0] + [0.0] * (VECTOR_DIM - 1)]),
                          crypto=Crypto(), score_threshold=-1.0, config=cfg)
    # DB 청크 삭제가 실패해 행이 남아도 새 요청의 설정은 벡터/키워드 모두 보호한다.
    result = retriever.search("report", ["local"])
    assert [item["chunk_id"] for item in result] == ["allowed"]
    assert calls and "DO-NOT-DECRYPT" not in calls
    assert table.count_rows() == 2
    retriever.close()
    reader.close()


def test_embedding_retry_closes_failed_and_final_connections(monkeypatch, caplog):
    from knowmate.rag.embedding import EmbeddingClient
    calls = []
    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.index = len(calls)
            calls.append(self)
            self.closed = False
        def connect(self):
            pass
        def request(self, *_args, **_kwargs):
            pass
        def getresponse(self):
            if self.index == 0:
                raise OSError("PRIVATE-NETWORK-ERROR")
            return SimpleNamespace(status=200, read=lambda: json.dumps({
                "data": [{"index": 0, "embedding": [1.0] + [0.0] * (VECTOR_DIM - 1)}],
            }).encode())
        def close(self):
            self.closed = True
    monkeypatch.setattr("knowmate.rag.embedding.http.client.HTTPConnection", Connection)
    client = EmbeddingClient("http://example", "example")
    assert len(client.embed(["q"])) == 1
    assert calls[0].closed and not calls[1].closed
    client.close()
    assert all(conn.closed for conn in calls)
    assert "PRIVATE-NETWORK-ERROR" not in caplog.text
