"""사내 LLM API 클라이언트 (직접 IP base_url + Host 헤더 라우팅)."""
import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "당신은 사내 문서 기반 지식 AI 비서입니다. "
    "아래 제공된 사내 문서 내용을 바탕으로 사용자 질문에 정확하게 답변하세요. "
    "문서에 없는 내용은 추측하지 말고 '해당 내용을 문서에서 찾을 수 없습니다'라고 안내하세요."
)

_CONTEXT_SEPARATOR = "\n---\n"


class LLMClient:
    def __init__(
        self,
        base_url: str,
        host_header: str,
        model: str,
        fake: bool = False,
    ) -> None:
        """LLM 클라이언트를 초기화한다. fake=True 이면 API를 호출하지 않는다."""
        self._base_url = base_url.rstrip("/")
        self._host_header = host_header
        self._model = model
        self._fake = fake

    def answer(self, query: str, context_chunks: list[str]) -> str:
        """질문과 컨텍스트 청크를 받아 LLM 답변 문자열을 반환한다."""
        if not context_chunks:
            return "관련 문서를 찾지 못했습니다. 인덱싱된 문서가 있는지 확인해 주세요."

        if self._fake:
            preview = context_chunks[0][:200]
            return (
                f"[fake 모드 답변]\n\n"
                f"질문: {query}\n\n"
                f"참고 문서 미리보기:\n{preview}"
            )

        return self._call_api(query, context_chunks)

    def _call_api(self, query: str, context_chunks: list[str]) -> str:
        """사내 LLM API /v1/chat/completions를 호출해 답변을 반환한다."""
        context_text = _CONTEXT_SEPARATOR.join(context_chunks)
        user_message = f"참고 문서:\n{context_text}\n\n질문: {query}"

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Host": self._host_header,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]


def get_llm_client(cfg: dict[str, Any]) -> LLMClient:
    """config dict로부터 LLMClient 인스턴스를 생성해 반환한다."""
    fake = cfg.get("extractor", "fake") == "fake"
    llm_cfg = cfg.get("llm", {})
    return LLMClient(
        base_url=llm_cfg.get("base_url", "http://localhost"),
        host_header=llm_cfg.get("host_header", "llm.internal"),
        model=llm_cfg.get("model", "qwen3-27b"),
        fake=fake,
    )
