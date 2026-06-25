# CLAUDE.md — KnowMate
> 최종 현행화: 2026-06-25 (Phase 1~4 구현 완료 기준)

## 1. 프로젝트 개요

KnowMate는 **개인 PC에 설치되는 사내 지식 AI 비서 데스크톱 앱**이다 (독립 프로젝트, MeMate 코드와 의존 관계 없음). 첫 번째 에이전트로 로컬 문서·메일 RAG 지식검색을 구현하되, 향후 온톨로지 기반 MES 챗봇 등 **다른 에이전트가 같은 UI 셸에서 구동될 수 있는 멀티 에이전트 구조**로 만든다.

핵심 시나리오: 사용자가 "작년 A설비 알람 폭주 때 처리 절차 찾아줘"라고 질문하면 관련 문서 청크를 의미 기반으로 검색해 요약 답변 + 출처 + 일치율을 제시한다.

---

## 2. 아키텍처

```
[개인 PC]
  KnowMate 앱 = PyQt6 + QWebEngineView (HTML/JS UI 임베드)
   ├─ UI 셸: 채팅·입력바·블록 렌더러 (UI_SPEC.md 준수, 에이전트 비종속)
   │    └─ 메인 스레드에서 실행
   ├─ AgentRegistry
   │    ├─ knowledge_agent : RAG 지식검색 (구현 완료)
   │    └─ mes_agent       : stub ("준비 중" 블록 반환)
   ├─ RAG 파이프라인
   │    ├─ 로컬 벡터DB : %APPDATA%/KnowMate/index   (읽기/쓰기) ← LanceDB
   │    └─ 공용 벡터DB : 네트워크 공유 폴더 경로      (읽기 전용) ← LanceDB (Phase 5)
   └─ 수집기 (QThread 워커)
        ├─ 증분 스캔 → 신규/변경 파일만 추출·임베딩
        ├─ orphan 정리 (8장 안전장치 명세 준수)
        ├─ 진행률은 pyqtSignal로 UI에 전달 (현재 파일명 + n/총건수)
        └─ 텍스트 추출은 TextExtractor 인터페이스를 통해서만

[서버/관리 PC] (Phase 5)
  공용 수집기: 부서 공유 폴더 → 공용 벡터DB 쓰기 (쓰기 주체는 이것 하나뿐)
```

---

## 3. 디렉토리 구조

```
knowmate/
 ├─ app/
 │   ├─ main.py            # 진입점, PyQt6 윈도우 + QWebEngineView
 │   ├─ bridge.py          # JS ↔ Python 브리지 (QWebChannel)
 │   ├─ threads.py         # 대화 스레드 저장/불러오기
 │   └─ ui/                # index.html, styles.css, app.js (블록 렌더러)
 │                         #  + mockup.html (룩앤필 레퍼런스)
 │
 ├─ agents/
 │   ├─ base.py            # AgentBackend 인터페이스(Protocol) + Block 타입 정의
 │   ├─ registry.py        # 에이전트 등록·조회
 │   ├─ knowledge_agent.py # RAG 검색 에이전트 (구현 완료)
 │   └─ mes_agent.py       # stub ("준비 중" 블록 반환)
 │
 ├─ rag/
 │   ├─ indexer.py         # 청크 분할 → 임베딩 → LanceDB 저장
 │   ├─ retriever.py       # 벡터 검색, 권한 필터, 샌드위치 배열
 │   ├─ embedding.py       # 임베딩 클라이언트 (fake/local/api 모드)
 │   └─ chunker.py         # 형식별 청크 전략 + 대용량 파일 3단 방어
 │
 ├─ collector/
 │   ├─ scanner.py         # 증분 스캔 (mtime+size 비교, 파일크기 상한)
 │   ├─ cleanup.py         # orphan 정리 + 안전장치 (8장 명세)
 │   ├─ state.py           # index_state.json 관리 (원자적 저장)
 │   └─ scheduler.py       # QThread 워커 + 우선순위 큐 + IdleScheduler
 │
 ├─ secure/                # ★ 환경·보안 의존 코드 격리 구역
 │   ├─ README.md          # COM 주의점, DPAPI 등 조사 결과 기록
 │   ├─ base.py            # TextExtractor 인터페이스
 │   ├─ plain_reader.py    # python-docx/openpyxl/fitz 직접 파싱
 │   │                     #  xlsx: 시트 구분자(=== 시트: ... ===) 삽입
 │   │                     #  xlsx 손상(custom.xml) 자동 복구
 │   ├─ com_reader.py      # 구형 바이너리 COM 추출 (★싱글톤 프로세스 재사용)
 │   ├─ fake_reader.py     # mock: 샘플 텍스트 반환 (사외 개발용)
 │   ├─ crypto.py          # text 컬럼 AES-256-GCM, 키는 Windows DPAPI
 │   ├─ signature.py       # 파일 서명 유틸
 │   └─ text_util.py       # 텍스트 전처리 유틸
 │
 ├─ llm/
 │   └─ client.py          # LLM 클라이언트 (fake/claude/openrouter/api 모드)
 │
 ├─ config.py              # config.yaml 싱글톤 로더
 ├─ config.yaml            # 전체 런타임 설정 (11장 참조)
 ├─ UI_SPEC.md             # 화면 사양 (이 문서를 항상 준수)
 └─ tests/
     ├─ test_phase1.py     # UI 셸 + 에이전트 골격
     ├─ test_phase2.py     # RAG 파이프라인
     ├─ test_phase3.py     # 수집기 + orphan 정리
     └─ test_phase4.py     # 보안 모듈
```

