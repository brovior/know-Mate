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
    if ext == ".xls":
        return plain_reader  # xlrd 우선 시도, 실패 시(DRM·손상)만 COM 폴백
    if ext in {".doc", ".ppt"}:
        return com_reader    # xlrd 대응 없음 → 항상 COM
    return plain_reader      # docx/xlsx/pptx/pdf/txt → 라이브러리
```

**xls는 xlrd 우선 (`plain_reader._read_xls` + `secure/__init__.py` AutoReader)**

`.doc/.xls/.ppt` 중 `.xls`만 순수 파이썬 라이브러리(`xlrd`, Office 불필요)로 직접 파싱할 수 있다.
`AutoReader.extract`가 `.xls`를 먼저 `PlainReader`(xlrd)로 시도하고, 실패(DRM 래핑·손상 등 소수)
시에만 `except Exception`으로 잡아 COM으로 폴백한다. 이렇게 하면 **정상 xls 대부분이 COM 경로를
아예 타지 않아** 행오버·좀비 프로세스·`win32timezone`(COM이 날짜 셀을 변환할 때만 필요) 문제가
원천 차단된다. `scheduler._classify_extract_method`/`_is_drm_suspected`도 `.xls`를 OLE2 시그니처로
판정해(정상 OLE2 → plain, 아니면 → com) 우선순위 큐잉·DRM 유휴 스킵·COM 워치독 무장 여부를 실제
라우팅과 일치시킨다. `.doc/.ppt`는 대응하는 순수 파이썬 라이브러리가 없어 그대로 COM만 사용한다
(행오버는 COM 워치독이 보호).

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

**Excel 셀 읽기 — 범위 단위 일괄 읽기 (`chunking.xlsx_block_rows`, 기본 1000)**

COM 호출은 프로세스 간 마샬링이라 왕복 1회가 수십~수백 µs다. 셀마다 `cell.Value`를 읽으면
1000행 × 20열 시트에서 왕복이 2만 번 발생해 `cell_read` 단계가 지배적 비용이 된다
(`com_timeout_per_mb_sec: 20`이라는 큰 여유가 필요했던 이유). `ExcelComReader`는
`sheet.Range(sheet.Cells(r1,c1), sheet.Cells(r2,c2)).Value`로 **블록당 왕복 1회**로 읽는다.
한 번에 전부 올리면 순간 메모리가 커지므로 `xlsx_block_rows`(기본 1000)행 단위로 분할한다.

- **`.Value` 사용, `.Value2` 금지** — Value2는 날짜를 일련번호(45730.0)로 돌려줘
  `rag/date_filter.py`의 날짜 기반 검색 품질을 떨어뜨린다. 성능 이득은 왕복 제거에서
  거의 다 나오므로 `.Value`로 충분하다(출력은 기존 `str(cell.Value)`와 동일).
- **pywin32 `Range.Value`는 1×1 범위에서 2차원 튜플이 아니라 스칼라를 반환**한다(대표적 함정).
  `_normalize_range_values()`가 1×N·N×1까지 포함해 항상 (행, 열) 2차원으로 맞춘다.
- 설정값은 `secure/` 관례대로 **주입**한다(`get_extractor(mode, xlsx_block_rows=...)` →
  `AutoReader` → `ComReader` → `ExcelComReader`). `secure/`는 전역 `get_config()`를
  직접 조회하지 않는다. 잘못된 값(0·음수·비정수)은 1000으로 폴백한다(fail-safe).

> **후속 과제**: `com_reader.py`가 약 490줄로 300줄 규칙을 초과한다. Word/Excel/PPT 리더
> 3개를 별도 모듈로 분리하는 리팩터링을 별건으로 진행할 것.

**Office 점유 가드 (`secure/office_guard.py`)**

COM 자동화는 대상 Office 프로세스가 이미 떠 있으면 그 인스턴스에 붙는다(사용자당 1 인스턴스). 백그라운드
인덱싱이 사용자가 열어둔 창을 점유해 응답없음을 유발하는 것을 막기 위해, `AutoReader`는 COM 라우팅
(`.doc/.xls/.ppt` + OLE2 오라벨 docx) **직전** `is_office_busy_for_ext(ext)`로 대상 앱 실행 여부를
확인한다. 실행 중이면 `OfficeBusyError`를 던져 그 확장자만 **이번 사이클에서 연기**하고, 소비자
루프(`scheduler`)는 이를 실패가 아닌 연기로 처리(state 미갱신 → 다음 유휴 사이클 자동 재시도). 감지는
Toolhelp32 **프로세스 열거만** 수행하며 COM 객체를 생성·연결하지 않는다(사용자 창 무간섭). 비Windows·
열거 실패 시 차단하지 않아(기존 동작 유지) 사외 테스트에 영향 없다. 정상 OOXML(docx 등)은 라이브러리
파싱이라 가드 대상이 아니다.

**우리 자신 vs 사용자 구분 (자기 감지 스킵 방지)**: 인덱싱이 DRM/구형 문서를 읽으려고 COM으로 직접
띄운 Office도 같은 실행 파일(WINWORD.EXE 등)이라, 단순히 "프로세스가 있나?"로 판정하면 *우리가 띄운
인스턴스를 우리가 다시 점유로 오판*해 앞부분은 인덱싱되다가 뒷부분 문서가 전부 스킵되는 자기 감지
버그가 생긴다. 이를 막기 위해 `com_reader._dispatch_and_own`이 Dispatch **전후의 PID 차이**로 우리가
띄운 프로세스를 식별해 `office_guard.register_owned_pids`로 스레드별 "소유" 등록하고, 가드는 소유 PID를
제외한 **외부(사용자) 프로세스가 있을 때만** 점유로 판정한다. `quit_com_apps`는 사이클 종료 시 Quit되지
않고 남은 소유 프로세스를 `terminate_owned_office_processes`로 강제 종료해(PID 재활용 방지 위해 '지금도
Office 실행 파일인' PID만) 좀비가 다음 사이클 가드를 오작동시키지 않게 한다.

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

**purge 조회 경량화 + 조건부 스킵** (`collector/purge_meta.py`, 설계 A-0002/ADR-0002, 2026-07-24):
유휴 자동 인덱싱이 기본 60초마다 반복되는데, `_purge_removed_folders`가 매 사이클 chunks 테이블
**전체**(1024차원 벡터 + AES 암호화 원문 포함)를 `to_arrow().to_pandas()`로 로드해, 변경 파일이
0건이어도 매분 수십 MB를 할당/해제했다(베타에서 exe 메모리 70MB 도달 관측 — 인덱스가 커지면
사이클당 수백 MB로 확대). 두 축으로 해결한다.

- **컬럼 projection**: `table.search().select(["file_path"]).to_arrow()`로 `file_path` 컬럼만
  조회(벡터·원문 미로드). 구현 착수 시 실측으로 확인(`table.to_lance().to_table(columns=...)`은
  별도 `pylance` 설치가 필요해 채택 안 함 — `search().select()`가 기본 limit 없이 전건을 반환하고
  컬럼도 정확히 요청한 것만 실려 오는 것을 확인).
- **조건부 스킵**(`purge_meta.decide`): "op_sig(정규화된 watch_folders+dry_run+max_delete_ratio의
  SHA-256 canonical JSON) 불변 + 이번 사이클 처리 0건 + 마지막 성공 purge 후 24시간(config화,
  `purge_force_reconcile_sec`) 미만"이면 DB 조회 자체를 생략한다. 판정은 **차단 → 백오프 → 성공
  스킵 → 실행** 순으로 고정 — 실패 직후 성공 서명(`reconciled_sig`)을 해제하지 않으면 백오프가
  "이전 성공 메타로 인한 스킵"에 가려 무력화되는 결함이 있었다(설계 리뷰 3~4차에서 발견).
- **상태 전이**: 성공 시 모든 억제 해제 + `reconciled_sig`/`last_purge_ts` 기록. 일시적 예외 시
  `failed_sig`+`next_retry_ts`(기본 30분 백오프, `purge_backoff_sec`)를 걸고 `reconciled_sig`·
  `blocked_sig`를 함께 해제. 대량삭제 차단 시 `blocked_sig`만 남기고(동일 구성으론 자동 재시도
  안 함 — 사용자가 구성·차단율을 바꿔야 재실행) 나머지 상태를 해제한다(전이별 필드 완전성은
  설계 리뷰 8차 m-1로 확정).
- **메타 저장**: `index_state.json`과 분리된 sidecar `index_state.meta.json`(tmp→replace 원자
  교체) — 기존 state 스키마·소비자 무변경. 성공·실패·차단 상태 모두 **sidecar 저장 성공 여부와
  무관하게 워커 인스턴스의 메모리 캐시(`CollectorWorker._purge_meta_cache`)에 즉시 반영**해,
  저장 실패(권한·디스크·백신 잠금)가 매 사이클 재조회를 유발하지 않는다 — 재시작 후에는 sidecar가
  최신이 아니므로 보수적으로 재실행된다(purge는 멱등이라 안전).
- **경로 정규화**(`purge_meta.normalize_folders`/`belongs_to_any`): 서명 계산과 소속 판정이
  동일한 정규화 규칙(절대경로화→normpath→normcase→구분자 통일)을 쓰고, 소속 판정은 **경계
  인식 비교**(`p == root or p.startswith(root + "/")`)라 `C:/watch`가 `C:/watch-old/...`를
  하위로 오판하지 않는다.

**건수 조회(UI 표시) 경량화** (`app/bridge.py`, 2026-07-25): 위와 같은 안티패턴이 다른 위치에도
있었다 — `Bridge.getIndexStatus`/`_on_worker_finished`(화면의 "문서 N개/메일 N개" 표시용)가
chunks·emails 테이블 **전체**를 `to_arrow().to_pandas()`로 로드했고, 이건 유휴 인덱싱 사이클이
끝날 때마다(60초 주기, 변경 0건이어도 매번) 호출돼 상주 메모리가 방치 시간에 비례해 계속
쌓이는 원인이었다(베타 실측 ~1GB). 동일한 두 축으로 해결: ① `Bridge._compute_doc_mail_counts`가
`file_path`/`scope`/`is_deleted`(+ 필요 시 `indexed_at`), `mail_uid`/`is_deleted`만 projection
조회 ② `CollectorWorker.last_cycle_changed`(처리 건수 + orphan 정리 + 메일 인덱싱 중 하나라도
있었는지)가 False면 `_on_worker_finished`가 DB를 아예 열지 않고 직전 캐시값을 재사용. 조회
실패 시에도 0으로 튀지 않고 직전 값으로 폴백(부수 개선).

---

## 스캔 트리거 / 태스크 우선순위

| 구분 | 트리거 |
|---|---|
| 최초 인덱싱 | 온보딩 [인덱싱 시작] 버튼 |
| 증분 인덱싱 | 유휴 감지 (기본 60초, `collector.idle_seconds`) |
| 수동 재인덱싱 | 사이드바 [지금 재인덱싱] 버튼 |

태스크 우선순위: `NEW(1) > MODIFIED(2) > ORPHAN(3)`. 큐는 `queue.PriorityQueue`이며
`(action우선순위, com_rank)` 정렬 키로 처리 순서를 정한다. `com_rank`는 state.json에 캐시된
이전 사이클의 `method`(`"com"` | `"plain"`)로 정해져, COM 경유(구형 바이너리·DRM 래핑) 파일이
같은 action 단계 내에서 먼저 처리된다 — DRM 세션이 유효한 유휴 초입에 우선 처리되도록 하는
best-effort 보정(생산자·소비자가 동시에 도는 스트리밍 구조라 아직 큐에 없는 항목까지 재정렬할
수는 없다). 정렬 키에 단조증가 시퀀스를 끼워 넣어 종료 신호(`_SENTINEL`)와 실제 태스크가 직접
비교되는 일이 없게 한다. `method`는 `scheduler._classify_extract_method`(확장자 + zip 서명)로
매 성공 시 state에 기록된다.

**유휴 감지 (`collector/idle_util.py` + `scheduler.IdleScheduler`)**

`get_idle_seconds()`가 Windows `GetLastInputInfo`(시스템 전역 마지막 입력 이후 경과초)를
**읽기 전용**으로 조회한다 — 입력을 발생시키거나 시스템 유휴 타이머를 리셋하지 않는다. 트레이
상주 앱이라 창이 비활성/최소화 상태여도 감지해야 하므로 Qt 이벤트 필터(포커스 필요) 대신 이
시스템 API를 쓴다. `IdleScheduler`는 타이머 만료 시 이 값을 확인해 `idle_seconds` 임계 이상일
때만 트리거하고, 미달이면(사용자 작업 중) 남은 시간만큼만 재확인을 예약한다(최소 재확인 간격
`_MIN_RECHECK_SECONDS=5s`). 비Windows·조회 실패 시 0.0(항상 "방금 활동함")을 반환해 안전하게
트리거를 억제한다.

**DRM 유휴 임계 — 세션 만료 추정 시 DRM 의심 문서 스킵 (`collector.drm_idle_threshold_sec`)**

`CollectorWorker`는 생산자 단계에서 파일마다 **실시간 유휴 시간**(`_get_idle_seconds`, 기본
`idle_util.get_idle_seconds` = `GetLastInputInfo`)을 조회해, `collector.drm_idle_threshold_sec`
(기본 480초) 이상이면 DRM/SSO 세션이 만료됐을 가능성이 크다고 보고 `_is_drm_suspected(path)`가
True인 파일을 큐잉에서 건너뛴다(state 불변 → 세션 유효한 다음 사이클에 자동 재시도, 로그·요약에
"유휴로 DRM 문서 스킵 N건" 표시). 값싼 유휴 조회를 먼저 하고 임계를 넘었을 때만 파일 시그니처를
읽어(단락 평가) 네트워크 폴더 부하를 피한다.

**핵심(사이클 시작 1회가 아니라 실시간)**: 이전 구현은 사이클 시작 시점의 유휴 스냅샷 1회로
판정해, 3시간 도는 긴 사이클 도중 세션이 만료돼도 감지하지 못했고 수동 재인덱싱은 아예 관여하지
않았다. 실시간 조회로 바꿔 — 수동 재인덱싱(유휴 0으로 시작)도, 밤새 도는 긴 사이클도, 심지어
사용자가 도중에 복귀(유휴 리셋)한 경우도 그 시점의 실제 유휴로 올바르게 판정한다. 예: "퇴근 전
[지금 재인덱싱] 클릭 후 귀가" → 앞쪽 문서는 세션 유효 중 처리되고, 유휴가 임계를 넘긴 시점부터
DRM 문서는 스킵된다(COM Open 실패/로그인 모달 대기로 사이클이 멈추는 것을 예방). 정상 레거시
바이너리(OLE2 매직)·정상 OOXML(zip 매직)은 세션과 무관하게 열리므로 스킵 대상이 **아니다**.
임계가 0이면 판정 자체가 비활성.

**복귀 캐치업 — 활동 재개 직후 밀린 DRM 문서 처리 (Phase D)**

`IdleScheduler`는 `idle_seconds` 디바운스 타이머와 별개로, 경량 폴링 워처(`_recovery_timer`,
`_RECOVERY_POLL_SECONDS=15s`)를 항상 병행 실행한다. 유휴가 `drm_idle_threshold_sec`를 넘으면
`_was_long_idle=True`로 기억만 해두고(그 사이클들에서 DRM 문서가 스킵됐을 것으로 추정), 이후
유휴가 다시 짧아진 순간(`< _RECOVERY_ACTIVE_SECONDS=10s`, 즉 "방금 활동함")을 포착하면
즉시 1회 트리거한다 — 활동이 막 재개돼 실시간 유휴가 임계보다 작으므로, 워커가 DRM 스킵 없이
밀린 DRM 문서를 정상 처리한다(다음 정기 유휴 사이클, 최대 `idle_seconds` 뒤까지 기다리지 않음).
`is_busy()`인 동안은 판정을 보류하고 다음 폴링에서 재시도한다.

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

## 종료 확실화 (`app/lifecycle.py` + `MainWindow._shutdown`)

트레이 [종료] 시 워커가 COM Open 등에 블로킹돼 있으면(로그가 특정 파일에서 멈추는 행오버) 취소
플래그(`_cancelled`)를 못 봐 정상 종료가 안 되고, 프로세스가 잔존해 QWebEngine 자식 프로세스 등과
함께 자원을 계속 물어 PC가 버벅인다(트레이 아이콘은 `hide()`로 사라져 종료된 것처럼 보임).
`lifecycle.stop_worker`가 **정상 종료(wait 8s) → 실패 시 `terminate()`(스레드 강제 종료, wait 3s)
→ 그래도 잔존 시 `os._exit(0)`(프로세스 하드 종료)**로 단계적으로 강제해, [종료]가 반드시 프로세스를
끝내도록 보장한다. `_shutdown`은 스케줄러 정지·트레이 숨김·워커 정리를 각각 독립 try로 감싸(한
곳의 실패가 이후 정리를 건너뛰지 않게) 처리한다. 로직은 PyQt6 비의존으로 분리해(worker 덕타이핑,
hard_exit 주입) 사외 단위 테스트가 가능하다.

**암묵 종료 의존 제거 (설계 A-0001/ADR-0001, 2026-07-24)**: 위 `stop_worker` 에스컬레이션은
"워커가 실행 중일 때"만 진입하는 안전망이라, 인덱싱이 돌지 않는 **유휴 상태에서의 트레이 [종료]**
(실사용의 대부분)는 커버하지 못했다. 원인은 이벤트 루프 종료를 Qt 기본값
`quitOnLastWindowClosed=True`에만 의존한 것 — 이 규칙은 "마지막으로 **보이는** 창이 닫힐 때"만
발동하는데, 트레이 상주 상태(창이 `hide()`됨)에서 [종료]→`close()`를 해도 "보이는 창이 닫히는"
사건 자체가 없어 `app.exec()`가 영영 반환되지 않았다(트레이 아이콘만 사라지고 프로세스 잔존이
항상 재현되던 원인). `main()`에서 `app.setQuitOnLastWindowClosed(False)`로 암묵 종료를 끄고,
`_shutdown()` 마지막에 `lifecycle.finalize_shutdown(worker, quit_fn, hard_exit)`를 항상 호출한다:
`worker.isRunning()`이 False로 확인되면 `QApplication.quit()`, 실행 중이거나 조회 자체가
예외(판정 불가)면 보수적으로 `hard_exit` — 둘 중 정확히 하나만 실행된다. `_shutdown()`은
`_shutdown_done` 플래그로 프로세스 수명 기준 1회만 실행되도록 멱등화했다(근접한 이중 종료 요청
방어). `finalize_shutdown`도 `stop_worker`와 동형으로 PyQt6 비의존 분리 — `quit_fn`/`hard_exit`
주입으로 사외 단위 테스트 가능(`knowmate/tests/test_phase3.py::TestFinalizeShutdown`).

**강제 종료 표식 (dirty-shutdown marker, 설계 리뷰 10차 M-1 → 11차 B-1로 방식 수정)**: 자동
손상 감지·복구는 여전히 보류하지만(아래), "강제 종료가 있었다"는 사실 자체는 저비용으로 남긴다.
처음엔 hard-exit 직전에 표식을 기록하는 방식이었으나, 그 동기 파일 쓰기 자체가 블록되면(백신·
네트워크 드라이브 등) 최후 안전망인 하드 종료가 멈추는 모순이 있어(9차 B-1로 확립한 "하드 종료는
무조건·즉시" 불변식과 충돌 — 11차 B-1) 방식을 바꿨다: `main()`이 **시작 시** 미리
`lifecycle.check_and_remark_dirty_shutdown()`으로 `%APPDATA%/AegisDesk/dirty_shutdown.flag`를
확인·재기록하고, `finalize_shutdown`의 **정상 quit 경로에서만** `clear_dirty_shutdown()`으로
지운다. `stop_worker`/`finalize_shutdown`의 hard-exit 분기는 이제 파일 I/O를 전혀 거치지 않는다.
표식이 남아있던 채로 시작되면(직전 실행이 못 지웠다는 뜻) WARNING 로그 + 트레이 풍선 알림(로그만
으로는 GUI 사용자가 놓치기 쉬움, 11차 M-1)으로 재인덱싱을 권장한다.

**남은 한계(후속 과제, 설계 리뷰 8차 M-1 — 보류)**: `QThread.terminate()`/`os._exit()`가 LanceDB
쓰기(add/delete/optimize) 도중 발생했을 때의 커밋 원자성은 실제 손상 시나리오를 재현·검증할 수
없는 상태에서 추측성 자동 복구(격리·재구축) 로직을 넣는 게 오히려 위험하다고 판단해 지금은 넣지
않았다. 인덱스는 원본 문서에서 **언제든 재생성 가능한 파생 데이터**이므로, 최악의 경우 "인덱스
폴더 삭제 후 재인덱싱"이 항상 유효한 완전 복구 경로다 — 실제 장애 사례가 쌓이면 자동 감지·격리를
근거 있게 설계한다. 위 dirty-shutdown 표식은 "언제 그 복구가 필요할지"에 대한 최소한의 신호일
뿐, 자동 격리·재구축은 여전히 하지 않는다.

## COM 행오버 워치독 (`collector/com_watchdog.py` + `secure/office_guard.py`)

동기 COM 호출(Excel/Word 열기·셀 순회)이 멈추면 그 호출을 한 스레드가 COM 안에 갇힌다 — 같은
스레드에서 타임아웃을 걸 수 없고(이미 갇힘), **유일한 해제 방법은 그 호출을 서비스하는 Office
프로세스를 종료하는 것**이다(프로세스가 죽으면 호출이 RPC 오류로 반환되며 스레드가 풀린다). 우리
워커는 MTA라 STA 전용인 COM 메시지 필터 타임아웃도 못 쓴다 — kill이 정말 유일하다.

**동작**: 소비자 루프가 COM 경유 파일(`_classify_extract_method=="com"`)의 `extract()` 앞에서
`ComWatchdog.arm(exe, timeout)`으로 무장하고, 정상 완료 시 `disarm()`한다. 타임아웃은 **파일 크기
비례**(`_com_timeout_for_size` = `com_timeout_base_sec` + `com_timeout_per_mb_sec`×MB, 상한
`com_timeout_max_sec`)로, 작은 파일 행오버는 빨리(≈60s) 잡고 셀 순회가 느린 대형 xls는 넉넉히
보호한다. 발화 시 `office_guard.terminate_stuck_office(exe)`가 **우리 소유** Office 프로세스를(없으면
`begin_com_op` baseline 이후 새로 뜬 것 = Dispatch-hang) 종료 → 갇힌 COM 호출이 오류 반환 →
그 파일만 실패 처리, **사이클은 계속**(로그·요약에 "COM 시간초과 강제해제 N건").

**경합 방지**: ① **세대 토큰**으로 이미 끝난 파일의 타이머가 다음 파일의 Office를 죽이는 오사살
차단, ② 락+active 플래그로 발화/해제 경합 처리, ③ **daemon 타이머**로 위 종료 확실화(A)를 되돌리지
않게, ④ 종료는 "우리 소유 & 지금도 그 exe인" PID만(PID 재활용·사용자 인스턴스 보호). 소유 PID는
워치독 스레드가 읽어야 하므로 스레드-로컬 → **모듈 레벨 + 락**으로 관리한다. `com_reader`·win32
직접 접촉 없이 `office_guard` API만 호출해 원칙3(보안·Office 코드 격리)을 지키고, `terminate_fn`·
`timer_factory` 주입으로 사외 단위 테스트가 가능하다.

**세이프모드 루프 차단 (`secure/office_resiliency.py`)**: 위 강제 종료에는 자기 강화되는 부작용이
있었다 — Office는 비정상 종료를 겪으면 `HKCU\...\Office\<ver>\<App>\Resiliency` 아래에 표식을
남기고, **다음 기동 때 "안전 모드로 시작할까요?" 프롬프트**를 띄운다. 이 프롬프트는 `Dispatch()`가
반환하기도 전에 뜨므로 앱 수준 `DisplayAlerts=False`로는 억제할 수 없다 → 또 행오버 → 또 kill →
표식 재생성의 무한 루프가 된다(사내 실사용 로그로 확인). 그래서 ① `_dispatch_and_own`이 Dispatch
**직전**에, ② `terminate_stuck_office`가 kill **직후**에 표식을 지운다. 지우는 대상은
`DisabledItems`·`StartupItems`뿐이고 **`DocumentRecovery`는 절대 건드리지 않는다** — 그건 *사용자
본인이* 저장하지 못하고 잃은 문서의 복구 목록이라 지우면 실제 업무 데이터가 복구 불가능해지고,
비모달 작업창이라 자동화를 막지도 않아 지울 이유도 없다.

같은 맥락에서 문서 열기 호출 자체도 **모달 프롬프트가 뜰 수 있는 모든 경로를 인자로 사전
차단**한다(프롬프트 하나 = 행오버 하나): Excel은 `CorruptLoad=xlRepairFile`(손상 복구 확인창)·
`IgnoreReadOnlyRecommended`·`Notify=False`(잠긴 파일 대기)·`UpdateLinks=0`(외부 링크 갱신), Word는
`OpenAndRepair`·`NoEncodingDialog`(구형 .doc 인코딩 선택창). 암호 보호 문서에는 **더미 암호**를
넘겨 암호 입력창 대신 즉시 실패시킨다(보호되지 않은 문서에서는 무시되는 인자). late binding에서
이름 인자는 신뢰할 수 없어 전부 위치 인자로 넘기며, 관심 없는 중간 인자는 `pythoncom.Missing`으로
건너뛴다.

*잔여 한계(후속 과제)*: (1) 프로세스 내부 COM 마샬링 데드락은 Office kill로도 안 풀림 → 종료 확실화
(A)가 최후 안전망. (2) 같은 파일이 매 사이클 행오버하면 사이클마다 타임아웃 낭비 → **아래
"실패 이력 기록"(3차)·"실패 백오프"(4차)에서 해결** — 유형별 재시도 시점 조정까지 구현됨.
(3) Dispatch-hang 종료 시 그 사이 사용자가 같은 앱을 새로 열면
죽일 수 있는 수 초 경합(가드가 사전 차단해 창이 좁고, 결과도 "방금 연 창 닫힘" vs "영구 행오버"의
교환).

**단계 구분·확실한 닫기·처리 중 변경 감지 (`secure/com_stage.py`, 2026-07-25)**: DRM 문서 hang이
`Workbooks.Open()`에서 멈춘 건지 셀 순회에서 멈춘 건지 구분할 방법이 없어, 원인에 따라 완전히
다른 대응(사전 판별 vs 벌크 읽기 교체)을 결정할 수 없었다. 세 가지로 해결한다.

- **단계 계측**: `com_stage.StageTimer`가 각 리더의 `parse()` 안에서
  `dispatch → open → sheets(Excel만) → cell_read(Excel)/read(Word·PPT) → close`를
  컨텍스트 매니저(`timer.stage(name)`)로 감싸 단계별 소요시간을 기록하고, 현재 단계를
  모듈 레벨 슬롯(`threading.Lock` 보호)에 게시한다. `ComWatchdog`가 다른 스레드(daemon
  타이머)에서 이 슬롯을 읽어(`stage_fn`, 기본 `com_stage.current_stage_name`/`describe`)
  타임아웃 경고 로그에 `[단계: open(74.0s) <path>]`를 남기고, 사이클 요약에는 단계
  **이름**만 모아 `Counter`로 집계한다(`COM 시간초과 3건 [단계별: {'open': 3}]`). 로그에는
  경로·단계명·소요시간만 남고 셀 값·본문은 절대 남기지 않는다(원칙7).
- **확실한 닫기**: 이전에는 `wb.Close(False)`가 **정상 경로에서만** 실행돼, 셀 순회 중
  예외가 나면 워크북이 열린 채 남았다. 이제 `wb`(`doc`/`prs`)를 try 밖에서 `None`으로
  초기화하고, `finally`의 `_close_quietly()`가 CLOSE 단계로 계측하며 항상 닫기를 시도한다
  (닫기 자체의 실패는 로그만 남기고 삼켜 원본 예외를 덮어쓰지 않음).
- **처리 중 변경 감지**: `IndexTask`에 스캔 시점 `mtime`을 실어, 추출 완료 후 재조회한
  `stat()` 결과와 비교한다. 달라졌으면(파일이 처리 도중 수정됨) 그 사이클엔 인덱싱하지
  않고 **state도 갱신하지 않은 채** 건너뛴다 — 그러지 않으면 "새 mtime + 옛 본문"이
  저장돼 다음 사이클의 `classify_changes`가 "변경 없음"으로 오판, 영구 미인덱싱된다.
  state 미갱신이라 다음 사이클에 자동 재검출·재시도된다(별도 재시도 큐 불필요).
  `task.mtime > 0`일 때만 판정하고(테스트 더블 등 스캔 정보 없는 태스크는 판정 제외)
  epsilon을 둬(1e-6초) 파일시스템 mtime 해상도 오차로 인한 오탐(=영구 미인덱싱)을 피한다.

## 실패 이력 기록 (`collector/failure_state.py` · `index_failure.json`, 2026-07-28)

지금까지 수집기는 실패한 파일을 그 사이클 안(`failed`/`unreadable`/`deferred` 리스트)에서만
알았고 다음 실행까지 남는 상태가 없었다 — "이 파일은 원래 못 읽는 파일인가, 오전처럼 잠깐
문제가 생겨 안 읽힌 건가"를 구분할 방법이 없었다는 뜻이다(세이프모드 루프 사건이 후자의
실례였다). **3차는 그 구분에 필요한 근거를 기록만 했다** — 기록된 분류·연속 실패
횟수로 재시도를 늦추거나 파일을 건너뛰는 정책은 바로 아래 "실패 백오프"(4차)에서
구현한다.

**분류 8종** (`failure_state.classify`): `TEMPORARY_BUSY`(Office 점유·COM
RPC_E_CALL_REJECTED류) · `OPEN_TIMEOUT`(워치독 발화, 단계가 dispatch/open) ·
`READ_TIMEOUT`(워치독 발화, 단계가 sheets/cell_read/read) · `OPEN_ERROR`(6a — 워치독
**미**발화 COM 오류, `failed_stage`가 dispatch/open) · `READ_ERROR`(6a — 워치독 미발화,
`failed_stage`가 sheets/cell_read/read) · `NEEDS_USER_ACTION`(`UnreadableFormatError`
— DRM 래핑·손상 추정) · `FILE_CHANGED`(처리 중 mtime·size 변경 감지) ·
`UNKNOWN_TRANSIENT`(나머지 전부, 기본값). COM 오류 코드만으로 실제 원인을 100% 확정할
수 없다(암호·손상 문서가 더미 암호로 즉시 실패하는 현재 구현상 일반 COM 오류와 코드로
구분되지 않는다) — 그래서 확실한 것만 분류하고 나머지는 `UNKNOWN_TRANSIENT`로
떨어뜨리되, `last_error_code`(HRESULT)는 그대로 보존해 실측 로그가 쌓이면 분류를 넓힐
근거로 쓴다.

**6a 이전에는 `failed_stage`를 받고도 쓰지 않았다** — `com_stage.take_last_failed_stage()`
결과가 `classify()`에 인자로 전달되고는 있었지만 함수 본문 어디에서도 참조되지 않아,
워치독이 발화하지 않은 일반 COM 오류(가장 흔한 케이스)는 전부 `UNKNOWN_TRANSIENT`로
떨어졌다(6단계 설계 리뷰에서 AST로 확인). 6a가 이 배관의 마지막 한 줄을 이어
`OPEN_ERROR`/`READ_ERROR`로 분리한다. 암호 보호 OOXML·DRM 래핑을 컨테이너 내부 증거로
세분하는 것은 **6b로 이연**됐다 — CFB 디렉터리는 FAT 체인이라 8바이트 매직만으로는
불충분하고(설계 리뷰 B-2), 판별 I/O가 COM 워치독 해제 이후 실행돼 SMB·DRM 드라이브에서
수집기 QThread에 새 행오버 경로를 만들 수 있다(리뷰 M-3). 근거: `docs/ai-workflow/
architecture.md` A-0003.

**연계**: 워치독의 `ComWatchdog.disarm()`이 이번 파일에서 실제로 발화했는지(발화했다면
어느 단계인지)를 반환하도록 확장했고, `com_stage.take_last_failed_stage()`가 COM 파싱
중 예외가 난 단계를 (읽고 비우는 방식으로) 스케줄러에 공개한다 — 둘 다 `classify()`의
입력이다.

**저장** — `index_state.json`(성공 상태)과 완전히 분리된 sidecar
`%APPDATA%/AegisDesk/index_failure.json`(purge sidecar `index_state.meta.json`과 동일
관례). 문서 내용·셀 값은 물론 **예외 메시지도 저장하지 않는다**(시트명 등 내용 조각이
섞여 들어올 수 있어서) — 저장하는 건 경로·분류·단계·HRESULT 문자열·연속 실패 횟수·
마지막 실패 시각(epoch)뿐이다.

**초기화(기록 삭제) 조건**: ① mtime·size 변경(다른 내용이 된 파일) ② 추출 성공
③ `READ_STRATEGY_VERSION` 변경(추출 로직이 바뀌면 과거 실패가 무효 — 예: 셀 단위→범위
단위 일괄 읽기 전환이 이 bump 대상이었다) ④ 사용자의 수동 재인덱싱(`CollectorWorker.
request_failure_retry()` — `bridge.startReindex`·트레이 [재인덱싱]만 호출, **유휴 자동
사이클은 호출하지 않는다** — 매번 비우면 연속 실패 횟수가 영원히 1에 머물러 기록 자체가
무의미해진다) ⑤ 파일이 더 이상 존재하지 않음(사이클 종료 시 `prune()` — "이번 스캔에서
못 봤음"이 아니라 `Path.exists()`가 False일 때만, 네트워크 드라이브 일시 단절로 멀쩡한
기록이 날아가지 않게).

## 실패 백오프 (`collector/failure_state.py` § `BackoffPolicy`, 2026-07-28)

3차가 남긴 `index_failure.json`을 읽어서, 문제 파일을 매 사이클(유휴 60초)마다 다시
열지 않도록 재시도 시점을 미룬다. **완료 기준은 "영구적인 무시가 아니라 재시도 시점
조정"** — 모든 분류에 상한이 있고, 파일이 바뀌거나 수동 재인덱싱하면 즉시 다시
시도된다.

**정책 표** (`config.yaml › collector.failure_backoff`):

| 분류 | 대기 | 비고 |
|---|---|---|
| `TEMPORARY_BUSY` | 5~10분(고정, 지터 포함) | 연속 횟수와 무관 — 하루 종일 열어둬도 계속 5~10분마다 재확인(escalation 금지) |
| `OPEN_TIMEOUT`·`READ_TIMEOUT`·`OPEN_ERROR`·`READ_ERROR` | 30분 → 6시간 → **7일**(3회째부터 승격) | 연속 실패 횟수. 6a부터 사다리는 30분·6시간 2단뿐이고, 3회째는 `escalation_action_at` 임계에 걸려 아래 `NEEDS_USER_ACTION` 상한을 재사용한다(기존 24시간 칸은 제거) |
| `UNKNOWN_TRANSIENT` | 위와 동일 | 별도 config 키(`unknown_ladder_sec`) — 실측 후 독립 조정 가능 |
| `NEEDS_USER_ACTION` | 시간 기반 재시도 없음 + **7일 안전밸브** | 파일 변경·수동 요청 시 즉시. 7일 상한은 스펙 밖 추가 — 세이프모드 루프 사건처럼 "우리가 만든 일시적 상태 때문에 멀쩡한 파일이 영구히 안 읽히는" 사고를 코드로 막는다 |
| `FILE_CHANGED` | 0초 | mtime이 이미 바뀌어 기록이 스스로 무효화됨 |

**6a: "반복 중"/"조치 필요" 표시는 대기 시간과 다른 축이다** (`escalation_state()`,
`config.yaml › collector.failure_backoff.escalation_repeat_at/escalation_action_at`,
기본 2/3). 연속 실패 횟수만 보는 단일 함수이며 `kind`와 무관하다 — [확인 필요한
문서] 화면(5차)과 백오프 계산(`backoff_seconds()`)이 **반드시 이 함수 하나만** 호출해,
같은 기록을 다르게 판정하는 일이 없게 한다.

다만 **대기 시간은 원인별 고정 정책이 승격 사다리보다 우선한다** — `TEMPORARY_BUSY`·
`FILE_CHANGED`·`NEEDS_USER_ACTION`은 연속 실패가 몇 회든 위 표의 고정값을 그대로 쓴다.
그렇지 않으면 파일을 3번 연속 열어둔 사용자(`TEMPORARY_BUSY`)의 재시도가 "3회 = 7일
대기" 사다리를 타 버려, 파일을 닫아도 최대 7일을 기다리는 회귀가 생긴다(6단계 설계
리뷰 3차 M-2 — "반복 승격은 원인과 무관"이라는 원칙과 "TEMPORARY_BUSY는 항상
5~10분"이라는 원칙이 충돌했던 것을 이렇게 정리했다). 즉 **표시는 모든 원인에 공통,
대기는 원인별로 다르다.**

`TEMPORARY_BUSY`의 지터는 `hashlib.sha256(경로)` 기반 결정적 값이다(파이썬 내장
`hash()`는 프로세스마다 솔트가 달라 재시작 시 값이 바뀌므로 쓸 수 없다) — 여러 파일의
재시도 시각이 한 사이클에 몰리는 것을 흩어준다.

**연속 횟수는 "같은 분류가 연속된 횟수"** (3차 `note_failure` 수정) — 분류가 바뀌면
1로 리셋한다. 안 그러면 사용자가 파일을 하루 종일 열어둬 `TEMPORARY_BUSY`가 여러 번
쌓인 뒤 우연히 시간초과가 1번 나면 `consecutive`를 그대로 이어받아 곧장 24시간 백오프로
튀는 오류가 생긴다.

**백오프 검사는 반드시 두 지점** — `should_defer()` 하나를 두 곳에서 호출한다
(판정 로직이 갈라지면 그 자체가 버그의 원천):

1. **생산자**(`_producer`, 큐에 넣기 전): action 확정 직후, DRM 유휴 스킵 검사보다
   앞. 여기서 걸러야 큐·Office 기동·워치독 무장 비용이 전부 발생하지 않는다.
2. **소비자**(처리 직전): `task_queue.get()` 직후, `done += 1` 앞. **권위 있는 최종
   판단** — 생산자는 스레드 경합을 피하려고 사이클 시작 시점의 스냅샷(`dict(failures)`)
   으로 판단하는데, 소비자는 실시간 `failures` 딕셔너리를 본다. 특히 `watch_folders`가
   겹치면(예: `C:/docs`와 `C:/docs/sub`를 둘 다 등록) 같은 파일이 큐에 두 번 들어갈
   수 있는데(`_producer`의 `seen` 집합은 `.add()`만 하고 중복 검사에 쓰이지 않는다),
   생산자 스냅샷만으로는 그 두 번째를 못 잡고 소비자 검사만 잡을 수 있다.

`should_defer()`의 판정 순서는 항상 "재시도하는 쪽"이 먼저다(fail-open): 기록 없음 →
처리 / 정책 `enabled: false` → 처리 / **mtime·size 불일치(파일 변경) → 처리** / 그 외엔
`마지막 실패 시각 + backoff_seconds() > now`면 건너뜀.

**`BackoffPolicy.from_config`은 fail-open** — `purge_meta.is_valid_ratio`(삭제 안전장치,
fail-closed)와 반대 방향이다. 백오프는 삭제를 막는 안전장치가 아니라 "언제 재시도할지"만
정하므로, 잘못된 설정값의 결과는 "예상보다 자주 재시도"(무해)여야지 "영구히 재시도 안
함"이면 안 된다. `enabled: false`는 이 기능 도입 전(매 사이클 재시도) 동작으로 되돌리는
비상 스위치다.

**최대 리스크는 조용한 미인덱싱**(파일이 백오프에 걸려 아무도 모르게 영영 안 들어가는
것)이라 여러 겹으로 방어한다: 모든 분류에 상한(7일 포함) · mtime/size 변경 시 무조건
즉시 재시도 · 사이클 요약에 `재시도 대기 N건` 상시 노출(`producer_state["backoff_deferred"]`
+ 소비자 쪽 카운터 합산) · `enabled: false` 비상 스위치 · 수동 재인덱싱이 기록을 전부
초기화.

**주기 재기동 (선제적 완화책, `collector.com_restart_every_n_files`)**: 워치독은 행오버가 **발생한
뒤** 반응적으로 kill하는 안전망이지만, 행오버가 없어도 한 Office 인스턴스가 COM 파일을 수백 건
연속 처리하면 핸들·메모리가 누적돼 후반부가 느려지거나 불안정해질 수 있다. 이를 선제적으로
예방하기 위해 소비자 루프가 **실제로 COM을 사용한 파일**(`_classify_extract_method=="com"`이고
`OfficeBusyError`로 연기되지 않은 경우)만 카운트해, `com_restart_every_n_files`건마다
`com_reader.quit_com_apps()`를 호출해 word/excel/ppt 인스턴스를 일괄 종료한다(다음 COM 파일에서
자동 재Dispatch). 앱별 개별 카운트 대신 **일괄 재기동**을 택한 건 외과적 정밀도 대비 복잡도 이득이
작기 때문이다. 재기동 실패는 삼키고 사이클을 계속한다(로그·요약에 "COM 재기동 N회"). 이 완화책은
DRM COM Helper 프로세스 분리(아래) 착수 **전** 실측 단계이며, 최종적으로는 helper 분리가 격리를
대체한다.

## DRM 추출 텍스트의 벡터DB 저장 — 컴플라이언스 확정 필요 (미해결 · 코드 아님)

**현황**: beta2에서 DRM 보호 문서 COM 인덱싱을 도입한 시점부터, Office로 복호화해 추출한 평문이
청크로 분할되어 LanceDB `text` 컬럼(AES-256-GCM + DPAPI 키, 원칙5)에 저장되고 있다. 이는 아래 helper
프로세스 분리 여부와 **무관하게 이미 발생 중**인 상태다(pipe·helper는 *임시 평문 파일*만 방지할 뿐,
벡터DB의 청크 원문 저장 자체는 그대로다).

**쟁점**: 우리 저장 방식(앱 레벨 암호화 — "해당 PC·해당 사용자면 복호화 가능")과 나스카/파수류 DRM의
보호 모델("SSO 세션 유지 중 + 허가된 렌더링 앱에서만 복호화")은 보호 수준이 다르다. 우리는 DRM 봉투
**밖으로** 내용을 꺼내 더 약한 보호로 재보관하는 구조 → 원본 DRM 파일은 PC 밖으로 나가면 못 열리지만
우리 인덱스는 (그 사용자 계정 하에선) 열린다.

**필요 조치**: 코드로 풀 문제가 아니라 보안/DRM 관리부서에 **"DRM 문서의 추출 텍스트를 로컬 앱
암호화로 보관해도 되는가"**를 명시 확인받는 컴플라이언스 사안.
- **허용** → 현행 유지. 확인 사실(승인 일자·담당 부서)을 이 섹션에 추가 기록.
- **불허** → DRM 의심 문서(`scheduler._is_drm_suspected`, 서명 `<## ` 감지)를 인덱싱에서 제외하는
  config 스위치가 최소 대응. 판별 로직이 이미 있어 스위치 추가는 소규모 작업.

