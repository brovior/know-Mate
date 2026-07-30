"""임베딩 모델 상수 및 API 클라이언트 (CLAUDE.md 5-1)."""
import http.client
import json
import logging
import math
import os
import random
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 인증 키가 없을 때 보내는 더미 값. 사내 임베딩 서버는 Authorization 헤더가
# 비어 있으면 호출을 거부하므로, 키 미설정 시 이 더미값을 채워 보낸다.
_DUMMY_API_KEY = "dummy"

# 이 시간을 넘는 임베딩 API 호출만 구간별로 WARNING 로그를 남긴다.
# 진단 스크립트 실측(사내 PC)에서 정상 호출은 32청크도 0.15초였으므로, 3초는
# "확실히 비정상"이면서 정상 호출을 걸러내기에 충분히 여유 있는 값이다.
SLOW_EMBED_CALL_LOG_SEC = 3.0

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

        # HTTP keep-alive 연결 재사용용 (배치마다 재연결하면 인덱싱 속도가 크게 느려짐).
        # http.client는 시스템 프록시를 조회하지 않으므로 별도 프록시 우회 처리가 불필요하다.
        parsed = urlparse(self._base_url)
        self._conn_scheme = parsed.scheme or "http"
        self._conn_host = parsed.hostname
        self._conn_port = parsed.port
        self._conn_path_prefix = parsed.path.rstrip("/")
        self._conn: http.client.HTTPConnection | None = None

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

    def _get_connection(self) -> tuple[http.client.HTTPConnection, float]:
        """keep-alive 연결을 재사용한다. 끊겼으면 재생성한다.

        반환: (연결, 이번 호출에서 새 연결을 맺는 데 쓴 초). 재사용이면 0.0 —
        느린 호출의 원인이 '연결 수립'인지 아닌지 구분하려면 이 값이 필요하다.
        """
        if self._conn is not None:
            return self._conn, 0.0

        conn_cls = (
            http.client.HTTPSConnection
            if self._conn_scheme == "https"
            else http.client.HTTPConnection
        )
        conn = conn_cls(self._conn_host, self._conn_port, timeout=30)
        t0 = time.perf_counter()
        conn.connect()   # 명시적으로 연결만 수행 — 수립 시간을 따로 재기 위해
        elapsed = time.perf_counter() - t0
        self._conn = conn
        return conn, elapsed

    def _call_api(self, texts: list[str]) -> list[list[float]]:
        """사내 임베딩 API를 호출해 벡터 리스트를 반환한다.

        인스턴스 수명 동안 HTTP 연결을 재사용한다(배치마다 재연결하면 인덱싱이
        크게 느려짐). 연결이 끊겨 있으면 1회 재연결 후 재시도한다.

        **구간별 계측**: 사내 실측에서 청크 3개짜리 파일도 임베딩에 21.93초가
        걸리는 현상이 관측됐는데, 같은 PC·같은 설정으로 돌린 진단 스크립트
        (`scripts/diag_embed_latency.py`)에서는 전 시나리오가 0.21초 미만이었다
        (새 연결·재사용·32청크·90초 유휴 후 전부). 즉 네트워크 경로나 서버가
        느린 게 아니라 **앱의 실행 맥락**(미서명 exe에 대한 EDR 검사, 인덱싱 중
        머신 부하, 워커 QThread 등)에서만 느려진다는 뜻이다.

        스크립트로는 더 좁힐 수 없어, 앱 안에서 같은 구간(connect / ttfb / read)을
        재고 느린 호출만 WARNING으로 남긴다 — 원인을 '연결 수립'과 '서버 응답'
        중 하나로 가르는 것이 목적이며, 정상 호출은 로그를 더럽히지 않는다.
        """
        path = f"{self._conn_path_prefix}/v1/embeddings"
        payload: dict[str, Any] = {"model": EMBEDDING_MODEL, "input": texts}
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Host": self._host_header,
            "Authorization": f"Bearer {self._api_key}",
        }

        last_exc: Exception | None = None
        for attempt in range(2):
            call_t0 = time.perf_counter()
            connect_sec = ttfb_sec = read_sec = 0.0
            try:
                conn, connect_sec = self._get_connection()

                t0 = time.perf_counter()
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()
                ttfb_sec = time.perf_counter() - t0

                t0 = time.perf_counter()
                body_bytes = resp.read()
                read_sec = time.perf_counter() - t0

                if resp.status >= 400:
                    raise RuntimeError(
                        f"임베딩 API 오류 {resp.status}: {body_bytes[:200]!r}"
                    )
                body = json.loads(body_bytes.decode("utf-8"))
                result = [item["embedding"] for item in body["data"]]
                self._log_if_slow(
                    len(texts), time.perf_counter() - call_t0,
                    connect_sec, ttfb_sec, read_sec, attempt,
                )
                return result
            except (http.client.HTTPException, OSError) as exc:
                # 연결이 끊겼을 가능성 — 폐기 후 재연결 시도.
                # WARNING으로 올린다(이전엔 DEBUG): 재시도 자체가 느린 호출의
                # 원인일 수 있는데, 기본 로그 레벨(INFO)에서 안 보여 진단이 막혔다.
                logger.warning(
                    "[embed] API 연결 재시도 (attempt=%d, %.2f초 소요 후): %s",
                    attempt, time.perf_counter() - call_t0, exc,
                )
                self._conn = None
                last_exc = exc
        raise RuntimeError(f"임베딩 API 호출 실패: {last_exc}") from last_exc

    def _log_if_slow(
        self, batch_size: int, total_sec: float,
        connect_sec: float, ttfb_sec: float, read_sec: float, attempt: int,
    ) -> None:
        """느린 호출만 구간별로 남긴다(정상 호출은 로그를 더럽히지 않는다).

        판정 가이드 — connect가 크면 연결 수립(네트워크·EDR), ttfb가 크면 서버
        처리, 어느 구간도 아닌데 total만 크면 프로세스 밖 요인(GIL 경합·머신 부하)
        이다. 텍스트 내용은 남기지 않고 건수만 남긴다(원칙7).
        """
        if total_sec < SLOW_EMBED_CALL_LOG_SEC:
            return
        logger.warning(
            "[embed] 느린 호출 %.2f초 (청크 %d개, 시도 %d): "
            "connect=%.2f ttfb=%.2f read=%.2f / 미계측=%.2f",
            total_sec, batch_size, attempt,
            connect_sec, ttfb_sec, read_sec,
            max(total_sec - connect_sec - ttfb_sec - read_sec, 0.0),
        )


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