---

## 4. 멀티 에이전트 원칙

1. **AgentBackend 인터페이스**: 모든 에이전트는 `handle(query: str, context: dict) -> list[Block]` 하나로 통신한다. Block 타입은 `agents/base.py`에 TypedDict로 정의하며, UI_SPEC.md 4장의 JSON 스펙(text/sources/table/chart)과 1:1 대응한다.
2. UI는 어떤 에이전트가 응답했는지 모른 채 블록만 렌더링한다. 에이전트별 분기 로직을 UI 코드에 넣지 않는다.
3. 사이드바의 모드별 패널은 에이전트가 선언(panel spec)하고 셸이 그린다.
4. `mes_agent`는 지금 구현하지 않는다. "준비 중" text 블록만 반환하는 stub으로 둔다.
5. 대화 스레드는 에이전트(모드)별로 분리 저장한다. (app/threads.py, %APPDATA%/KnowMate/threads.json)

### 4-1. Block 타입 정의 (agents/base.py)

```python
class TextBlock(TypedDict):
    type: Literal["text"]
    content: str

class SourceItem(TypedDict):
    badge: str          # "메일" | "문서"
    title: str
    subtitle: str
    score: float
    path: str

class SourcesBlock(TypedDict):
    type: Literal["sources"]
    title: str
    items: list[SourceItem]

class TableBlock(TypedDict):
    type: Literal["table"]
    title: str
    columns: list[str]
    rows: list[list]

class ChartBlock(TypedDict):
    type: Literal["chart"]
    chart_type: Literal["line", "bar"]
    title: str
    x: list[str]
    series: list[dict]

Block = TextBlock | SourcesBlock | TableBlock | ChartBlock
```

---

## 5. 핵심 설계 원칙 (위반 금지)

1. **임베딩 모델·차원은 `rag/embedding.py`에서 단일 상수로 고정.**
   ```python
   EMBEDDING_MODEL = "bge-m3"
   MODEL_DIMS = {"bge-m3": 1024}
   VECTOR_DIM = MODEL_DIMS[EMBEDDING_MODEL]  # 모델 변경 시 반드시 함께 변경 + 전체 재인덱싱
   ```
   임베딩은 사내 임베딩 API 호출 방식이 기본 (llm/client.py와 동일하게 base_url + Host 헤더 라우팅). fake 모드에서는 랜덤 벡터를 반환한다.

