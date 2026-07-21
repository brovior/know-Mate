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

**Office 점유 가드 (`secure/office_guard.py`)**

COM 자동화는 대상 Office 프로세스가 이미 떠 있으면 그 인스턴스에 붙는다(사용자당 1 인스턴스). 백그라운드
인덱싱이 사용자가 열어둔 창을 점유해 응답없음을 유발하는 것을 막기 위해, `AutoReader`는 COM 라우팅
(`.doc/.xls/.ppt` + OLE2 오라벨 docx) **직전** `is_office_busy_for_ext(ext)`로 대상 앱 실행 여부를
확인한다. 실행 중이면 `OfficeBusyError`를 던져 그 확장자만 **이번 사이클에서 연기**하고, 소비자
루프(`scheduler`)는 이를 실패가 아닌 연기로 처리(state 미갱신 → 다음 유휴 사이클 자동 재시도). 감지는
Toolhelp32 **프로세스 열거만** 수행하며 COM 객체를 생성·연결하지 않는다(사용자 창 무간섭). 비Windows·
열거 실패 시 차단하지 않아(기존 동작 유지) 사외 테스트에 영향 없다. 정상 OOXML(docx 등)은 라이브러리
파싱이라 가드 대상이 아니다.

**xlsx 손상 복구 (`plain_reader._load_xlsx_sanitized`)**

openpyxl이 `docProps/custom.xml` 타입 오류로 실패하면, custom.xml 파트와 `[Content_Types].xml` 내 해당 Override 엔트리를 함께 제거한 사본으로 재시도.

---

## 검색 파라미터 (`config.yaml › search`)

- `top_k_max`: 10 (설정 패널에서 3~20 조정)
- `score_threshold`: **0.3** (검색 엄격도. 설정 패널에서 0~0.7 조정)
- `rerank_enabled`: false (Phase 2 이후 품질 확인 후 결정)
- **샌드위치 배열** (Lost in the Middle 대응):
  ```
  입력 [1위, 2위, 3위, 4위, 5위]
  출력 [1위, 3위, 5위, 4위, 2위]
  ```
- 토큰 근사: `len(text) * 0.75`
- **파일명·경로 임베딩**: `indexer.index_file`이 청킹 전 본문 앞에 `파일명/경로` 헤더를 삽입 → 제목·폴더명 언급 질의가 벡터 검색에 매칭.
- **키워드 보강**: `retriever._keyword_where`가 질의 토큰과 `file_path`가 `LIKE` 매칭되는 문서 청크를 점수 문턱 완화로 강제 포함(파일명 직접 지목 질의 대응).
- **현재 날짜 주입**: `llm/client.py`가 시스템 프롬프트에 "오늘은 YYYY-MM-DD (요일), ISO N주차" 문맥을 붙여 "어제/저번주/N주차/최근" 등 상대 날짜를 해석 — **검색된 결과**의 날짜를 LLM이 이해하는 용도.
- **날짜 기반 검색 필터** (`rag/date_filter.py`, LLM 날짜 주입과는 별개 — 검색 **대상 자체**를 기간으로 선별):
  - `parse_date_range_ko(query, now)`가 오늘/어제/그저께·이번주/지난주/지지난주·이번달/지난달·올해/작년·N월·N주차(ISO)·최근 N일/주/개월을 규칙기반으로 (start_epoch, end_epoch)로 변환. 순수 datetime, 의존성 0, 미매칭 시 None → 일반 검색 폴백.
  - 문서(chunks)는 `mtime`(파일 수정일 근사)을, 메일(emails)은 `mail_date_ts`(RFC Date 헤더를 `email.utils.parsedate_to_datetime`으로 파싱한 epoch)를 날짜 컬럼으로 사용.
  - `retriever.search`가 날짜 범위를 `extra_where`로 각 테이블에 적용하고, 날짜 필터 적용 시 `score_threshold`를 건너뛰고 후보 수(`limit_override`)를 `top_k*5`로 올려 해당 기간을 폭넓게 회수.
  - LLM 추출이 아닌 규칙기반을 택한 이유: 결정적·사외 완전 테스트 가능(원칙4), 검색 전 LLM 호출 추가 회피.
