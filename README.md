# Aegis Desk

사내 문서를 AI로 검색하는 개인 PC용 데스크톱 지식 비서입니다.

> **Aegis Desk (이지스 데스크)** — 그리스 신화의 방패 '이지스'에서 영감을 받아, 강력한 암호화로 원문을 안전하게 보호하면서 스마트하게 답변하는 철벽 비서. (구 KnowMate)

"작년 A설비 알람 폭주 때 처리 절차 찾아줘"처럼 자연어로 질문하면, 인덱싱된 사내 문서에서 관련 내용을 찾아 요약 답변과 출처를 제시합니다.

---

## 주요 기능

- **자연어 문서 검색**: bge-m3 임베딩 기반 의미 검색 (벡터 유사도)
- **다양한 파일 형식 지원**: docx, xlsx, pptx, pdf, txt, doc, xls, ppt
- **증분 인덱싱**: 변경된 파일만 재인덱싱 (mtime + size 비교)
- **검색 범위 선택**: 내 PC 문서 / 공유 폴더 체크박스 전환
- **출처 표시**: 답변에 사용된 문서 파일명, 경로, 일치율 제공
- **대화 스레드**: 이전 질문 목록 저장 및 복원
- **암호화 저장**: 벡터DB 내 원문을 AES-256-GCM으로 암호화

---

## 아키텍처

```
PyQt6 + QWebEngineView
  └─ HTML/JS UI (채팅 인터페이스)
       ↕ QWebChannel (JS ↔ Python 브리지)
  └─ AgentRegistry
       └─ KnowledgeAgent
            ├─ Retriever → LanceDB (로컬/공용 벡터DB)
            └─ LLMClient → 사내 LLM API
  └─ CollectorWorker (QThread)
       └─ Scanner → TextExtractor → Indexer → LanceDB
```

---

## 환경 요구사항

- **OS**: Windows 10 / 11
- **Python**: 3.11
- **네트워크**: 사내망 (임베딩 API, LLM API 접근 필요)

---

## 설치 및 실행

```bash
# 1. 가상환경 생성
py -3.11 -m venv .venv
.venv\Scripts\activate

# 2. 패키지 설치 (사내 PyPI 미러 사용)
pip install -r requirements.txt

# 3. config.yaml 설정
# embedding.base_url, llm.base_url에 사내 서버 IP 입력
# embedding.mode: api, llm.mode: api 확인

# 4. 실행
python -m knowmate.app.main
```

---

## 설정 (config.yaml)

| 키 | 기본값 | 설명 |
|---|---|---|
| `log_level` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `extractor` | `auto` | fake / plain / auto (확장자 자동 선택) |
| `embedding.mode` | `api` | fake / local / **api** (운영은 반드시 api) |
| `embedding.base_url` | - | 사내 임베딩 서버 IP |
| `chunking.chunk_size` | `400` | 청크 크기 (한국어 기준 자) |
| `chunking.max_file_size_mb` | `30` | 파일 크기 상한 (초과 시 인덱싱 제외) |
| `chunking.xlsx_max_rows_per_sheet` | `2000` | xlsx 시트 행 수 상한 |
| `chunking.max_chunks_per_file` | `500` | 파일당 최대 청크 수 |
| `search.score_threshold` | `0.4` | 유사도 임계값 (0.3~0.5) |
| `collector.watch_folders` | - | 인덱싱할 폴더 목록 |
| `collector.idle_seconds` | `60` | 유휴 감지 후 자동 인덱싱 간격 |
| `cleanup.dry_run` | `true` | true이면 orphan 삭제 시뮬레이션만 수행 |
| `llm.mode` | `api` | fake / claude / openrouter / **api** |
| `llm.base_url` | - | 사내 LLM 서버 IP |
| `llm.model` | `qwen3-27b` | 사용할 모델명 |

> `embedding.mode: local`은 폐쇄망에서 Hugging Face 모델 다운로드를 시도하므로 사용하지 마세요.

---

## 지원 파일 형식

| 확장자 | 파서 | 비고 |
|---|---|---|
| docx, xlsx, pptx, pdf, txt | python-docx / openpyxl / python-pptx / PyMuPDF | 직접 파싱 |
| doc, xls, ppt | win32com (COM) | 구형 바이너리, Windows 전용 |

xlsx 파일에서 `docProps/custom.xml` 손상으로 인한 로드 실패는 자동 복구합니다.

---

## 대용량 파일 처리

대용량 데이터 파일이 인덱싱을 수시간 점유하는 문제를 3단으로 방어합니다:

1. **파일 크기 상한** (`max_file_size_mb: 30`): 파싱 전에 제외
2. **xlsx 시트 행 수 상한** (`xlsx_max_rows_per_sheet: 2000`): 초과 시 메타 청크 1개로 대체
3. **파일당 최대 청크 수** (`max_chunks_per_file: 500`): 초과분 절단

---

## 테스트

```bash
# fake 모드로 사외 환경에서도 전체 테스트 실행 가능
pytest knowmate/tests/ -v
```

Phase별 테스트 파일:
- `test_phase1.py` — UI 셸 + 에이전트 골격
- `test_phase2.py` — RAG 파이프라인 (chunker / embedding / indexer / retriever)
- `test_phase3.py` — 수집기 + orphan 정리
- `test_phase4.py` — 보안 모듈 (COM / crypto)

---

## 디버깅

```yaml
# config.yaml에서 로그 레벨 변경
log_level: DEBUG
```

인덱싱이 특정 파일에서 멈출 경우 DEBUG 로그로 어느 단계에서 블로킹되는지 확인할 수 있습니다:

```
[단계1] 텍스트 추출 시작
[단계2] 텍스트 추출 완료
[단계4] 임베딩·저장 시작  ← 여기서 멈추면 임베딩 API 또는 LanceDB 저장 문제
[단계5] 임베딩·저장 완료
```

---

## 구현 현황

| Phase | 내용 | 상태 |
|---|---|---|
| Phase 1 | UI 셸 + 에이전트 골격 | ✅ 완료 |
| Phase 2 | RAG 파이프라인 | ✅ 완료 |
| Phase 3 | 수집기 (증분 스캔 + orphan 정리) | ✅ 완료 |
| Phase 4 | 보안 모듈 (COM + AES-GCM + DPAPI) | ✅ 완료 |
| Phase 5 | 공용 벡터DB + 메일 커넥터 + 배포 | 🔲 예정 |

---

## 보안

- 벡터DB 내 원문(`text` 컬럼)은 AES-256-GCM으로 암호화 저장
- 암호화 키는 Windows DPAPI로 보호 (해당 PC, 해당 사용자만 복호화 가능)
- 공용 벡터DB는 읽기 전용 연결만 허용 (개인 PC에서 쓰기 불가)
- 로그에 문서 본문을 출력하지 않음 (경로·건수·소요시간만)