2. **환경·보안 의존 코드는 secure/ 밖으로 나가지 않는다.** 다른 모듈은 TextExtractor 인터페이스만 알고 COM/DPAPI를 직접 import하지 않는다.

3. **mock 전환은 config 한 줄.** `extractor: fake | plain | auto`. fake 모드에서 Windows COM 없이 전체 파이프라인·전체 테스트가 동작해야 한다.

4. **벡터DB의 원문(text 컬럼)은 반드시 crypto.py로 암호화 저장.** 복호화는 검색 결과로 선택된 청크에 한해 메모리에서만. 복호화 평문을 파일·로그에 남기지 않는다.

5. **권한 메타데이터를 모든 청크에 저장** (file_path, owner, scope: local|shared, acl_group). retriever는 반환 전 권한 필터를 거친다.

6. **공용 벡터DB에 개인 PC는 절대 쓰기하지 않는다.** 읽기 전용 연결만.

7. **로그에 문서·메일 본문을 출력하지 않는다.** 경로·건수·소요시간만.

8. **수집기는 QThread 워커에서 실행.** 메인 스레드(UI)와 분리하고, 진행률은 pyqtSignal로 전달. 별도 프로세스(multiprocessing)는 쓰지 않는다 (LanceDB 파일 락 충돌 회피 + 구현 단순화).

9. **검색 범위(scopes)가 빈 배열이면 전체 검색으로 fallback하지 않는다.** JS에서 1차 차단 후 채팅창에 안내 메시지를 표시하고, knowledge_agent에서도 동일하게 처리한다.

---

## 6. 확정된 설계 결정사항

### 6-1. 벡터DB
- **LanceDB 채택**
- 이유: soft delete / 컬럼 암호화 / `optimize()` 내장

### 6-2. LanceDB 스키마

```python
SCHEMA = pa.schema([
    pa.field("chunk_id",     pa.string()),
    pa.field("file_path",    pa.string()),
    pa.field("file_type",    pa.string()),
    pa.field("scope",        pa.string()),    # 'local' | 'shared'
    pa.field("owner",        pa.string()),
    pa.field("acl_group",    pa.string()),    # 로컬="", Phase 5에서 채움
    pa.field("mtime",        pa.float64()),
    pa.field("indexed_at",   pa.string()),
    pa.field("chunk_index",  pa.int32()),
    pa.field("chunk_total",  pa.int32()),
    pa.field("text",         pa.string()),    # AES-256-GCM 암호화 저장
    pa.field("vector",       pa.list_(pa.float32(), VECTOR_DIM)),
    pa.field("is_deleted",   pa.bool_()),
    pa.field("deleted_at",   pa.string()),    # 최초 soft delete 시각 (없으면 "")
    pa.field("miss_count",   pa.int32()),     # 연속 미발견 횟수
])
```

**soft delete 동작**
```
스캔에서 파일 없음 발견
  → miss_count == 1 : is_deleted=true, deleted_at=now (검색 제외)
  → miss_count >= 2 : 물리삭제 대상
파일 다시 나타남 → miss_count=0, is_deleted=false, deleted_at=""
```

### 6-3. LanceDB API 사용 규칙
- compact/cleanup은 `optimize()` 사용 (`compact_files()`는 deprecated, 사용 금지)
- DataFrame 변환은 `table.to_arrow().to_pandas()` 사용 (`table.to_pandas()` 직접 호출 금지)

### 6-4. 청킹 파라미터

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `chunk_size` | 400 | 한국어 400자 ≈ 200~250 토큰 |
| `overlap` | 80 | chunk_size의 20%, 경계 문맥 보존 |
| `max_file_size_mb` | 30 | **1단**: 파싱 전 파일 크기 초과 시 제외 |
| `xlsx_max_rows_per_sheet` | 2000 | **2단**: xlsx 시트 행 수 초과 시 메타 청크 1개로 대체 |
| `max_chunks_per_file` | 500 | **3단**: 모든 형식 공통 최후 안전망 |

**대용량 xlsx 처리 (3단 방어)**

