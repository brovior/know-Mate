# EMAIL_DESIGN.md — Aegis Desk 메일 인덱싱 설계
> 상태: **Phase 5a 구현 완료** (Knox `.mysingle` + 표준 `.eml`) | 2026-09-10
> Outlook PST/.msg 보류

---

## 0. 범위와 우선순위

| 소스 | 상태 | 비고 |
|---|---|---|
| **Knox `.mysingle`** | ✅ 구현 완료 | "메일 1통 = 파일 1개" → 기존 파일 스캐너 모델에 얹힘 |
| **표준 `.eml`** | ✅ 구현 완료 | 동일 RFC822+MIME 포맷 → 같은 파서 재사용(`parse_mail_file`) |
| **Outlook PST/.msg** | 🔲 보류 | COM 보안 정책 선결 검증 필요. §8 참조 |

**설계 원칙**: mysingle로 메일 파이프라인 뼈대를 세우고, `.eml`(동일 포맷)을 확장자 분기만으로 얹었다.
Outlook은 그 위에 얹는다. 스키마는 **Outlook까지 고려한 풀 스키마**로 한 번에 정의 (LanceDB는 ALTER가 비싸므로).

---

## 1. 메일 파싱 (`secure/mysingle_reader.py` → `parse_mail_file`)

- **대상**: `.mysingle`(Knox) / `.eml`(표준). 둘 다 RFC822+MIME라 하나의 파서(`parse_mail_file`)로 처리, `parse_mysingle`은 하위호환 래퍼.
- **source_type 판별**: 확장자로 결정 — `.mysingle` → `knox`, 그 외 → `eml`. `mail_uid` 접두도 동일(`knox:` / `eml:`).
- **포맷**: 표준 RFC822 + MIME multipart. DRM 없음.
- **파싱**: `email.message_from_binary_file(f, policy=email.policy.compat32)`
  - `policy.default`는 `=?UTF-8?B?...?=` 헤더 처리 이슈 → 헤더는 `email.header.decode_header()`로 별도 디코딩.
- **실측 구조**:
  ```
  multipart/mixed
    ├── multipart/related
    │     ├── text/html          ← 본문 (Disposition: inline)
    │     └── image/*            ← 인라인 이미지 (cid 참조)
    └── application/octet-stream ← 첨부 (Disposition: attachment)
          filename: =?UTF-8?B?...?=
  ```
- `msg.walk()`로 중첩 구조 무관하게 평탄 순회 가능.

---

## 2. 본문 처리

- `text/html`만 있는 경우가 일반적. `text/plain` fallback 지원.
- **HTML→텍스트**: stdlib `html.parser` (외부 의존성 0, 사외 fake 테스트 즉시 통과).
  - `_HTMLStripper(HTMLParser)`로 태그 제거 후 공백 정리.
- 추출 텍스트는 기존 청킹 파이프라인(`chunk_size=400`, `overlap=80`)에 투입.

---

## 3. 첨부 처리 정책 (1차 미구현)

1차 구현은 본문만 인덱싱. 첨부는 스키마 자리만 확보 (`attach_filename`, `attach_sha256`, `chunk_origin`).

향후 구현 방향 (인메모리 파싱, 디스크에 평문 저장 금지):

| 첨부 종류 | 처리 |
|---|---|
| docx/xlsx/pptx/pdf | `BytesIO`로 인메모리 파싱 후 인덱싱 |
| doc/xls/ppt (구형) | 메타데이터(파일명)만 저장 — COM = 임시파일 필요 = 보안 원칙 충돌 |
| 이미지/기타 | 무시 |

---

## 4. 중복 판별

| 순서 | 키 | 역할 |
|---|---|---|
| 1 | `mail_uid` = `knox:{UniqueID}` / `eml:{Message-ID}` | 같은 메일 재인덱싱 방지 |
| 2 | `mtime` 비교 | 내용 변경 감지 |
| 3 | **`_index_version`** (`source_meta`에 저장, `EMAIL_INDEX_VERSION`) | 인덱싱 포맷 변경 시 자동 재인덱싱 |
| 4 | `message_id` (RFC, 소스 간 공통) | (향후) 소스 간 dedup |
| 5 | `attach_sha256` | (향후) 동일 첨부 중복 방지 |

