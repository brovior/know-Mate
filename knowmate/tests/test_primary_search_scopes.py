"""Independent review checks for scope protection before content decryption."""
from types import SimpleNamespace

import pyarrow as pa
import pytest

from knowmate.rag.retriever import Retriever


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        (None, {"mine", "shared"}),
        (["local"], {"mine"}),
        (["shared"], {"shared"}),
        (["local", "shared", "mail"], {"mine", "shared"}),
    ],
)
def test_scope_and_owner_rechecked_before_decrypt(scopes, expected):
    """An adapter ignoring its SQL filter must not expose unauthorized content."""
    captured = {}
    records = [
        {"chunk_id": "mine", "scope": "local", "owner": "review'user"},
        {"chunk_id": "other", "scope": "local", "owner": "another-user"},
        {"chunk_id": "shared", "scope": "shared", "owner": "another-user"},
        {"chunk_id": "unknown", "scope": "unknown", "owner": "review'user"},
    ]
    for record in records:
        record.update(text=record["chunk_id"], file_path=f"C:/synthetic/{record['chunk_id']}.txt", _distance=0.1)
    arrow = pa.Table.from_pylist(records)

    class Query:
        def where(self, expression, *, prefilter):
            captured.update(expression=expression, prefilter=prefilter)
            return self

        def select(self, columns):
            captured["columns"] = columns
            return self

        def limit(self, count):
            captured["limit"] = count
            return self

        def to_arrow(self):
            return arrow

    class Table:
        schema = arrow.schema

        def search(self, _vector):
            return Query()

    decrypted = []
    crypto = SimpleNamespace(decrypt=lambda value: decrypted.append(value) or value)
    retriever = Retriever(SimpleNamespace(table=Table()), object(), crypto=crypto, top_k=5)
    retriever._current_user = "review'user"
    rows = retriever._search_table(
        retriever._table, [1.0], scopes, use_owner_filter=True,
        extra_where="mtime >= 1000 AND lower(file_path) NOT IN ('c:/synthetic/excluded.txt')",
    )
    assert {row["chunk_id"] for row in rows} == expected
    assert set(decrypted) == expected
    assert captured["prefilter"] is True
    assert "is_deleted = false" in captured["expression"]
    assert "mtime >= 1000" in captured["expression"]
    assert "NOT IN" in captured["expression"]
    if scopes is None or "local" in scopes:
        assert "owner = 'review''user'" in captured["expression"]
    assert "vector" not in captured["columns"]
    assert captured["limit"] == 10