157,000행짜리 시트처럼 RAG에 부적합한 대용량 데이터 파일이 수시간 동안 인덱싱을 점유하는 문제를 방어한다:
1. 파일이 30MB 초과이면 스캔 단계에서 제외 (WARNING 로그)
2. xlsx 시트가 2,000행 초과이면 메타 청크 1개로 대체 (`[시트: 이름] N행 데이터 시트 (인덱싱 생략). 컬럼 정보: ...`)
3. 청크 수가 500 초과이면 절단 (WARNING 로그)

### 6-5. 파일타입별 청킹 전략

| 파일 | 전략 |
|---|---|
| txt/md/log | 빈 줄 기준 단락 분리 → 초과 시 재분할 |
| docx | 문단 리스트 합치다 초과 시 새 청크 |
| pdf | 페이지 단위 독립 처리 → 초과 시 재분할 |
| pptx | 1 슬라이드 = 1 청크 → 초과 시 재분할 |
| xlsx/xls | 시트별 분리 (plain_reader가 `=== 시트: 이름 ===` 헤더 삽입) → ≤20행: 전체 1청크 / 21~2000행: 5행씩 / >2000행: 메타 청크 |

### 6-6. 파서 구조

```python
def get_reader(path: str):
    ext = Path(path).suffix.lower()
    if ext in {".doc", ".xls", ".ppt"}:
        return com_reader     # 구형 바이너리 → COM
    else:
        return plain_reader   # docx/xlsx/pptx/pdf/txt → 라이브러리
```

**★ COM 방식 필수 구현 패턴 (싱글톤 프로세스 재사용)**

```python
class WordComReader:
    _instance = None
    def get_app(self):
        if self._instance is None:
            self._instance = win32com.client.Dispatch("Word.Application")
        return self._instance
    def parse(self, path: str) -> str:
        word = self.get_app()
        try:
            doc = word.Documents.Open(path)
            text = doc.Content.Text
            doc.Close()
            return text
        except Exception:
            self._instance = None  # 예외 시 재생성
            raise
```
> 매번 Quit() 하는 방식 절대 사용 금지.

### 6-7. xlsx 손상 복구

openpyxl이 `docProps/custom.xml`의 알 수 없는 타입으로 실패하면, custom.xml 파트와 `[Content_Types].xml` 내 해당 Override 엔트리를 함께 제거한 사본으로 재시도한다. (`plain_reader._load_xlsx_sanitized()`)

### 6-8. 스캔 트리거

| 구분 | 트리거 |
|---|---|
| 최초 1회 인덱싱 | 온보딩 화면 [인덱싱 시작] 버튼 클릭 |
| 이후 증분 인덱싱 | 유휴시간 감지 (기본 60초, config로 조정) |
| 수동 재인덱싱 | 사이드바 [지금 재인덱싱] 버튼 |

### 6-9. 태스크 우선순위
```
NEW(1) > MODIFIED(2) > ORPHAN(3)
```

### 6-10. scope 판별

