"""External SDK stand-ins verify request-owned HTTP resources close."""
import sys
from types import SimpleNamespace

import pytest

from knowmate.llm.client import LLMClient


@pytest.mark.parametrize("outcome", ["success", "api_error", "invalid_response"])
def test_claude_request_closes_client_on_every_outcome(monkeypatch, outcome):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))
            self.messages = self

        def create(self, **kwargs):
            calls.append(("request", kwargs))
            if outcome == "api_error":
                raise RuntimeError("synthetic private request error")
            return SimpleNamespace(content=[SimpleNamespace(text="synthetic answer")] if outcome == "success" else [])

        def close(self):
            calls.append(("close", None))

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=Client))
    llm = LLMClient("unused", "unused", "model-one", mode="claude", api_key="synthetic-token")
    if outcome == "success":
        assert llm.answer("synthetic question", ["synthetic context"]) == "synthetic answer"
    else:
        with pytest.raises(RuntimeError if outcome == "api_error" else IndexError):
            llm.answer("synthetic question", ["synthetic context"])
    assert [name for name, _ in calls] == ["init", "request", "close"]
    assert calls[0][1] == {"api_key": "synthetic-token"}
    assert calls[1][1]["model"] == "model-one"
    assert calls[1][1]["messages"] == [{"role": "user", "content": "참고 문서:\nsynthetic context\n\n질문: synthetic question"}]
