"""임베딩 모델 상수 및 API 클라이언트 (CLAUDE.md 5-1)."""
import json
import math
import os
import random
import urllib.request
from typing import Any

# 인증 키가 없을 때 보내는 더미 값. 사내 임베딩 서버는 Authorization 헤더가
# 비어 있으면 호출을 거부하므로, 키 미설정 시 이 더미값을 채워 보낸다.
_DUMMY_API_KEY = "dummy"

# 모델 → 벡터 차원 매핑 (단일 출처). 모델 추가 시 여기만 갱신한다.
# 모델과 차원은 한 몸이라 따로 두면 desync 되므로 VECTOR_DIM은 여기서 파생한다.
MODEL_DIMS = {
    "bge-m3": 1024,
}

EMBEDDING_MODEL = "bge-m3"
VECTOR_DIM = MODEL_DIMS[EMBEDDING_MODEL]  # 모델에서 자동 파생. 변경 시 전체 재인덱싱 필수

_local_model = None  # sentence-transformers 모델 싱글톤


def _get_local_model(model_name: str):
    """sentence-transformers 모델을 싱글톤으로 반환한다."""
    global _local_model
    if _local_model is None:
        import os
        # Qt 이벤트 루프와 PyTorch 스레드 풀 충돌 방지
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("MKL_NUM_THREADS", "1")
        try:
            import torch
            torch.set_num_threads(1)
            torch.set_num_interop_threads(1)
        except Exception:
            pass
        from sentence_transformers import SentenceTransformer
        _local_model = SentenceTransformer(model_name)
    return _local_model


class EmbeddingClient:
    def __init__(
        self,
        base_url: str,
        host_header: str,
        fake: bool = False,
        local: bool = False,
        local_model_name: str = "BAAI/bge-m3",
        api_key: str = "",
    ) -> None:
        """임베딩 클라이언트를 초기화한다.

        fake=True: 랜덤 벡터 반환 (API 불필요)
        local=True: sentence-transformers 로컬 모델 사용
        둘 다 False: 사내 임베딩 API 호출
        api_key: 사내 API 인증 키. 비우면 더미 값을 전송한다.
        """
        self._base_url = base_url.rstrip("/")
        self._host_header = host_header
        self._fake = fake
        self._local = local
        self._local_model_name = local_model_name
        self._api_key = api_key or _DUMMY_API_KEY

    def embed(self, texts: list[str]) -> list[list[float]]:
        """텍스트 리스트를 임베딩 벡터 리스트로 변환한다."""
        if not texts:
            return []
        if self._fake:
            return [self._random_unit_vector() for _ in texts]
        if self._local:
            return self._call_local(texts)
        return self._call_api(texts)

    def _random_unit_vector(self) -> list[float]:
        """정규화된 랜덤 단위벡터를 반환한다."""
        vec = [random.gauss(0.0, 1.0) for _ in range(VECTOR_DIM)]
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            norm = 1.0
        return [x / norm for x in vec]

    def _call_local(self, texts: list[str]) -> list[list[float]]:
        """sentence-transformers 로컬 모델로 임베딩한다."""
        model = _get_local_model(self._local_model_name)
        vecs = model.encode(texts, normalize_embeddings=True)
        return vecs.tolist()

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
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        # 사내 시스템 프록시가 요청을 가로채 403을 내므로 프록시를 우회한다
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return [item["embedding"] for item in body["data"]]


def get_embedding_client(cfg: dict[str, Any]) -> EmbeddingClient:
    """config dict로부터 EmbeddingClient 인스턴스를 생성해 반환한다."""
    extractor = cfg.get("extractor", "fake")
    embed_cfg = cfg.get("embedding", {})
    mode = embed_cfg.get("mode", "fake" if extractor == "fake" else "api")

    # API 키: config 우선, 없으면 환경변수
    api_key = embed_cfg.get("api_key", "") or os.environ.get("EMBED_API_KEY", "")

    return EmbeddingClient(
        base_url=embed_cfg.get("base_url", "http://localhost"),
        host_header=embed_cfg.get("host_header", "embed.internal"),
        fake=(mode == "fake"),
        local=(mode == "local"),
        local_model_name=embed_cfg.get("local_model", "BAAI/bge-m3"),
        api_key=api_key,
    )