> **상태**: ✅ **허용 승인 완료 (2026-07-23)** — DRM 추출 텍스트를 현행 AES-256-GCM + DPAPI 저장
> 방식으로 계속 보관하는 것으로 결론. 현행 유지(별도 코드 변경 없음), DRM 의심 문서 인덱싱 제외
> 스위치(불허 시 대응)는 **불필요**로 확정.

## DRM COM Helper 프로세스 분리 (로드맵 · 비개발자 정식 배포 전 완료 조건)

**목적**: COM 추출을 메인 프로세스에서 떼어내 별도 helper 프로세스에서 수행 → 행오버·크래시가
메인(UI·LanceDB)을 오염시키지 않게 **격리**. 현행 워치독·주기 재기동은 이 격리 전의 실측 완화책이며,
helper 분리가 최종 구조다.

**채택 구조 (확정)**
- **재실행 방식**: `sys.executable --com-worker` + `subprocess.Popen`. `multiprocessing` **금지**(frozen
  `freeze_support` 함정 + LanceDB 파일 락 원칙8 리스크). onedir 빌드에서 `sys.executable`은
  `AegisDesk.exe` 자신이라 파이썬 별도 배포 없이 자기 재실행 가능.
- **argv 분기 위치**: `main.py` 최상단에서 PyQt6·단일 인스턴스 가드·LanceDB import **전에** 분기.
  특히 단일 인스턴스 가드보다 앞서야 helper가 "이미 실행 중" 오판으로 기존 창을 복원시키고 죽는 사고를
  막는다. QWebEngine import를 안 타 helper 기동도 가볍다.
