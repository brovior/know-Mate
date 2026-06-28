# DESIGN.md — Aegis Desk 설계 결정 상세
> CLAUDE.md의 핵심 원칙 보완 레퍼런스. 구현 시 참고.

---

## 멀티 에이전트 — Block 타입 (`agents/base.py`)

```python
class TextBlock(TypedDict):
    type: Literal["text"];  content: str

class SourceItem(TypedDict):
    badge: str   # "메일" | "문서"
    title: str;  subtitle: str;  score: float;  path: str

class SourcesBlock(TypedDict):
    type: Literal["sources"];  title: str;  items: list[SourceItem]

class TableBlock(TypedDict):
    type: Literal["table"];  title: str;  columns: list[str];  rows: list[list]

class ChartBlock(TypedDict):
    type: Literal["chart"];  chart_type: Literal["line","bar"]
    title: str;  x: list[str];  series: list[dict]

Block = TextBlock | SourcesBlock | TableBlock | ChartBlock
```

---

## LanceDB 스키마 (`rag/indexer.py`)

```python
SCHEMA = pa.schema([
    pa.field("chunk_id",   pa.string()),
    pa.field("file_path",  pa.string()),
    pa.field("file_type",  pa.string()),
    pa.field("scope",      pa.string()),   # 'local' | 'shared'
    pa.field("owner",      pa.string()),
    pa.field("acl_group",  pa.string()),   # Phase 5에서 채움
    pa.field("mtime",      pa.float64()),
    pa.field("indexed_at", pa.string()),
    pa.field("chunk_index",pa.int32()),
    pa.field("chunk_total",pa.int32()),
    pa.field("text",       pa.string()),   # AES-256-GCM 암호화
    pa.field("vector",     pa.list_(pa.float32(), 1024)),
    pa.field("is_deleted", pa.bool_()),
    pa.field("deleted_at", pa.string()),   # soft delete 시각
    pa.field("miss_count", pa.int32()),    # 연속 미발견 횟수
])
```

**Soft delete 동작**
```
파일 없음 감지
  miss_count = 1 → is_deleted=true, deleted_at=now  (검색 제외)
  miss_count ≥ 2 → 물리 삭제 대상
파일 재발견   → miss_count=0, is_deleted=false, deleted_at=""
```

---

## 청킹 파라미터 (`config.yaml › chunking`)

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `chunk_size` | 400 | 한국어 400자 ≈ 200~250 토큰 |
| `overlap` | 80 | chunk_size의 20% |
| `max_file_size_mb` | 30 | **1단**: 파싱 전 크기 초과 시 제외 |
| `xlsx_max_rows_per_sheet` | 2000 | **2단**: 행 초과 시 메타 청크 1개로 대체 |
| `max_chunks_per_file` | 500 | **3단**: 최후 안전망, 초과분 절단 |

**파일타입별 청킹 전략**

| 파일 | 전략 |
|---|---|
| txt/md/log | 빈 줄 기준 단락 분리 → 초과 시 재분할 |
| docx | 문단 합산, 초과 시 새 청크 시작 |
| pdf | 페이지 단위 독립 처리 |
| pptx | 1 슬라이드 = 1 청크 |
| xlsx/xls | 시트별: ≤20행 전체 1청크 / 21~2000행 5행씩 / >2000행 메타 청크 |

---

## 파서 구조 (`secure/`)

```python
def get_reader(path):
    ext = Path(path).suffix.lower()
    if ext in {".doc", ".xls", ".ppt"}:
        return com_reader    # 구형 바이너리 → COM
    return plain_reader      # docx/xlsx/pptx/pdf/txt → 라이브러리
```

**COM 싱글톤 패턴 (★ 반드시 준수)**

```python
class WordComReader:
    _instance = None
    def get_app(self):
        if self._instance is None:
            self._instance = win32com.client.Dispatch("Word.Application")
        return self._instance
    def parse(self, path):
        word = self.get_app()
        try:
            doc = word.Documents.Open(path)
            text = doc.Content.Text
            doc.Close()
            return text
        except Exception:
            self._instance = None   # 예외 시 재생성
            raise
```
> **매번 Quit() 하는 방식 절대 사용 금지.**

**xlsx 손상 복구 (`plain_reader._load_xlsx_sanitized`)**

openpyxl이 `docProps/custom.xml` 타입 오류로 실패하면, custom.xml 파트와 `[Content_Types].xml` 내 해당 Override 엔트리를 함께 제거한 사본으로 재시도.

---

## 검색 파라미터 (`config.yaml › search`)

