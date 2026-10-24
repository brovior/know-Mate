"""Bound indexing metadata caches without changing stored rows or query results."""
from __future__ import annotations

def test_indexing_cache_settings_use_bundled_defaults_and_reject_invalid_values():
    from knowmate.config import indexing_cache_settings

    assert indexing_cache_settings({}) == (64 * 1024 * 1024, 64 * 1024 * 1024)
    assert indexing_cache_settings({'index_cache_mib': 8, 'metadata_cache_mib': 16}) == (
        8 * 1024 * 1024, 16 * 1024 * 1024)
    for invalid in (0, -1, True, 1.5, '32', None):
        assert indexing_cache_settings({'index_cache_mib': invalid, 'metadata_cache_mib': invalid}) == (
            64 * 1024 * 1024, 64 * 1024 * 1024)


def test_cache_eviction_preserves_mail_rows_and_readers(tmp_path):
    from knowmate.rag.lance_session import create_indexing_session
    from knowmate.rag.indexer import Indexer
    from knowmate.rag.email_indexer import EmailIndexer
    from knowmate.rag.embedding import EmbeddingClient
    from knowmate.rag.query_reader import QueryReader

    session = create_indexing_session({'index_cache_mib': 1, 'metadata_cache_mib': 1})
    embed = EmbeddingClient('http://127.0.0.1:1', '', fake=True)
    documents = Indexer(tmp_path / 'db', embed, lance_session=session)
    emails = EmailIndexer(tmp_path / 'db', embed, lance_session=session)
    # Each append creates a new manifest. Queries must remain correct as old
    # metadata is evicted, including reads through an independent connection.
    rows = []
    for number in range(100):
        row = {field.name: '' for field in emails.table.schema}
        row.update(chunk_id=str(number), mail_uid=f'knox:{number}', scope='local',
            source_type='knox', chunk_index=0, chunk_total=1, vector=[0.] * 1024,
            is_deleted=False, miss_count=0, mtime=float(number), mail_date_ts=0.)
        emails.table.add([row])
        rows = emails.table.search().select(['chunk_id']).limit(101).to_arrow().to_pylist()
        assert len(rows) == number + 1
    assert {row['chunk_id'] for row in rows} == {str(i) for i in range(100)}
    assert session.size_bytes < 3 * 1024 * 1024
    reader = QueryReader(tmp_path / 'db')
    try:
        assert reader.email_indexer.table.count_rows() == 100
        assert documents.table.count_rows() == 0
    finally:
        reader.close()


def test_default_indexers_supply_bounded_sessions(tmp_path, monkeypatch):
    import lancedb
    from knowmate.rag.embedding import EmbeddingClient
    from knowmate.rag.indexer import Indexer
    from knowmate.rag.email_indexer import EmailIndexer

    sessions = []
    connect = lancedb.connect
    def capture(*args, **kwargs):
        sessions.append(kwargs.get('session'))
        return connect(*args, **kwargs)
    monkeypatch.setattr(lancedb, 'connect', capture)
    embed = EmbeddingClient('http://127.0.0.1:1', '', fake=True)
    Indexer(tmp_path / 'db', embed)
    EmailIndexer(tmp_path / 'db', embed)
    assert len(sessions) == 2 and all(isinstance(session, lancedb.Session) for session in sessions)
