# CLAUDE.md — KnowMate
> 최종 현행화: 2026-06-20 (v2 — 설계 확정 반영)

## 1. 프로젝트 개요

KnowMate는 **개인 PC에 설치되는 사내 지식 AI 비서 데스크톱 앱**이다(독립 프로젝트,
MeMate 코드와 의존 관계 없음). 첫 번째 에이전트로 로컬 문서·메일 RAG 지식검색을
구현하되, 향후 온톨로지 기반 MES 챗봇 등 **다른 에이전트가 같은 UI 셸에서
구동될 수 있는 멀티 에이전트 구조**로 만든다.

핵심 시나리오: 사용자가 "작년 A설비 알람 폭주 때 처리 절차 찾아줘"라고 질문하면
관련 문서 청크를 의미 기반으로 검색해 요약 답변 + 출처 + 일치율을 제시한다.

---

## 2. 아키텍처

```
[개인 PC]
  KnowMate 앱 = PyQt6 + QWebEngineView (HTML/JS UI 임베드)
   ├─ UI 셸: 채팅·입력바·블록 렌더러 (UI_SPEC.md 준수, 에이전트 비종속)
   │    └─ 메인 스레드에서 실행
   ├─ AgentRegistry
   │    ├─ knowledge_agent : RAG 지식검색 (이번에 구현)
   │    └─ mes_agent       : 인터페이스만 정의, 구현은 추후 (placeholder)
   ├─ RAG 파이프라인
   │    ├─ 로컬 벡터DB : %APPDATA%/KnowMate/index   (읽기/쓰기) ← LanceDB
   │    └─ 공용 벡터DB : 네트워크 공유 폴더 경로      (읽기 전용) ← LanceDB
   └─ 수집기 (QThread 워커 — 5장 원칙8)
        ├─ 증분 스캔 → 신규/변경 파일만 추출·임베딩
        ├─ orphan 정리 (8장 안전장치 명세 필수 준수)
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
 │   └─ ui/                # index.html, styles.css, app.js (블록 렌더러)
 │                         #  + mockup.html (룩앤필 레퍼런스)
 │
 ├─ agents/
 │   ├─ base.py            # AgentBackend 인터페이스(Protocol) + Block 타입 정의
 │   ├─ registry.py        # 에이전트 등록·모드 전환
 │   ├─ knowledge_agent.py # RAG 검색 에이전트
 │   └─ mes_agent.py       # placeholder ("준비 중" 블록 반환)
 │
 ├─ rag/
 │   ├─ indexer.py         # 청크 분할 → 임베딩 → LanceDB 저장
 │   ├─ retriever.py       # 로컬+공용 동시 검색, 병합·랭킹, 권한 필터
 │   ├─ embedding.py       # 임베딩 모델 로더 (모델명·차원 단일 상수 고정)
 │   └─ chunker.py         # 형식별 청크 전략 (docx/xlsx/pptx/pdf/txt)
 │
 ├─ collector/
 │   ├─ scanner.py         # 증분 스캔 (mtime+size 상태 비교)
 │   ├─ cleanup.py         # orphan 정리 + 안전장치 (8장 명세)
 │   ├─ state.py           # index_state.json 관리
 │   └─ scheduler.py       # 유휴시간 실행, 태스크 우선순위 큐
 │
 ├─ secure/                # ★ 환경·보안 의존 코드 격리 구역
 │   ├─ README.md          # COM 주의점, DPAPI 등 조사 결과 기록
 │   ├─ base.py            # TextExtractor 인터페이스
 │   ├─ plain_reader.py    # 일반 파일 직접 추출 (python-docx/openpyxl/fitz 등)
 │   ├─ com_reader.py      # 구형 바이너리 COM 추출 (★프로세스 재사용 싱글톤)
 │   ├─ fake_reader.py     # mock: 샘플 텍스트 반환 (사외 개발용)
 │   └─ crypto.py          # text 컬럼 AES-256-GCM, 키는 Windows DPAPI
 │
 ├─ llm/
 │   └─ client.py          # 사내 LLM API 클라이언트
 │                         # (직접 IP base_url + Host 헤더 지정 방식 지원)
 ├─ config.yaml
 ├─ UI_SPEC.md             # 화면 사양 (이 문서를 항상 준수)
 ├─ mockup.html            # UI 룩앤필 레퍼런스 (app/ui/ 에 위치)
 └─ tests/
```