- **문서 인덱스 버전**: `DOC_INDEX_VERSION`(state.index_version). 인덱싱 포맷 변경 시 mtime 불변이어도 자동 1회 재인덱싱.

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
5. **dry-run 모드**: `cleanup.dry_run: true`이면 대상 목록 로그만 출력 (기본값 true). 설정 패널에선 "제거된 폴더 데이터 자동 삭제" 토글(긍정형)로 노출.
6. **사이클 리포트**: 스캔N / 신규a / 변경b / 마킹c / 물리삭제d / 스킵 폴더 목록 매 사이클 로그

**제거된 폴더 정리** (`scheduler._purge_removed_folders`, cleanup.py와 별개):
watch_folders에서 빠진 폴더의 청크를 DB `file_path` 기준으로 정리. 동일 안전장치 적용 —
① `watch_folders`가 비면 즉시 스킵(빈 목록을 "전부 삭제"로 오판 방지) ② dry_run이면 state·DB 모두 불변
③ 삭제 대상이 `max_delete_ratio` 초과 시 차단 + UI 알림.

---

## 스캔 트리거 / 태스크 우선순위

| 구분 | 트리거 |
|---|---|
| 최초 인덱싱 | 온보딩 [인덱싱 시작] 버튼 |
| 증분 인덱싱 | 유휴 감지 (기본 60초, `collector.idle_seconds`) |
| 수동 재인덱싱 | 사이드바 [지금 재인덱싱] 버튼 |

태스크 우선순위: `NEW(1) > MODIFIED(2) > ORPHAN(3)`

**유휴 감지 (`collector/idle_util.py` + `scheduler.IdleScheduler`)**

`get_idle_seconds()`가 Windows `GetLastInputInfo`(시스템 전역 마지막 입력 이후 경과초)를
**읽기 전용**으로 조회한다 — 입력을 발생시키거나 시스템 유휴 타이머를 리셋하지 않는다. 트레이
상주 앱이라 창이 비활성/최소화 상태여도 감지해야 하므로 Qt 이벤트 필터(포커스 필요) 대신 이
시스템 API를 쓴다. `IdleScheduler`는 타이머 만료 시 이 값을 확인해 `idle_seconds` 임계 이상일
때만 트리거하고, 미달이면(사용자 작업 중) 남은 시간만큼만 재확인을 예약한다(최소 재확인 간격
`_MIN_RECHECK_SECONDS=5s`). 비Windows·조회 실패 시 0.0(항상 "방금 활동함")을 반환해 안전하게
트리거를 억제한다.

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
| 소스 | Knox `.mysingle` + 표준 `.eml` (RFC822+MIME, `parse_mail_file`로 파싱) |
| 저장 테이블 | `emails` (기존 `chunks`와 분리, LanceDB 같은 DB 파일) |
| scope | 항상 `local` 고정 (개인 PC) |
| 중복 판별 | `mail_uid`(`knox:`/`eml:` 접두) + mtime + `_index_version` |
| orphan 정리 | **OFF** (백업저장소 = 파일이 지워져도 인덱스 보존) |
| HTML→텍스트 | stdlib `html.parser` (외부 의존성 없음) |
| 첨부 인덱싱 | 1차 미구현 (본문만). 스키마에 `attach_*` 컬럼 자리 확보됨 |
| Outlook PST/.msg | 보류. 스키마에 `source_type`, `message_id`, `source_meta` 자리 확보 |

**활성화**: `config.yaml › mail.enabled: true`(기본 켬), `mail.extensions: [.mysingle, .eml]`
→ `watch_folders` 내 해당 확장자 파일을 자동으로 메일 파이프라인으로 라우팅 (scandir 단일 순회)

**Retriever 병합**: `chunks` + `emails` score 내림차순 병합 후 top_k 적용, 샌드위치 배열.
