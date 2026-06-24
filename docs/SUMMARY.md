# 변경 사항 요약 (2026-06-25)

## 핵심 원인 요약

| 문제 | 원인 | 해결 |
|---|---|---|
| Embedding 403 (`deny_ads`) | 시스템 프록시가 요청 인터셉트 | `ProxyHandler({})`로 프록시 우회 |
| LLM 403 (`genfab`) | `mode: openrouter`가 외부 서버로 요청 + 프록시 인터셉트 | `mode: api` 변경 + `ProxyHandler({})` + `Authorization` 헤더 |
| 검색 실패 | `pandas 3.0`에서 `_append()` 제거 | `pd.concat()`로 교체 |

---

## 1. `config.yaml` — LLM 모드 변경

```yaml
llm:
  mode: openrouter  →  mode: api
```

`openrouter`는 외부 `openrouter.ai`로 요청을 보냈습니다. `api`로 변경해 사내 서버(`70.220.152.1`)로 직접 연결되도록 했습니다.

---

## 2. `rag/embedding.py` — Embedding API 호출 수정

### 변경 1: `Authorization` 헤더 추가

```python
# __init__에 api_key 파라미터 추가
def __init__(self, base_url, host_header, api_key="dummy", ...):
    self._api_key = api_key

# _call_api() 헤더에 Authorization 추가
headers = {
    "Content-Type": "application/json",
    "Host": self._host_header,
    "Authorization": f"Bearer {self._api_key}",  # ← 추가
}
```

### 변경 2: 프록시 우회

```python
# 변경 전
with urllib.request.urlopen(req, timeout=30) as resp:

# 변경 후
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as resp:
```

### 변경 3: `get_embedding_client()`에서 api_key 전달

```python
api_key = embed_cfg.get("api_key", "dummy"),
```

---

## 3. `llm/client.py` — LLM API 호출 수정

### 변경 1: `_call_api()`에 Authorization 헤더 추가

```python
headers = {
    "Content-Type": "application/json",
    "Host": self._host_header,
    "Authorization": f"Bearer {self._api_key}",  # ← 추가
}
```

### 변경 2: `_call_api()`와 `_call_openrouter()` 모두 프록시 우회

```python
# 변경 전
with urllib.request.urlopen(req, timeout=60) as resp:

# 변경 후
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60) as resp:
```

---

## 4. `rag/retriever.py` — pandas 3.0 호환성 수정

```python
# 변경 전
raw = raw[other_mask]._append(local_filtered)

# 변경 후
import pandas as pd
raw = pd.concat([raw[other_mask], local_filtered])
```

`pandas 3.0`에서 `_append()`가 제거되었습니다.