---

## 4. 멀티 에이전트 원칙

1. **AgentBackend 인터페이스**: 모든 에이전트는
   `handle(query: str, context: dict) -> list[Block]` 하나로 통신한다.
   Block 타입은 `agents/base.py`에 TypedDict로 정의하며, UI_SPEC.md 4장의
   JSON 스펙(text/sources/table/chart)과 1:1 대응한다.
2. UI는 어떤 에이전트가 응답했는지 모른 채 블록만 렌더링한다.
   에이전트별 분기 로직을 UI 코드에 넣지 않는다.
3. 사이드바의 모드별 패널은 에이전트가 선언(panel spec)하고 셸이 그린다.
4. mes_agent는 지금 구현하지 않는다. 인터페이스 적합성 테스트용으로
   "준비 중" text 블록만 반환하는 stub으로 둔다.
5. 대화 스레드는 에이전트(모드)별로 분리 저장한다. 저장 방식은 6-12 참조.

### 4-1. Block 타입 정의 (agents/base.py)
```python
from typing import TypedDict, Literal, Protocol

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

class AgentBackend(Protocol):
    def handle(self, query: str, context: dict) -> list[Block]:
        """질문을 받아 UI 블록 리스트를 반환한다."""
        ...
```

---

## 5. 핵심 설계 원칙 (위반 금지)

1. **임베딩 모델·차원은 `rag/embedding.py`에서 단일 상수로 고정.**
   ```python
   EMBEDDING_MODEL = "bge-m3"
   VECTOR_DIM = 1024   # bge-m3 고정 차원. 모델 변경 시 반드시 함께 변경 + 전체 재인덱싱
   ```
   임베딩은 로컬 모델 로드가 아니라 **사내 임베딩 API 호출** 방식이다
   (llm/client.py와 동일하게 base_url + Host 헤더 라우팅). embedding.py는
   "API 클라이언트" 역할을 한다. fake 모드에서는 API를 타지 않고 랜덤 벡터를
   반환해 사외에서도 전체 파이프라인이 돌아야 한다.
2. **환경·보안 의존 코드는 secure/ 밖으로 나가지 않는다.** 다른 모듈은
   TextExtractor 인터페이스만 알고 COM/DPAPI를 직접 import하지 않는다.
3. **mock 전환은 config 한 줄.** `extractor: fake | plain | auto`
   (auto = 확장자로 plain/com 자동 선택). fake 모드에서 Windows COM 없이
   전체 파이프라인·전체 테스트가 동작해야 한다.
4. **벡터DB의 원문(text 컬럼)은 반드시 crypto.py로 암호화 저장.**
   벡터·메타데이터는 평문 허용. 복호화는 검색 결과로 선택된 청크에 한해
   메모리에서만. 복호화 평문을 파일·로그에 남기지 않는다.
5. **권한 메타데이터를 모든 청크에 저장** (file_path, owner,
   scope: local|shared, acl_group). retriever는 반환 전 권한 필터를 거친다.
   로컬 단계에서 owner=현재 사용자, acl_group="" 로 채우되 필드 자리는 확보.
6. **공용 벡터DB에 개인 PC는 절대 쓰기하지 않는다.** 읽기 전용 연결만.
7. **로그에 문서·메일 본문을 출력하지 않는다.** 경로·건수·소요시간만.
8. **수집기는 QThread 워커에서 실행.** 메인 스레드(UI)와 분리하고,
   진행률은 pyqtSignal로 전달. 별도 프로세스(multiprocessing)는 쓰지 않는다
   (LanceDB 파일 락 충돌 회피 + 구현 단순화). 문서가 수십만 규모로 커지면
   그때 프로세스 분리를 재검토한다.

---

## 6. 확정된 설계 결정사항 (검증 완료)

### 6-1. 벡터DB
- **LanceDB 채택** (ChromaDB 검증 후 전환)
- 채택 이유: soft delete / 컬럼 암호화 / `optimize()` 내장

