# EMAIL_DESIGN.md — Aegis Desk 메일 인덱싱 설계
> 상태: **Phase 5a 구현 완료** (2026-06-26)
> Knox `.mysingle` 구현 완료 · Outlook PST 보류

---

## 0. 범위와 우선순위

| 소스 | 상태 | 비고 |
|---|---|---|
| **Knox `.mysingle`** | ✅ 구현 완료 | "메일 1통 = 파일 1개" → 기존 파일 스캐너 모델에 얹힘 |
| **Outlook PST/.msg** | 🔲 보류 | COM 보안 정책 선결 검증 필요. §8 참조 |

**설계 원칙**: mysingle로 메일 파이프라인 뼈대를 세우고, Outlook은 그 위에 얹는다.
스키마는 **Outlook까지 고려한 풀 스키마**로 한 번에 정의 (LanceDB는 ALTER가 비싸므로).

---

## 1. `.mysingle` 파싱 (`secure/mysingle_reader.py`)

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
| 1 | `mail_uid` = `knox:{X-Desktop-Msg-UniqueID}` | 같은 메일 재인덱싱 방지 |
| 2 | `mtime` 비교 | 내용 변경 감지 (`is_indexed(mail_uid, mtime)`) |
| 3 | `message_id` (RFC, 소스 간 공통) | (향후) Knox↔Outlook 소스 간 dedup |
| 4 | `attach_sha256` | (향후) 동일 첨부 중복 방지 |

`mail_uid` 정규화: Knox → `knox:{UniqueID}`, Outlook → `outlook:{EntryID}` (소스 접두사로 통일).

---

## 5. `emails` 테이블 스키마 (`rag/email_indexer.py`)

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
    pa.field("mail_date",       pa.string()),
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

## 6. get-or-create 패턴

```python
def get_or_create_emails_table(db):
    if "emails" in db.table_names():
        return db.open_table("emails")
    return db.create_table("emails", schema=EMAIL_SCHEMA)
```

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
  enabled: false           # true로 바꾸면 watch_folders 내 .mysingle 자동 인덱싱
  max_mails_per_scan: 500  # 스캔당 처리 상한 (최신 mtime 순)
  batch_commit_every: 50   # state 중간 저장 주기
```

`watch_folders`를 공유 — `.mysingle` 확장자를 감지해 자동으로 메일 파이프라인으로 라우팅.