`get_index_state(mail_uid, mtime)`는 위 1·2·3을 모두 만족하면 `CURRENT`, DB에 행이 없으면
`MISSING`, 기존 행의 mtime·포맷이 다르면 `STALE`, 조회 자체가 실패하면 `ERROR`를 반환한다.
`ERROR`에서는 저장·삭제를 하지 않고 다음 수집 기회에 재시도한다. `STALE`은 기존 active `chunk_id`를
캡처한 뒤 모든 새 청크 임베딩과 `add()`가 성공해야 그 ID만 삭제한다. 삭제 실패 ID는
`mail_scan_state.json`의 최상위 `pending_deletes` 대기열에 먼저 기록하고 다음 사이클에
원본 파일·성공 캐시·인덱스 버전과 무관하게 재시도한다. 스캔 단계는
`mail_scan_state.json`의 `source_file`+`mtime`+size+버전 성공 캐시를 먼저 확인해 정상 메일의 DB 조회와
파싱을 피한다. 스캔은 모든 파일을 확인해 캐시 정리·실패 이력을 유지하지만, 성공 캐시 적중과 활성 백오프
파일은 정렬·보관할 처리 후보에서 즉시 제외한다. 캐시가 없는 기존 설치는 사이클당 처리 한도 안에서만
`get_index_state`로 점진적으로 캐시를 만든다.

`mail_uid` 정규화: Knox → `knox:{UniqueID}`, eml → `eml:{Message-ID}`, Outlook → `outlook:{EntryID}` (소스 접두사로 통일).

`mail_scan_state.json`은 **schema_version 2**다. `files`는 정규화한 source file 경로를
키로 쓰므로 항목 안에 경로를 중복 저장하지 않으며, `mtime`·`size`·`mail_uid`·인덱스/UID
해결 버전만 둔다. v1은 최초 읽기에서 성공 캐시·커서·`pending_deletes`를 보존해 v2로
원자 저장한다. `mail_uid`는 동일 UID 복사본의 최신 세대를 판별하는 캐시 요약이므로 유지한다.

**본문 임베딩 시 메타 헤더 삽입**: `index_mail`은 청킹 전 본문 앞에 `제목/발신/수신/날짜` 헤더를 붙여,
"○○가 보낸 메일", "○월 메일" 같은 발신인·날짜 기반 질의도 벡터 검색에 매칭되게 한다.

**교차메일 임베딩 배치**: 파싱·메타헤더·청킹은 메일별로 끝내고 각 청크에 소유 메일·청크 순번을 유지한다.
그 뒤 여러 메일 청크만 평탄화해 최대 32개씩 임베딩하고, 벡터를 다시 메일별로 돌려놓는다. 한 메일은 모든
청크 벡터가 준비될 때만 DB에 저장한다. `EmbeddingContentError`만 이분 분할해 불량 청크를 격리하며,
`EmbeddingTransientError`·`EmbeddingProtocolError`는 분할하지 않고 해당·미처리 메일을 다음 스캔으로 넘긴다.
응답 벡터는 유한한 float32 범위를 확인한 뒤에만 LanceDB에 저장한다.

**같은 UID의 변경 복사본**: 스캔 안에서만 본문·인덱스 메타의 SHA-256 지문을 비교한다(본문·지문은 상태
파일과 로그에 남기지 않음). 내용이 다르고 mtime이 더 새면 기존 세대가 최종 확정된 뒤 STALE 안전 교체를
수행하며, 더 오래된 다른 내용은 새 세대를 되돌리지 않는 shadow로만 처리한다. `batch_commit_every`는
실제 처리 시도의 **최종 결과** 진행률을 묶는 간격이며, 성공 캐시·백오프 skip은 진행률 callback을 만들지 않는다.