### 6-2. LanceDB 스키마
```python
SCHEMA = pa.schema([
    pa.field("chunk_id",     pa.string()),
    pa.field("file_path",    pa.string()),
    pa.field("file_type",    pa.string()),
    pa.field("scope",        pa.string()),    # 'local' | 'shared'
    pa.field("owner",        pa.string()),    # 파일 소유자 (로컬=현재 사용자)
    pa.field("acl_group",    pa.string()),    # 권한 그룹 (로컬="", Phase5에서 채움)
    pa.field("mtime",        pa.float64()),
    pa.field("indexed_at",   pa.string()),
    pa.field("chunk_index",  pa.int32()),
    pa.field("chunk_total",  pa.int32()),
    pa.field("text",         pa.string()),    # AES-256-GCM 암호화 저장
    pa.field("vector",       pa.list_(pa.float32(), VECTOR_DIM)),  # VECTOR_DIM=1024 (bge-m3)
    pa.field("is_deleted",   pa.bool_()),     # 검색 제외 플래그 (빠른 필터용)
    pa.field("deleted_at",   pa.string()),    # 최초 soft delete 마킹 시각 (없으면 "")
    pa.field("miss_count",   pa.int32()),     # 연속 미발견 횟수 (2회 확인용)
])
```

**soft delete 필드 동작**
```
스캔에서 파일 없음 발견
  → miss_count += 1
  → miss_count == 1 : is_deleted=true, deleted_at=now (마킹만, 검색 제외)
  → miss_count >= 2 : 물리삭제 대상
파일 다시 나타남(부활)
  → miss_count=0, is_deleted=false, deleted_at=""
```

### 6-3. LanceDB API 사용 규칙 (검증 중 발견 — 반드시 준수)
- compact/cleanup은 `optimize()` 사용 (`compact_files()`는 deprecated, 사용 금지)
- DataFrame 변환은 `table.to_arrow().to_pandas()` 사용
  (`table.to_pandas()` 직접 호출 금지 — lance 패키지 충돌)

### 6-4. 청킹 파라미터
- `chunk_size = 400` (한국어 400자 ≈ 200~250 토큰)
- `overlap = 80` (chunk_size의 20%, 경계 문맥 보존)
- config.yaml로 분리해 재인덱싱으로 튜닝 가능하게 한다
- bge-m3는 최대 8192토큰을 받지만, 검색 정밀도를 위해 작은 청크 유지

### 6-5. 파일타입별 청킹 전략
| 파일 | 전략 |
|---|---|
| txt | 빈 줄 기준 단락 분리 → 초과 시 재분할 |
| docx | 문단 리스트 합치다 초과 시 새 청크 |
| pdf | 페이지 단위 독립 처리 → 초과 시 재분할 |
| pptx | 1 슬라이드 = 1 청크 → 초과 시 재분할 |
| xlsx | 소규모(≤20행): 전체 1청크 / 대규모(>20행): 자연어 변환 후 5행씩 |

### 6-6. 파서 구조 (확장자 기반 — NASCA 무관)
**중요**: NASCA 걸린 문서도 python 라이브러리로 파싱된다. COM이 필요한 이유는
NASCA가 아니라 **구형 바이너리 포맷(doc/xls/ppt)이 라이브러리로 안 열리기 때문**이다.
따라서 시그니처 판별(PK 4바이트) 불필요 — 확장자 매핑만으로 판별한다.

| 파일 | 방식 | 모듈 |
|---|---|---|
| docx | python-docx 직접 | plain_reader.py |
| xlsx | openpyxl 직접 | plain_reader.py |
| pptx | python-pptx 직접 | plain_reader.py |
| pdf | PyMuPDF(fitz) 직접 | plain_reader.py |
| doc | win32com COM (Word) | com_reader.py |
| xls | win32com COM (Excel) | com_reader.py |
| ppt | win32com COM (PowerPoint) | com_reader.py |

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
            self._instance = None  # 예외 시 인스턴스 초기화 → 다음 호출에 재생성
            raise