| 경로 패턴 | scope |
|---|---|
| `C:\`, `D:\`, `E:\` | local |
| `\\server\share\` (UNC) | shared |
| `Z:\`, `F:\` 등 매핑 드라이브 | shared |

### 6-11. 검색 파라미터

- `TOP_K_MAX = 10`
- 유사도 임계값: `0.4` (config.yaml로 튜닝)
- 청크 배열: 샌드위치 배열 (Lost in the Middle 대응)
  ```
  입력: [1위, 2위, 3위, 4위, 5위]
  출력: [1위, 3위, 5위, 4위, 2위]
  ```
- 토큰 근사 추정: `len(text) * 0.75` (한국어 특성 반영)
- **rerank**: 기본 off. Phase 2 이후 검색 품질 확인 후 결정 (`search.rerank_enabled: false`)

### 6-12. 감시 폴더 관리
- UI(온보딩/사이드바 설정)와 config.yaml 양쪽에서 추가/제거 가능
- UI 변경 시 config.yaml 자동 갱신
- 다음 인덱싱 사이클에 반영

### 6-13. 대화 스레드 저장
- 저장 위치: `%APPDATA%/KnowMate/threads.json`
- 원자적 교체 저장 (임시파일 → replace())
- 모드별 분리 (`"knowledge"`, `"mes"` 키)
- 사이드바 "최근 질문" 클릭 → 해당 스레드 복원

### 6-14. 임베딩·LLM 모드

**임베딩 (rag/embedding.py)**

| mode | 동작 |
|---|---|
| `fake` | 랜덤 단위벡터 반환, API 불필요 (사외 개발·테스트용) |
| `local` | sentence-transformers 로컬 모델. **폐쇄망에서 주의**: 최초 실행 시 Hugging Face에서 모델 다운로드 시도. 다운로드 실패 시 무한 대기 발생. |
| `api` | 사내 임베딩 API 호출 (base_url + Host 헤더 라우팅, **운영 기본값**) |

**LLM (llm/client.py)**

| mode | 동작 |
|---|---|
| `fake` | 고정 텍스트 반환 |
| `claude` | Anthropic Claude API |
| `openrouter` | OpenRouter API (OpenAI 호환) |
| `api` | 사내 LLM API 호출 (base_url + Host 헤더, **운영 기본값**) |

---

## 7. 인덱싱 UI 명세

> 룩앤필·인터랙션 상세는 `app/ui/mockup.html`을 레퍼런스로 삼는다.

### 온보딩 화면 (최초 1회 — state.json 없을 때 자동 표시)
```
┌─────────────────────────────────────┐
│  KnowMate 첫 설정                    │
│  인덱싱할 폴더를 선택하세요            │
│  [C:/Users/문서        ] [✕]        │
│  [Z:/공유폴더          ] [✕]        │
│  [+ 폴더 추가]                       │
│  ████████████░░░░░░░  128/3,200건   │
│  A설비_점검매뉴얼.docx 처리 중...     │
│         [인덱싱 시작]  [취소]        │
└─────────────────────────────────────┘
```

### 메인 화면 사이드바 하단
```
─────────────────────────────
✅ 마지막 인덱싱: 오늘 11:24
   문서 3,200건 / 폴더 2개
   [지금 재인덱싱]  [폴더 관리]
─────────────────────────────
```

---

## 8. Orphan 정리 안전장치 명세 (반드시 전부 구현)

1. **폴더 루트 가드**: 감시 폴더 루트가 없거나 접근 불가하면 해당 폴더 소속 항목은 정리에서 전부 제외하고 WARNING 로그.
2. **대량 삭제 차단기**: 한 폴더의 orphan 비율이 30% 초과이면 해당 폴더 정리를 중단하고 ERROR 로그 + UI 알림. 임계값은 `cleanup.max_delete_ratio`로 설정 가능.
3. **2단계 삭제(soft delete)**: orphan은 즉시 삭제하지 않고 `deleted_at` 마킹 + `miss_count` 증가 후 검색에서만 제외. 다음 스캔에서도 없으면 물리 삭제.
4. **잔존 제거**: 물리 삭제 후 `optimize()` 호출.
5. **dry-run 모드**: `cleanup.dry_run: true`이면 대상 목록만 로그 출력. 초기 기본값 true.
6. **사이클 리포트**: 스캔N / 신규a / 변경b / 마킹c / 물리삭제d / 스킵 폴더 목록을 매 사이클 로그로 남긴다.

---

## 9. 구현 단계

### ✅ Phase 1 — UI 셸 + 에이전트 골격 (완료)
- PyQt6 + QWebEngineView 셸
- AgentBackend/registry, mes_agent stub, knowledge_agent mock 블록 반환
- 온보딩 화면 (폴더 선택 + 프로그레스 바)

### ✅ Phase 2 — RAG 수직 슬라이스 (완료)
- chunker, embedding, indexer, retriever 구현
- knowledge_agent에 연결 (fake/plain 모드 모두 동작)

### ✅ Phase 3 — 수집기 (완료)
- scanner, state, cleanup, scheduler 구현 (8장 명세 전부 반영)
- QThread 워커 구조 + pyqtSignal 진행률 전달

### ✅ Phase 4 — 보안 모듈 (완료)
- com_reader (COM 싱글톤 프로세스 재사용)
- crypto (AES-GCM + DPAPI)

### 🔲 Phase 5 — 공용 벡터DB·메일 커넥터·배포
- 공용 DB 읽기 연결 및 병합 랭킹, acl_group 실제 값 적용
- Outlook MAPI 커넥터 (Knox Mail은 백업 포맷 확인 후 별도 지시)
- PyInstaller 빌드 스크립트 (--onedir)

> 메일(.msg) 처리: Phase 5까지 보류. sources 블록의 "메일" 배지는 mock 데이터로만 표시.

---

## 10. 환경 제약

- Windows 10/11, Python 3.11 (`py -3.11 -m venv .venv`)
- 폐쇄망: pip는 사내 PyPI 미러 사용 (index-url 끝 슬래시 + trusted-host 전제)
- **임베딩**: 운영은 반드시 `mode: api`. `local` 모드는 폐쇄망에서 모델 다운로드 불가로 무한 대기가 발생하므로 사용 금지.
- 주요 라이브러리: PyQt6, PyQt6-WebEngine, lancedb, pyarrow, pywin32, cryptography, PyMuPDF, python-docx, openpyxl, python-pptx, pytest
- 테스트는 fake extractor 기준으로 사외 환경에서도 전부 통과해야 함

---

## 11. config.yaml 표준 구조

```yaml
log_level: INFO          # DEBUG | INFO | WARNING | ERROR