- **IPC**: 임시 평문 파일 금지(원칙5) → stdin/stdout **length-prefix 바이너리 프로토콜**
  (`sys.stdout.buffer` 직접 쓰기).
  - **stdout은 프로토콜 전용 예약**: helper의 로그·경고·`print`가 stdout에 한 글자라도 섞이면
    length-prefix 프레이밍이 깨진다 → helper 로그는 전부 stderr/파일로.
  - **부모는 stdout·stderr를 지속 소비**: 파이프 커널 버퍼(Windows 기본 수십 KB)가 차면 자식 write가
    블록되는 교착이 실재. stdout은 프레임 읽기로 자연 소비되나 **stderr는 별도 스레드**(또는
    DEVNULL/로그파일 리다이렉트)로 빼야 교착을 막는다.
- **kill 순서**: helper가 `Dispatch` **직후** Office PID·생성시각을 부모에 **선보고** → timeout 시
  **소유 확인된 Office 먼저 종료 → 2~3초 유예 → helper 종료**(블록된 동기 COM 호출이 RPC 오류로 풀려
  helper가 스스로 정리하고 나올 시간). 생성시각 동반은 PID 재사용 오살 방지.
  - **Dispatch 자체 행오버 폴백**: 선보고 전이라 죽일 Office PID를 모르는 경우 → helper 종료 후
    "helper 시작 시각 이후 생성된 소유 미확인 Office"를 `terminate_stuck_office` 계열로 정리(관찰된
    행오버는 주로 `Documents.Open`이라 선보고가 대개 커버하나, DRM 로그인 대기는 기동 단계에서도 발생).
  - HWND는 참고용만(Excel `Application.Hwnd`는 있으나 Word `Application`엔 동급 속성 없음 → 종료 근거는
    PID+생성시각으로 충분).