```
> doc/xls/ppt 모두 동일 패턴 적용. 매번 Quit() 하는 방식 절대 사용 금지.

### 6-7. 스캔 트리거
| 구분 | 트리거 |
|---|---|
| 최초 1회 인덱싱 | 온보딩 화면 [인덱싱 시작] 버튼 클릭 |
| 이후 증분 인덱싱 | 유휴시간 감지 (기본 60초, config로 조정 가능) |
| 수동 재인덱싱 | 사이드바 [지금 재인덱싱] 버튼 |

### 6-8. 태스크 우선순위
```
NEW(1) > MODIFIED(2) > ORPHAN(3)
```

### 6-9. scope 판별
| 경로 패턴 | scope |
|---|---|
| `C:\`, `D:\`, `E:\` | local |
| `\\server\share\` (UNC) | shared |
| `Z:\`, `F:\` 등 매핑 드라이브 | shared |

### 6-10. 검색 파라미터
- `TOP_K_MAX = 10`
- 유사도 임계값: `0.3~0.5` (실운영 후 config.yaml로 튜닝)
- 청크 배열: 샌드위치 배열 (Lost in the Middle 대응)
  ```
  입력: [1위, 2위, 3위, 4위, 5위]
  출력: [1위, 3위, 5위, 4위, 2위]  ← 고관련도를 앞뒤에 배치
  ```
- 토큰 근사 추정: `len(text) * 0.75` (한국어 특성 반영)
- **rerank**: 벡터 검색(1단계) 후 cross-encoder 재정렬(2단계)을 붙일 수 있는
  자리를 retriever에 마련하되 **기본 off**. 사내 rerank API(있으면 동일하게
  base_url+Host 방식) 사용. Phase 2에서 실제 검색 품질을 보고 필요하면 켠다.
  미리 켜지 않는다(조기 최적화 방지). config: `search.rerank_enabled: false`

### 6-11. 감시 폴더 관리
- UI(온보딩/사이드바 설정)와 config.yaml 양쪽에서 추가/제거 가능
- UI 변경 시 config.yaml 자동 갱신
- 다음 인덱싱 사이클에 반영

### 6-12. 대화 스레드 저장
- 저장 위치: `%APPDATA%/KnowMate/threads.json` (state.json과 별도 파일)
- 구조: 모드별 분리 → 스레드 배열 → 메시지 배열(질문 + 응답 블록)
  ```json
  {
    "knowledge": [
      { "id": "uuid", "title": "A설비 알람 폭주 처리 절차",
        "created_at": "2026-06-20T11:24:00",
        "messages": [
          { "role": "user", "content": "작년 A설비 알람 폭주 때 처리 절차" },
          { "role": "ai",   "blocks": [ {"type":"text",...}, {"type":"sources",...} ] }
        ] }
    ],
    "mes": []
  }
  ```
- 저장은 **원자적 교체**로 파일 깨짐 방지 (임시파일 작성 → `replace()`)
  ```python
  def save_threads(data):
      tmp = THREADS_FILE.with_suffix(".tmp")
      tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
      tmp.replace(THREADS_FILE)   # 원자적 교체
  ```
- 사이드바 "최근 질문" 클릭 → 해당 스레드 messages를 블록 렌더러로 복원
- **구현 시점**: Phase 1에는 불필요(mock 렌더링이 목표). Phase 2 이후 붙인다.
- **전환 기준**: 스레드가 수천 개를 넘어 로딩이 느려지거나 대화 전문 검색이
  필요해지면 SQLite로 마이그레이션 검토.

---

## 7. 인덱싱 UI 명세

> 룩앤필·인터랙션 상세는 app/ui/mockup.html 을 레퍼런스로 삼는다.

### 온보딩 화면 (최초 1회 — state.json 없을 때 자동 표시)
```
┌─────────────────────────────────────┐
│  KnowMate 첫 설정                    │
│                                     │
│  인덱싱할 폴더를 선택하세요            │
│  [C:/Users/문서        ] [✕]        │
│  [Z:/공유폴더          ] [✕]        │
│  [+ 폴더 추가]                       │
│                                     │
│  예상 문서 수: 약 3,200건            │
│                                     │
│  ████████████░░░░░░░  128/3,200건   │
│  A설비_점검매뉴얼.docx 처리 중...     │
│                                     │
│         [인덱싱 시작]  [취소]        │
└─────────────────────────────────────┘
```
- 재진입: 사이드바 [폴더 관리] 버튼으로 동일 화면 오픈 (타이틀만 "폴더 관리")

### 메인 화면 사이드바 하단
```
─────────────────────────────
✅ 마지막 인덱싱: 오늘 11:24
   문서 3,200건 / 폴더 2개
   [지금 재인덱싱]  [폴더 관리]
─────────────────────────────

↓ 재인덱싱 진행 중