**사이클 계측**: 종료 시 상태 읽기·후보 열거/선별/정렬·파싱·DB 상태 확인·임베딩·메일 저장·삭제 재시도·
상태/실패 이력 저장의 시간과 건수를 한 줄로 기록한다. 파일 수, 캐시·백오프·DB 상태, 임베딩 청크/배치/
`EmbeddingClient.embed()` 호출/분할 재시도, 저장 성공·실패만 남긴다. `embed_calls`는 내부 전송 재시도를
제외한 애플리케이션 호출 수이며 HTTP POST 수가 아니다. 본문·복호화 평문·메일별 정상 처리 로그는 남기지 않는다.

**현행 제한**: 내용이 다른 같은 UID 복사본이 `max_mails_per_scan` 때문에 서로 다른 사이클로 나뉘면,
뒤 사이클의 더 오래된 복사본이 최신 세대를 교체할 수 있다. 한 사이클 안의 충돌만 방지하며, 이는 이번
성능 후속에서 고치지 않은 기존 제한이다.

---

## 5. `emails` 테이블 스키마 (`rag/email_indexer.py`)

> `EMAIL_INDEX_VERSION` 이력: v2(메타헤더 임베딩) → **v3**(`mail_date_ts` 추가, 날짜 범위 검색용).
> 버전 범프 시 `_index_version`(source_meta) 불일치로 기존 메일이 자동 1회 재인덱싱된다.

```python
EMAIL_SCHEMA = pa.schema([
    # ── 청크 공통 ──
    pa.field("chunk_id",        pa.string()),
    pa.field("scope",           pa.string()),     # 항상 'local'
    pa.field("indexed_at",      pa.string()),
    pa.field("chunk_index",     pa.int32()),
    pa.field("chunk_total",     pa.int32()),
    pa.field("text",            pa.string()),     # AES-256-GCM 암호화
    pa.field("vector",          pa.list_(pa.float32(), 1024)),
    pa.field("is_deleted",      pa.bool_()),      # 자리만 확보, orphan 정리 미사용
    pa.field("deleted_at",      pa.string()),
    pa.field("miss_count",      pa.int32()),
    pa.field("mtime",           pa.float64()),    # .mysingle 파일 mtime
    # ── 메일 공통 (Knox/Outlook 동일하게 채움) ──
    pa.field("mail_uid",        pa.string()),
    pa.field("source_type",     pa.string()),     # 'knox' | 'outlook'
    pa.field("message_id",      pa.string()),
    pa.field("subject",         pa.string()),
    pa.field("sender",          pa.string()),
    pa.field("recipients",      pa.string()),
    pa.field("mail_date",       pa.string()),     # RFC 원문 문자열 (표시용)
    pa.field("mail_date_ts",    pa.float64()),     # epoch (날짜 범위 검색용, v3부터)
    pa.field("thread_ref",      pa.string()),
    pa.field("source_file",     pa.string()),     # .mysingle 경로 (출처 카드 열기)
    # ── 청크 출처 구분 ──
    pa.field("chunk_origin",    pa.string()),     # 'body' | 'attachment'
    pa.field("attach_filename", pa.string()),
    pa.field("attach_sha256",   pa.string()),
    # ── 소스 고유 봉투 ──
    pa.field("source_meta",     pa.string()),     # JSON: Knox/Outlook 고유 헤더
])
```

**chunks ↔ emails 비교**

| 개념 | chunks (파일) | emails (메일) |
|---|---|---|
| 중복 판별 | 경로 + state.json | `mail_uid` + mtime |
| 삭제 감지 (orphan) | ON | **OFF** (백업저장소) |
| 원본 열기 | `os.startfile(path)` | 탐색기 `/select` 또는 인앱 미리보기 |
| 출처 카드 | 파일명 + 상위 경로 | 제목 + 발신자·날짜 |

---

## 6. get-or-create + 스키마 마이그레이션 패턴

```python
def get_or_create_emails_table(db):
    if "emails" in db.table_names():
        table = db.open_table("emails")
        return _migrate_emails_schema(db, table)  # 누락 컬럼 자동 보강
    return db.create_table("emails", schema=EMAIL_SCHEMA)
```