- **경계 유지**: LanceDB는 **메인 프로세스에서만**(원칙8 준수). helper는 COM 추출만 담당하고 기존
  `TextExtractor` 프로토콜(`extract(path)->str`)로 결과 반환 → 스케줄러 무변경.

**선행**: 워치독·주기 재기동 효과 **실측** → helper 분리 착수. 정식(비개발자) 배포 전 완료 조건.

## 단일 인스턴스 보장 (`app/single_instance.py`)

트레이 상주 앱이라(닫아도 종료되지 않음) 사용자가 바로가기를 여러 번 눌러 실수로 여러 인스턴스를
띄우기 쉽다. 두 인스턴스가 같은 `%APPDATA%/AegisDesk`의 LanceDB·`index_state.json`·`threads.json`에
동시에 쓰면 락 충돌·데이터 유실 위험이 있다(원칙8 "수집기는 QThread 워커에서만 실행, multiprocessing
금지"와 동일한 이유 — 동시 쓰기 자체가 문제).

`QLocalServer`/`QLocalSocket`(명명된 로컬 소켓) 기반:
- `main()`이 `QApplication` 생성 직후 `try_acquire_or_notify_existing()`을 호출해 기존 서버
  (`AegisDeskSingleInstance`)에 연결을 시도한다.
- **연결되면** 이미 다른 인스턴스가 떠 있는 것 → `"show"` 메시지를 보내고 새 프로세스는 창을 만들지
  않고 즉시 종료한다.
- **연결되지 않으면** 내가 첫 인스턴스 → `SingleInstanceServer`가 리슨을 시작하고, 이후 다른
  프로세스가 접속해 `"show"`를 보내면 `show_requested` 시그널을 emit → `MainWindow._show_from_tray`
  (트레이 복원과 동일 로직)로 연결돼 기존 창이 앞으로 나온다.
- 이전 비정상 종료로 서버 이름이 남아있으면 `listen()` 전에 `QLocalServer.removeServer()`로 정리
  후 재시도한다.
- QLocalServer/Socket은 `QCoreApplication` 인스턴스가 있어야 동작하므로 반드시 `QApplication` 생성
  이후에 호출해야 한다.

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