─────────────────────────────
⟳ 인덱싱 중...
██████░░░░░░░░  45/128건
A설비_점검매뉴얼.docx 처리 중...
[취소]
─────────────────────────────
```

- 사이드바 하단 버튼 2개:
  - **[지금 재인덱싱]**: 변경된 파일만 증분 처리. 사이드바 내 프로그레스 바 + 취소
  - **[폴더 관리]**: 온보딩과 동일 오버레이. 폴더 추가/제거 + 전체 재인덱싱
- 프로그레스 바: 건수 + 현재 처리 중인 파일명 표시
- 취소 버튼: 진행 중 언제든 중단 가능, 중단 시점까지 인덱싱된 내용은 유지

---

## 8. Orphan 정리 안전장치 명세 (반드시 전부 구현)

orphan = 인덱스에 있으나 디스크에 없는 파일의 청크.
오동작 시 멀쩡한 인덱스를 통째로 삭제하므로 아래를 전부 구현한다.

1. **폴더 루트 가드**: 감시 폴더 루트가 없거나 접근 불가하면(네트워크
   드라이브 미연결 등) 해당 폴더 소속 항목은 정리에서 전부 제외하고
   WARNING 로그. 정리는 폴더 단위 스코프로 수행한다.
2. **대량 삭제 차단기**: 한 폴더의 orphan 비율이 그 폴더 인덱스의 30%를
   초과하면 해당 폴더 정리를 중단하고 ERROR 로그 + UI 알림.
   임계값은 `cleanup.max_delete_ratio`로 설정 가능.
3. **2단계 삭제(soft delete)**: orphan은 즉시 삭제하지 않고 `deleted_at`
   마킹 + `miss_count` 증가 후 검색에서만 제외. 다음 스캔에서도 디스크에
   없으면(miss_count >= 2) 물리 삭제. 파일이 다시 나타나면 마킹 해제.
4. **잔존 제거**: 물리 삭제한 사이클 말미에 LanceDB `optimize()` 호출로
   삭제 데이터가 파일 내부에 남지 않게 한다.
5. **dry-run 모드**: `cleanup.dry_run: true`면 대상 목록만 로그 출력.
   초기 기본값은 true.
6. **사이클 리포트**: "스캔 N / 신규 a / 변경 b / 마킹 c / 물리삭제 d /
   스킵 폴더 목록"을 매 사이클 로그로 남긴다.

---

## 9. 구현 단계 — 각 Phase 완료 시 멈추고 승인 요청

각 Phase가 끝나면 작업 요약과 실행 방법을 보고하고 승인을 받은 후
다음 Phase로 진행한다. 임의로 다음 단계를 시작하지 않는다.

### Phase 1 — UI 셸 + 에이전트 골격
- **Phase 0 (최우선)**: QWebChannel 브리지 단독 검증. JS↔Python mock 메시지
  한 줄 왕복(전송→시그널 수신→렌더)부터 확인한다. 이게 안 뚫리면 UI 전체가
  막히므로 Phase 1의 실질적 첫 단계로 삼는다.
- PyQt6 + QWebEngineView 셸, UI_SPEC.md·mockup.html대로 레이아웃·블록 렌더러 구현
- AgentBackend/registry, mes_agent stub, knowledge_agent는 고정 mock 블록 반환
- 온보딩 화면 (폴더 선택 + 프로그레스 바 UI)
- 완료 기준: 모드 전환·질문 입력 시 mock의 text/sources 블록이 화면에 렌더링됨

### Phase 2 — RAG 수직 슬라이스 (보안 의존성 없음, fake/plain만)
- chunker, embedding, indexer, retriever 구현, knowledge_agent에 연결
- 완료 기준 (모드별 분리):
  - **fake 모드**: 파이프라인 연결성 검증 — 청킹→임베딩→검색→답변 흐름이
    끊김 없이 도는가 (실제 파싱·문서 불필요)
  - **plain 모드**: 실제 문서 품질 검증 — docx/txt 샘플 10개·질문 5개가
    올바른 출처와 함께 답변됨

### Phase 3 — 수집기 (증분 스캔 + orphan 정리 + 안전장치)
- scanner, state, cleanup, scheduler 구현 (8장 명세 전부 반영)
- QThread 워커 구조 + pyqtSignal 진행률 전달 (5장 원칙8)
- 완료 기준: 파일 추가/수정/삭제 각각에 대한 pytest 통과
  (tmp_path 기반, 네트워크 단절·대량 삭제 시나리오 포함)

### Phase 4 — 보안 모듈
- com_reader(COM 싱글톤 프로세스 재사용), crypto(AES-GCM + DPAPI) 구현
- 완료 기준: fake 모드 테스트 전체 통과 + 회사 PC 수동 검증 체크리스트 제공

### Phase 5 — 공용 벡터DB·메일 커넥터·배포
- 공용 DB 읽기 연결 및 병합 랭킹, acl_group 실제 값 적용
- Outlook MAPI 커넥터 (Knox Mail은 백업 포맷 확인 후 별도 지시)
- 메일 청킹·파서 전략 정의 (별도 emails 테이블 검토)
- PyInstaller 빌드 스크립트 (--onedir). 임베딩이 API 방식이라 모델 파일
  동봉은 불필요

> 메일(.msg) 처리: Phase 5까지 보류. Phase 1~4 동안 sources 블록의 "메일"
> 배지는 mock 데이터로만 표시하고, 실제 메일 인덱싱은 Phase 5에서 구현한다.

---

## 10. 환경 제약

- Windows 10/11, Python 3.11 (`py -3.11 -m venv .venv`)
- 폐쇄망: pip는 사내 PyPI 미러 사용(index-url 끝 슬래시 + trusted-host 전제).
  미러에 없는 패키지가 필요하면 대안을 먼저 제시하고 진행 여부를 물을 것
- **임베딩: 사내 임베딩 API 호출** (로컬 모델 로드 아님). 사내 미러/서버에
  bge-m3 제공. base_url은 LLM과 동일, Host 헤더로 분기
- 주요 라이브러리: PyQt6, PyQt6-WebEngine, lancedb, pyarrow,
  pywin32, cryptography, PyMuPDF, python-docx, openpyxl, python-pptx, pytest
  (임베딩이 API 방식이라 sentence-transformers는 불필요. 단, 사외 fake 모드
  개발에는 어떤 모델도 필요 없음)
- LLM: 사내 LLM API. `llm/client.py`는 직접 IP base_url + Host 헤더 지정
  방식을 설정으로 지원 (nginx 라우팅 환경 대응)
- 테스트는 fake extractor 기준으로 사외 환경에서도 전부 통과해야 함

---

## 11. config.yaml 표준 구조

문서 전체가 참조하는 설정 키를 한 곳에 정의한다. 클로드 코드는 이 키 이름을
그대로 사용하고 임의로 바꾸지 않는다.

```yaml
# 추출 모드
extractor: fake          # fake | plain | auto