**스키마 마이그레이션 (v3 회귀 방지)**: `_index_version` 범프는 기존 메일의 *재인덱싱*은
트리거하지만, LanceDB 테이블의 *컬럼*을 자동으로 늘려주지는 않는다. 그래서 v3에서
`mail_date_ts`를 추가했을 때, v2 시절 생성된 테이블은 컬럼이 없어 `table.add()`와 날짜필터
WHERE가 `field 'mail_date_ts' does not exist in table schema`로 실패했다. `_migrate_emails_schema`가
open 시 `EMAIL_SCHEMA` 대비 누락된 스칼라 컬럼을 `table.add_columns({col: SQL기본값})`으로
채워(기존 행 보존) 재생성 없이 해소한다. `add_columns` 미지원·실패 또는 list/vector 등
기본값 생성 불가 컬럼이 있으면 테이블을 재생성해 폴백한다(메일은 백업저장소 =
`.mysingle`/`.eml`에서 다음 스캔에 재인덱싱되므로 데이터 유실 없음).

---

## 7. 출처 카드 동작

`.mysingle`은 더블클릭으로 Knox에 바로 안 열림 → `os.startfile` 대신:

- **기본**: `explorer /select, {source_file}` — 탐색기에서 파일 선택 표시
- **향후**: Knox URL 스킴 확인되면 Knox 직접 열기 추가

출처 카드 표시:
- `badge`: "메일"
- `title`: `subject`
- `subtitle`: `sender · mail_date`
- `path`: `source_file` (.mysingle 경로)

---

## 8. Outlook PST — 보류 항목

**현재 결론**: Outlook 구현 미룬다. 스키마에 연결 끈 확보됨 (`message_id`, `thread_ref`, `source_type`, `source_meta`).

**PST 처리 조사 결과**
- Knox "Outlook 내보내기"는 PST 단일 컨테이너로 떨어짐 (건별 파일 아님).
- 폐쇄망 미러 라이브러리 현황:
  - `pypff` → 동명이인 천체물리 패키지. PST 파서 아님.
  - `extract-msg` → `.msg` 단일 파일 전용 (PST 불가).
  - `libyal pypff` / `libratom` → 미러 없음.

**PST 착수 시 선결 검증**
1. Outlook COM이 폐쇄망 보안정책(COM/매크로 차단, "외부 프로그램 메일 접근" 프롬프트)에 막히는지 확인.
2. PST 내부 메일 단위 증분 감지 설계 (`Message-ID` 기반).
3. Knox 내보내기가 `Message-ID`를 보존하는지 샘플 검증.

**착수 방식 (우선순위)**
1. Outlook COM — `Stores.AddStore`로 PST 마운트 → MAPI 순회 (기존 COM 싱글톤 패턴 확장)
2. `extract-msg` — `.msg` 단일 파일인 경우에만 차선

---

## 9. 메일 스캐너 동작 원칙

Knox 데스크탑 메일함은 **백업저장소**. 웹 Knox에서 메일을 지워도 `.mysingle`은 사라지지 않는다.

| 스캐너 동작 | 파일(chunks) | 메일(emails) |
|---|---|---|
| 신규 감지 | ON | ON |
| 중복 방지 | 경로 기반 | `mail_uid` 기반 |
| 삭제 감지 | ON | **OFF** |

- `CleanupManager`는 `emails` 테이블을 대상으로 하지 않는다.
- `is_deleted`/`deleted_at`/`miss_count` 필드는 스키마에 두되 채우지 않음.

---

## 10. config.yaml — mail 블록

```yaml
mail:
  enabled: true            # watch_folders 내 메일 파일 자동 인덱싱 (기본 켬)
  extensions:              # 인덱싱할 메일 확장자
  - .mysingle
  - .eml
  max_mails_per_scan: 500  # 스캔당 실제 처리 시도 상한 (파싱·DB 확인·인덱싱, 다음 순환에서 계속)
  batch_commit_every: 50   # 최종 결과 기반 진행률 알림 최소 간격(성공 캐시·백오프 skip 제외)
```

`watch_folders`를 공유 — `extensions`에 지정된 확장자를 감지해 자동으로 메일 파이프라인으로 라우팅.
스캔은 `mail_scanner._iter_mail_files`가 `os.scandir` 단일 순회로 처리(확장자별 rglob 아님, `DirEntry.stat` 캐시 재사용).
