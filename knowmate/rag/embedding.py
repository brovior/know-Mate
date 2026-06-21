"""임베딩 모델 상수 및 API 클라이언트 (CLAUDE.md 5-1)."""
import json
import math
import random
import urllib.request
from typing import Any

EMBEDDING_MODEL = "bge-m3"
VECTOR_DIM = 1024  # bge-m3 고정 차원. 모델 변경 시 전체 재인덱싱 필수


class EmbeddingClient:
    def __init__(self, base_url: str, host_header: str, fake: bool = False) -> None:
        """임베딩 클라이언트를 초기화한다. fake=True 이면 API를 호출하지 않는다."""
        self._base_url = base_url.rstrip("/")
        self._host_header = host_header
        self._fake = fake

    def embed(self, texts: list[str]) -> list[list[float]]:
        """텍스트 리스트를 임베딩 벡터 리스트로 변환한다."""
        if not texts:
            return []
        if self._fake:
            return [self._random_unit_vector() for _ in texts]
        return self._call_api(texts)

    def _random_unit_vector(self) -> list[float]:
        """정규화된 랜덤 단위벡터를 반환한다."""
        vec = [random.gauss(0.0, 1.0) for _ in range(VECTOR_DIM)]
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            norm = 1.0
        return [x / norm for x in vec]

    def _call_api(self, texts: list[str]) -> list[list[float]]:
        """사내 임베딩 API를 호출해 벡터 리스트를 반환한다."""
        url = f"{self._base_url}/v1/embeddings"
        payload: dict[str, Any] = {"model": EMBEDDING_MODEL, "input": texts}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Host": self._host_header,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return [item["embedding"] for item in body["data"]]


def get_embedding_client(cfg: dict[str, Any]) -> EmbeddingClient:
    """config dict로부터 EmbeddingClient 인스턴스를 생성해 반환한다."""
    fake = cfg.get("extractor", "fake") == "fake"
    embed_cfg = cfg.get("embedding", {})
    return EmbeddingClient(
        base_url=embed_cfg.get("base_url", "http://localhost"),
        host_header=embed_cfg.get("host_header", "embed.internal"),
        fake=fake,
    )