# 임베딩 (사내 API 호출 — base_url은 LLM과 동일, Host 헤더로 분기)
embedding:
  base_url: "http://10.x.x.x"   # LLM과 동일
  host_header: "embed.internal" # Host만 다름
  model: "bge-m3"
  batch_size: 32                # 한 번에 보낼 청크 수 (API 배치 지원 여부 확인 후 조정)
  # vector_dim은 embedding.py 상수(1024)로 고정 — config에 두지 않음

# 청킹
chunking:
  chunk_size: 400
  overlap: 80

# 검색
search:
  top_k_max: 10
  score_threshold: 0.4   # 0.3~0.5 범위에서 튜닝
  rerank_enabled: false  # 검색 품질 아쉬우면 그때 true

# 수집기
collector:
  watch_folders:
    - "C:/Users/이재관/Documents"
    - "Z:/부서공유/설비관리"
  idle_seconds: 60

# orphan 정리
cleanup:
  dry_run: true
  max_delete_ratio: 0.30

# LLM
llm:
  base_url: "http://10.x.x.x"
  host_header: "llm.internal"
  model: "qwen3-27b"
```

---

## 12. 코딩 규칙

- 함수는 작고 단일 책임으로. 파일당 300줄 초과 시 분리 제안
- 모든 public 함수에 타입 힌트 + 한 줄 docstring
- 예외는 삼키지 않는다. 수집기는 파일 1건 실패가 사이클 전체를 중단시키지
  않도록 건별 try/except + 실패 목록 리포트
- 설정값 하드코딩 금지, 전부 config.yaml (11장 구조 준수)
- 각 Phase마다 해당 모듈의 pytest 테스트를 함께 작성
- UI 작업 시 UI_SPEC.md·mockup.html을 먼저 읽고, 스펙과 다른 판단이 필요하면
  묻고 진행
