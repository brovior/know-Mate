"""임베딩의 네트워크 이후 지연과 연결 준비 지연을 구분한다."""
import json
import logging
from types import SimpleNamespace

import pytest

from knowmate.rag import embedding
from knowmate.rag.embedding_cancel import EmbeddingCancellation, embedding_cancellation_scope


@pytest.mark.parametrize("slow_phase", ["setup", "json", "validate"])
@pytest.mark.parametrize("tracing", [False, True])
def test_slow_embedding_attributes_non_network_time(monkeypatch, caplog, slow_phase, tracing):
    """해당 단계만 지연시키면 ttfb·read나 미계측으로 시간이 새지 않는다."""
    clock = SimpleNamespace(wall=0.0, cpu=0.0)
    response_bytes = json.dumps({
        "data": [{"index": 0, "embedding": [0.0] * embedding.VECTOR_DIM}],
        "private_response": "RESPONSE_MUST_NOT_BE_LOGGED",
    }).encode("utf-8")

    def advance(phase, seconds, cpu=0.0):
        clock.wall += seconds + (5.0 if phase == slow_phase else 0.0)
        clock.cpu += cpu + (4.5 if phase == slow_phase == "validate" else 0.0)

    class Response:
        status = 200

        def read(self):
            advance("read", 0.03)
            return response_bytes

        def close(self):
            pass

    class Connection:
        sock = None

        def request(self, *args, **kwargs):
            advance("ttfb", 0.90)

        def getresponse(self):
            return Response()

    connection = Connection()

    def get_connection():
        advance("setup", 0.01)
        return connection, 0.01

    real_loads = json.loads
    real_validate = embedding._vectors_in_request_order

    def parse_response(data):
        advance("json", 0.02, 0.01)
        return real_loads(data)

    def validate_response(body, expected_count):
        advance("validate", 0.01, 0.01)
        return real_validate(body, expected_count)

    monkeypatch.setattr(embedding, "time", SimpleNamespace(
        perf_counter=lambda: clock.wall, thread_time=lambda: clock.cpu,
    ))
    monkeypatch.setattr(embedding, "tracemalloc", SimpleNamespace(
        is_tracing=lambda: tracing, get_traceback_limit=lambda: 10,
    ))
    monkeypatch.setattr(embedding, "json", SimpleNamespace(dumps=json.dumps, loads=parse_response))
    monkeypatch.setattr(embedding, "_vectors_in_request_order", validate_response)
    client = embedding.EmbeddingClient("http://unused", "unused", api_key="KEY_MUST_NOT_BE_LOGGED")
    monkeypatch.setattr(client, "_get_connection", get_connection)
    with caplog.at_level(logging.WARNING, logger=embedding.__name__):
        with embedding_cancellation_scope(EmbeddingCancellation()):
            vectors = client.embed(["INPUT_MUST_NOT_BE_LOGGED"])

    assert vectors == [[0.0] * embedding.VECTOR_DIM]
    log = caplog.text
    assert "느린 호출 5.97초" in log
    assert "connect=0.01 ttfb=0.90 read=0.03" in log
    assert f"setup={5.0 if slow_phase == 'setup' else 0.0:.2f}" in log
    assert f"json={5.02 if slow_phase == 'json' else 0.02:.2f}" in log
    assert f"validate={5.01 if slow_phase == 'validate' else 0.01:.2f}" in log
    assert "미계측=0.00" in log
    assert f"cpu={4.52 if slow_phase == 'validate' else 0.02:.2f}" in log
    assert f"tracing={tracing} trace_depth={10 if tracing else 0}" in log
    assert "MUST_NOT_BE_LOGGED" not in log
