# CLAUDE.md — Aegis Desk

이 파일은 Claude Code와 개발자가 이 저장소에서 작업할 때 따르는 **공통 개발 계약**이다.
매 세션 반드시 필요한 규칙만 두고, 구조·설계·운영 상세는 관련 문서로 위임한다.

**충돌 시 우선순위**:
`이 파일의 불변식(§2)` > `이 파일의 나머지` > `docs/dev/` 작업 지침
> `UI_SPEC.md`와 `docs/DESIGN.md` 등 위임 문서 > `README.md`와 배포 안내 > 코드 주석

---

## 0. 작업 시작 전

- 현재 단계와 다음 과제는 [`docs/ROADMAP.md`](docs/ROADMAP.md)를 먼저 확인한다.
- 문서를 작성하거나 개편할 때는
  [`docs/dev/document_guidelines.md`](docs/dev/document_guidelines.md)를 따른다.
- 사용자에게 답할 때는 [`docs/dev/response_style.md`](docs/dev/response_style.md)의
  결론 우선·간결한 표현 원칙을 따른다.
- 코드의 기능이나 동작을 바꾸면 같은 변경에서 [`docs/UPDATE_NOTES.md`](docs/UPDATE_NOTES.md)를
  갱신한다. 문서만 고치는 변경은 제외한다. 형식은 [`docs/WORKFLOW.md`](docs/WORKFLOW.md)를 따른다.
- 기존 작업 트리가 깨끗하지 않으면 사용자 변경을 보존하고, 요청과 무관한 파일은 수정하지 않는다.

---

## 1. 프로젝트가 하는 일

**Aegis Desk(구 KnowMate)**는 Windows 개인 PC에서 사내 문서와 메일을 검색하는 데스크톱 지식
비서다. PyQt6·QWebEngineView UI에서 질문을 받으며, 로컬 LanceDB의 문서·메일 청크를 검색한 뒤
사내 LLM API로 답변과 출처를 만든다.

- 지원 문서: `docx`, `xlsx`, `pptx`, `pdf`, `txt`, `doc`, `xls`, `ppt`
- 지원 메일: Knox `.mysingle`, 표준 `.eml`
- 데이터 위치: `%APPDATA%/AegisDesk`
- 버전 정본: `knowmate/version.py`
- 현재 상태: Phase 1~4·5a·5c 완료, 베타 배포 중, 공용 벡터DB(5b) 예정

---

## 2. 불변식 — 위반하면 안 되는 규칙

### 2-1. 보안과 데이터

- LanceDB `text` 컬럼의 원문은 반드시 **AES-256-GCM**으로 암호화해 저장한다. 키는 Windows
  DPAPI로 보호한다.
- 복호화한 평문과 문서·메일 본문을 파일이나 로그에 남기지 않는다. 로그에는 경로·건수·소요시간만
  기록한다.
- COM·DPAPI 등 Windows 보안 의존 코드는 `knowmate/secure/`에 격리한다. 다른 패키지에서
  `win32com`이나 `win32crypt`를 직접 import하지 않는다.
- 개인 PC는 공용 벡터DB에 절대 쓰지 않는다.

### 2-2. 검색과 저장소

- 에이전트는 `handle(query: str, context: dict) -> list[Block]` 인터페이스로만 UI와 통신한다.
  UI에 에이전트별 응답 분기 로직을 넣지 않는다.
- 임베딩 모델과 차원은 `knowmate/rag/embedding.py`의 상수로 고정한다. 현재 벡터 차원은 1024이며,
  모델이나 인덱싱 포맷을 바꾸면 인덱스 버전을 올려 전체 재인덱싱한다.
- 운영 임베딩 모드는 `api`만 사용한다. `local` 모드는 폐쇄망에서 모델 다운로드를 시도할 수 있다.
- 검색 범위 `scopes`가 비어 있으면 전체 검색으로 되돌아가지 않는다. UI와
  `knowledge_agent` 양쪽에서 차단한다.
- LanceDB 조회는 필요한 컬럼만 `table.search().select([...]).to_arrow()`로 가져온다.
  `table.to_pandas()`와 `select()` 없는 전체 `to_arrow()` 호출은 금지한다.
- LanceDB 정리는 `optimize()`를 사용한다. 폐기된 `compact_files()`를 사용하지 않는다.

