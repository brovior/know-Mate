"""사내 LLM API 클라이언트 (직접 IP base_url + Host 헤더 라우팅)."""
import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "당신은 사내 문서 기반 지식 AI 비서입니다. "
    "아래 제공된 사내 문서 내용을 바탕으로 사용자 질문에 정확하게 답변하세요. "
    "문서에 없는 내용은 추측하지 말고 '해당 내용을 문서에서 찾을 수 없습니다'라고 안내하세요."
)

_CONTEXT_SEPARATOR = "\n---\n"


def _estimate_tokens(text: str) -> int:
    """한국어 특성 반영 토큰 근사 추정: len(text) * 0.75 (CLAUDE.md 6-10)."""
    return int(len(text) * 0.75)


def _trim_chunks(chunks: list[str], max_tokens: int) -> list[str]:
    """상위 청크부터 토큰 예산 내에서 선택해 반환한다. 최소 1개는 보장한다."""
    selected: list[str] = []
    budget = max_tokens
    for chunk in chunks:
        cost = _estimate_tokens(chunk)
        if selected and budget - cost < 0:
            break
        selected.append(chunk)
        budget -= cost
    return selected or chunks[:1]


class LLMClient:
    def __init__(
        self,
        base_url: str,
        host_header: str,
        model: str,
        mode: str = "fake",
        api_key: str = "",
        max_context_tokens: int = 4096,
    ) -> None:
        """LLM 클라이언트를 초기화한다.

        mode: fake | claude | api
          fake   — API 호출 없이 고정 텍스트 반환
          claude — Anthropic Claude API 사용 (api_key 필요)
          api    — 사내 LLM API (base_url + host_header)
        """
        self._base_url = base_url.rstrip("/")
        self._host_header = host_header
        self._model = model
        self._mode = mode
        self._api_key = api_key
        self._max_context_tokens = max_context_tokens

    def answer(self, query: str, context_chunks: list[str]) -> str:
        """질문과 컨텍스트 청크를 받아 LLM 답변 문자열을 반환한다."""
        if not context_chunks:
            return "관련 문서를 찾지 못했습니다. 인덱싱된 문서가 있는지 확인해 주세요."

        trimmed = _trim_chunks(context_chunks, self._max_context_tokens)
        if len(trimmed) < len(context_chunks):
            logger.info("컨텍스트 토큰 초과로 %d→%d청크 트리밍", len(context_chunks), len(trimmed))

        if self._mode == "fake":
            preview = trimmed[0][:200]
            return (
                f"[fake 모드 답변]\n\n"
                f"질문: {query}\n\n"
                f"참고 문서 미리보기:\n{preview}"
            )

        if self._mode == "claude":
            return self._call_claude(query, trimmed)

        if self._mode == "openrouter":
            return self._call_openrouter(query, trimmed)

        return self._call_api(query, trimmed)

    def _call_claude(self, query: str, context_chunks: list[str]) -> str:
        """Anthropic Claude API를 호출해 답변을 반환한다."""
        import anthropic
        context_text = _CONTEXT_SEPARATOR.join(context_chunks)
        user_message = f"참고 문서:\n{context_text}\n\n질문: {query}"

        client = anthropic.Anthropic(api_key=self._api_key or None)
        message = client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        return message.content[0].text

    def _call_openrouter(self, query: str, context_chunks: list[str]) -> str:
        """OpenRouter API(OpenAI 호환)를 호출해 답변을 반환한다."""
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
            "https://openrouter.ai/api/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]

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
    extractor = cfg.get("extractor", "fake")
    llm_cfg = cfg.get("llm", {})
    mode = llm_cfg.get("mode", "fake" if extractor == "fake" else "api")

    # API 키: config 우선, 없으면 환경변수 (모드별 우선순위)
    api_key = (
        llm_cfg.get("api_key", "")
        or os.environ.get("OPENROUTER_API_KEY", "")
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )

    return LLMClient(
        base_url=llm_cfg.get("base_url", "http://localhost"),
        host_header=llm_cfg.get("host_header", "llm.internal"),
        model=llm_cfg.get("model", "qwen3-27b"),
        mode=mode,
        api_key=api_key,
        max_context_tokens=llm_cfg.get("max_context_tokens", 4096),
    )