- `top_k_max`: 10
- `score_threshold`: **0.3** (docx 텍스트 추출 시 유사도가 낮게 나오는 특성 반영. 0.3~0.5 범위에서 튜닝)
- `rerank_enabled`: false (Phase 2 이후 품질 확인 후 결정)
- **샌드위치 배열** (Lost in the Middle 대응):
  ```
  입력 [1위, 2위, 3위, 4위, 5위]
  출력 [1위, 3위, 5위, 4위, 2위]
  ```
- 토큰 근사: `len(text) * 0.75`

---

## Scope 판별 (`collector/scanner.py`)

| 경로 | scope |
|---|---|
| `C:\`, `D:\`, `E:\` (로컬 드라이브) | `local` |
| `\\server\share\` (UNC) | `shared` |
| `Z:\`, `F:\` 등 매핑 드라이브 | `shared` |

---

## Orphan 정리 안전장치 (`collector/cleanup.py`)

1. **폴더 루트 가드**: 감시 폴더 접근 불가 시 해당 폴더 항목 전부 제외 + WARNING
2. **대량 삭제 차단기**: orphan 비율 30% 초과 시 해당 폴더 정리 중단 + ERROR + UI 알림 (`cleanup.max_delete_ratio`)
3. **Soft delete**: orphan 즉시 삭제 않고 `miss_count` 증가 + `is_deleted=true` 마킹, 다음 스캔에서도 없으면 물리 삭제
4. **물리 삭제 후 `optimize()` 호출**
5. **dry-run 모드**: `cleanup.dry_run: true`이면 대상 목록 로그만 출력 (기본값 true)
6. **사이클 리포트**: 스캔N / 신규a / 변경b / 마킹c / 물리삭제d / 스킵 폴더 목록 매 사이클 로그

---

## 스캔 트리거 / 태스크 우선순위

| 구분 | 트리거 |
|---|---|
| 최초 인덱싱 | 온보딩 [인덱싱 시작] 버튼 |
| 증분 인덱싱 | 유휴 감지 (기본 60초, `collector.idle_seconds`) |
| 수동 재인덱싱 | 사이드바 [지금 재인덱싱] 버튼 |

태스크 우선순위: `NEW(1) > MODIFIED(2) > ORPHAN(3)`

---

## 임베딩·LLM 모드

**임베딩** (`rag/embedding.py`, 모델 BAAI/bge-m3, 차원 1024)

| mode | 동작 |
|---|---|
| `fake` | 랜덤 단위벡터 (사외 개발·테스트) |
| `local` | sentence-transformers 로컬 — **폐쇄망 사용 금지** (HuggingFace 다운로드 무한 대기) |
| `api` | 사내 임베딩 API (운영 기본값) |

**LLM** (`llm/client.py`)

| mode | 동작 |
|---|---|
| `fake` | 고정 텍스트 반환 |
| `claude` | Anthropic Claude API |
| `openrouter` | OpenRouter API |
| `api` | 사내 LLM API (운영 기본값) |

---

## 대화 스레드 저장

- 위치: `%APPDATA%/AegisDesk/threads.json`
- 원자적 교체 저장 (임시파일 → replace())
- 모드별 분리 (`"knowledge"`, `"mes"` 키)

---

## Knox 메일 인덱싱 (`Phase 5a`)

상세 설계: `docs/EMAIL_DESIGN.md`

**핵심 결정 요약**

| 항목 | 결정 |
|---|---|
| 소스 | Knox `.mysingle` (RFC822+MIME, 표준 `email` 라이브러리로 파싱) |
| 저장 테이블 | `emails` (기존 `chunks`와 분리, LanceDB 같은 DB 파일) |
| scope | 항상 `local` 고정 (Knox 백업 = 개인 PC) |
| 중복 판별 | `mail_uid` = `knox:{X-Desktop-Msg-UniqueID}` + mtime |
| orphan 정리 | **OFF** (백업저장소 = 파일이 지워져도 인덱스 보존) |
| HTML→텍스트 | stdlib `html.parser` (외부 의존성 없음) |
| 첨부 인덱싱 | 1차 미구현 (본문만). 스키마에 `attach_*` 컬럼 자리 확보됨 |
| Outlook PST | 보류. 스키마에 `source_type`, `message_id`, `source_meta` 자리 확보 |

**활성화**: `config.yaml › mail.enabled: true`
→ `watch_folders` 내 `.mysingle` 파일을 자동으로 메일 파이프라인으로 라우팅

**Retriever 병합**: `chunks` + `emails` score 내림차순 병합 후 top_k 적용, 샌드위치 배열.