### 2-3. 수집기와 실행 환경

- 수집기는 QThread 워커에서 실행한다. LanceDB 파일 잠금 충돌을 일으킬 수 있는
  `multiprocessing`은 사용하지 않는다.
- 파일 하나의 추출 실패가 전체 인덱싱 사이클을 중단하지 않도록 파일 단위로 예외를 격리한다.
- `extractor: fake | plain | auto` 전환은 설정 한 곳에서 유지한다. 사외 환경에서는 fake 모드로
  전체 테스트가 통과해야 한다.
- 설정값을 코드에 중복 하드코딩하지 않는다. 배포 기본값은 `knowmate/config.yaml`에만 추가하고,
  실행 중 설정은 `%APPDATA%/AegisDesk/config.yaml`을 사용한다.

---

## 3. 코드 변경 규칙

- 코드를 쓰거나 검토할 때는
  [`docs/dev/karpathy_guidelines.md`](docs/dev/karpathy_guidelines.md)의 전제 명시·최소 구현·외과적
  수정·검증 기준 원칙을 따른다. 이 파일의 §2 불변식과 충돌하면 §2를 우선한다.
- 요청에 필요한 범위만 수정하고, 무관한 리팩터링이나 포매팅을 섞지 않는다.
- 함수는 한 가지 책임만 갖게 한다. 파일이 300줄을 넘으면 책임 분리를 검토한다.
- 모든 public 함수와 클래스에 타입 힌트와 한 줄 docstring을 작성한다.
- 예외를 조용히 삼키지 않는다. 복구 가능한 실패는 적절한 로그와 상태로 남긴다.
- UI를 바꾸기 전에는 [`UI_SPEC.md`](UI_SPEC.md)와 `knowmate/app/ui/mockup.html`을 확인한다.
- 로그 수준은 `DEBUG`(흐름), `INFO`(정상 결과), `WARNING`(복구 가능), `ERROR`(즉시 확인)로 구분한다.
- 코드 변경 후 `pytest knowmate/tests -v`를 실행한다. Windows·Office·사내망이 필요한 검증은 실행
  가능 여부와 미검증 범위를 결과에 명시한다.

---

## 4. 위임 문서

| 필요한 정보 | 정본 |
|---|---|
| 문서 작성·개편 방식 | [`docs/dev/document_guidelines.md`](docs/dev/document_guidelines.md) |
| 코드 작성·검토 태도 | [`docs/dev/karpathy_guidelines.md`](docs/dev/karpathy_guidelines.md) |
| 사용자 응답 방식 | [`docs/dev/response_style.md`](docs/dev/response_style.md) |
| 런타임 구조·디렉토리·문서 지도 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| 상세 설계 결정·실패 처리·LanceDB 운용 | [`docs/DESIGN.md`](docs/DESIGN.md) |
| 메일 파싱·저장·검색 계약 | [`docs/EMAIL_DESIGN.md`](docs/EMAIL_DESIGN.md) |
| 현재 단계·남은 과제·보류 사유 | [`docs/ROADMAP.md`](docs/ROADMAP.md) |
| 개발·배포 환경과 버전 고정 | [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md) |
| 화면 동작과 시각 규칙 | [`UI_SPEC.md`](UI_SPEC.md) |
| 베타 배포와 사용자 안내 | [`docs/BETA_GUIDE.md`](docs/BETA_GUIDE.md) |
| 사용자 관점 변경 이력 | [`docs/UPDATE_NOTES.md`](docs/UPDATE_NOTES.md) |
| 수정노트 작성 규칙 | [`docs/WORKFLOW.md`](docs/WORKFLOW.md) |
| 쿼리 비동기화 보류 설계 | [`docs/ISSUE_B_query_async.md`](docs/ISSUE_B_query_async.md) |
| 보안 패키지 운영·수동 검증 | [`knowmate/secure/README.md`](knowmate/secure/README.md) |

`docs/ai-workflow/` 기반 설계 원장과 자동 GPT 리뷰 절차는 폐지됐다. 과거 판단이 필요하면 Git
이력에서 확인하고, 현행 계약의 근거로 직접 인용하지 않는다.

---

<tone_preference>
답변은 결론부터 짧고 쉽게 작성한다. 세부 구현과 긴 근거는 사용자가 요청할 때 덧붙인다.
</tone_preference>