extractor: auto          # fake | plain | auto

embedding:
  mode: api              # fake | local | api  ← 운영은 반드시 api
  local_model: BAAI/bge-m3
  base_url: "http://10.x.x.x"
  host_header: "embed.internal"
  api_key: ""            # 비우면 EMBED_API_KEY 환경변수 → dummy 순
  batch_size: 32
  # vector_dim은 embedding.py 상수(1024)로 고정 — config에 두지 않음

chunking:
  chunk_size: 400
  overlap: 80
  max_file_size_mb: 30           # 1단: 파싱 전 파일 크기 상한
  xlsx_max_rows_per_sheet: 2000  # 2단: xlsx 시트 행 수 상한 (메타 청크 대체)
  max_chunks_per_file: 500       # 3단: 파일당 최대 청크 수 (최후 안전망)

search:
  top_k_max: 10
  score_threshold: 0.4   # 0.3~0.5 범위에서 튜닝
  rerank_enabled: false

collector:
  watch_folders:
    - "C:/Users/.../Documents"
    - "Z:/부서공유/설비관리"
  idle_seconds: 60

cleanup:
  dry_run: true          # 초기값 true. 운영 검증 후 false로 전환
  max_delete_ratio: 0.30

llm:
  mode: api              # fake | claude | openrouter | api
  model: "qwen3-27b"
  api_key: ""
  base_url: "http://10.x.x.x"
  host_header: "llm.internal"
  max_context_tokens: 4096
```

---

## 12. 코딩 규칙

- 함수는 작고 단일 책임으로. 파일당 300줄 초과 시 분리 제안
- 모든 public 함수에 타입 힌트 + 한 줄 docstring
- 예외는 삼키지 않는다. 수집기는 파일 1건 실패가 사이클 전체를 중단시키지 않도록 건별 try/except + 실패 목록 리포트
- 설정값 하드코딩 금지, 전부 config.yaml (11장 구조 준수)
- 각 Phase마다 해당 모듈의 pytest 테스트를 함께 작성
- UI 작업 시 UI_SPEC.md·mockup.html을 먼저 읽고, 스펙과 다른 판단이 필요하면 묻고 진행
- 로그 레벨 기준: DEBUG(단계별 흐름 추적) / INFO(정상 처리 결과) / WARNING(복구 가능한 이상) / ERROR(즉시 확인 필요)
