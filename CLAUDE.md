# CLAUDE.md — Aegis Desk
> Phase 1~4 완료, Phase 5a(메일) 완료, 5c(포터블 빌드) 완료 · 베타 배포 단계 | 2026-07-21
> 제품명: **Aegis Desk (이지스 데스크)** — 구 KnowMate. 버전 `knowmate/version.py` (현재 0.9.0-beta2).
> 데이터 폴더 `%APPDATA%/AegisDesk` (구 KnowMate 폴더는 최초 실행 시 자동 이전). config.yaml·index·logs·km.key·index_state.json 모두 여기 위치.

## 모델 사용 정책

- **분석·설계·계획 수립**: Opus 이상 모델 허용.
- **실제 코드 작성·구현**: Sonnet 사용. 구현 착수 시점에 현재 모델이 Opus 계열이면 사용자에게 Sonnet 전환(`/model`)을 먼저 안내할 것.
- **예외**: 동시성(스레드), 보안(crypto), 대규모 리팩터링 등 고위험 구현은 사용자 판단으로 상위 모델 허용.
- 참고: `/model opusplan`은 계획=Opus / 실행=Sonnet을 자동 전환한다.

## 수정노트 관리 (`docs/UPDATE_NOTES.md`)

**코드(기능·버그수정) 변경을 커밋할 때마다 반드시 함께 갱신한다.** 문서만 고치는 커밋(오타, 서식)은 대상 아님.

- 오늘 날짜(요일 포함, 예: `## 2026-07-21 (화)`) 섹션이 이미 있으면 그 아래에 항목만 추가. 없으면 파일 맨 위(최신이 위)에 새 섹션을 만든다.
- 날짜 형식은 `## YYYY-MM-DD (요일)` — 요일은 반드시 함께 적는다(베타 테스터가 "무슨 요일에 뭐가 바뀌었는지"를 보는 용도).
- 한 줄은 **사용자(테스터) 관점 요약** 1문장. 커밋 메시지를 그대로 붙여넣지 말고 "무엇이 바뀌어 사용자에게 뭐가 달라지는지"로 쓴다. 내부 리팩터링·테스트 추가처럼 사용자에게 안 보이는 변경은 생략 가능.
- 버전이 올라간 날은 섹션 제목에 버전을 함께 적는다: `## 2026-07-21 (화) — v0.9.0-beta2`.
- 베타 시작일은 2026-07-16(목) — 파일 상단에 고정.

## 프로젝트 개요

**개인 PC용 사내 지식 AI 비서 데스크톱 앱.** PyQt6 + QWebEngineView 셸 위에서 여러 에이전트가 구동되는 멀티 에이전트 구조. Phase 1~4 완료(RAG 지식검색), 5a 완료(Knox `.mysingle` + 표준 `.eml` 메일 인덱싱), 5c 완료(PyInstaller 포터블 빌드). 현재 소수 대상 **베타 배포 단계**. 5b(공용 벡터DB)는 SMB 위 LanceDB 직접 실행 불가로 "로컬 캐시 복사" 방식 확정, 착수 예정.

핵심 시나리오: "작년 A설비 알람 폭주 때 처리 절차 찾아줘" → 로컬 문서·메일 의미 검색 → 요약 답변 + 출처 제시.

---

## 아키텍처

```
PyQt6 + QWebEngineView
  ├─ HTML/JS UI (index.html / app.js / styles.css)
  │    └─ QWebChannel 브리지 (bridge.py)
  ├─ AgentRegistry
  │    ├─ knowledge_agent  ← RAG 지식검색 (완료)
  │    └─ mes_agent        ← stub ("준비 중")
  ├─ RAG 파이프라인
  │    ├─ Indexer       → chunker → embedding → LanceDB chunks 테이블
  │    ├─ EmailIndexer  → chunker → embedding → LanceDB emails 테이블 (Knox 메일)
  │    └─ Retriever → 벡터검색(chunks+emails 병합) → 권한필터 → 샌드위치배열 → LLM
  └─ CollectorWorker (QThread)
       ├─ Scanner(scandir) → 생산자 스레드 → 큐 → 소비자(추출·임베딩·저장)  ← 스캔·인덱싱 파이프라인
       │                     TextExtractor → Indexer, CleanupManager
       └─ MailScanner(scandir) → parse_mail_file(.mysingle/.eml) → EmailIndexer (orphan 정리 없음)
```

- **UI 셸 상주**: 닫기(X) 시 시스템 트레이로 숨김(설정으로 종료 전환 가능). 유휴 시 자동 인덱싱(설정으로 on/off·주기 조정).
- **설정 패널**(⚙): 연결(LLM/임베딩 주소)·검색(엄격도·문서수)·인덱싱(유휴·메일·파일크기·정리삭제)·동작(닫기·로그레벨) + 연결 테스트. `bridge.getSettings/saveSettings/testConnection/openConfigFile`.
- **파일 로깅**: `%APPDATA%/AegisDesk/logs/aegisdesk.log` (Rotating 5MB×3) + 전역 excepthook.

---

## 디렉토리 구조

```
AegisDesk.spec · build.bat      # PyInstaller 포터블 빌드 (onedir) · 사내 원클릭 빌드
knowmate/
 ├─ app/          main.py · bridge.py · threads.py · ui/
 ├─ agents/       base.py · registry.py · knowledge_agent.py · mes_agent.py
 ├─ rag/          indexer.py · email_indexer.py · retriever.py · embedding.py · chunker.py
 ├─ collector/    scanner.py · mail_scanner.py · cleanup.py · state.py · scheduler.py
 ├─ secure/       base.py · plain_reader.py · com_reader.py · fake_reader.py
 │                mysingle_reader.py · crypto.py · signature.py · text_util.py
 ├─ llm/          client.py
 ├─ version.py    # __version__ (릴리스마다 갱신)
 ├─ config.py     # config.yaml 로더 (번들 템플릿 → %APPDATA% 시드, watch_folders만 초기화)
 ├─ config.yaml   # 배포 기본값 템플릿 (설정 추가 시 여기에만). 실사용본은 %APPDATA%/AegisDesk/config.yaml
 └─ tests/        test_phase1~4.py · test_mysingle.py · fixtures/sample.mysingle
scripts/          diag_search.py · inspect_index.py · test_shared_db.py(5b 사전검증)
```

참고 문서: `UI_SPEC.md`(루트, 화면 사양) · `app/ui/mockup.html`(룩앤필) · `docs/DESIGN.md`(설계 결정) · `docs/EMAIL_DESIGN.md`(메일 인덱싱) · `docs/BETA_GUIDE.md`(테스터 배포 가이드) · `docs/UPDATE_NOTES.md`(베타 수정노트, 요일별)

---

## 핵심 원칙 (위반 금지)

1. **에이전트 인터페이스**: `handle(query: str, context: dict) -> list[Block]` 하나로만 통신. UI에 에이전트별 분기 로직 금지.

2. **임베딩 모델·차원은 `rag/embedding.py` 상수로 고정** (`VECTOR_DIM = 1024`). 모델 변경 시 전체 재인덱싱 필수.

3. **보안 의존 코드는 `secure/` 밖으로 나가지 않는다.** COM/DPAPI를 다른 모듈에서 직접 import 금지.

4. **mock 전환은 config 한 줄.** `extractor: fake | plain | auto`. fake 모드로 사외 전체 테스트 통과해야 함.

5. **벡터DB 원문(`text` 컬럼)은 반드시 AES-256-GCM 암호화 저장.** 복호화 평문을 파일·로그에 남기지 않는다.

6. **공용 벡터DB에 개인 PC는 절대 쓰기하지 않는다.**

7. **로그에 문서·메일 본문 출력 금지.** 경로·건수·소요시간만.

8. **수집기는 QThread 워커에서만 실행.** multiprocessing 금지 (LanceDB 파일 락 충돌).

9. **scopes 빈 배열이면 전체 검색 fallback 금지.** JS 1차 + knowledge_agent 2차 차단.

10. **LanceDB API**: `optimize()` 사용 (`compact_files()` deprecated 금지). DataFrame 변환은 `table.to_arrow().to_pandas()` (`table.to_pandas()` 직접 금지).

---

## 구현 단계

| Phase | 내용 | 상태 |
|---|---|---|
| 1 | UI 셸 + 에이전트 골격 | ✅ 완료 |
| 2 | RAG 파이프라인 (chunker/embedding/indexer/retriever) | ✅ 완료 |
| 3 | 수집기 (증분스캔 + orphan정리 + 스케줄러) | ✅ 완료 |
| 4 | 보안 모듈 (COM 싱글톤 + AES-GCM + DPAPI) | ✅ 완료 |
| 5a | 메일 인덱싱 (`.mysingle` Knox + `.eml` 표준) | ✅ 완료 |
| 5c | PyInstaller 포터블 빌드(onedir) + 파일 로깅·버전·설정 패널·트레이 상주 | ✅ 완료 |
| 베타 | 소수 테스터 배포 (`docs/BETA_GUIDE.md`) | 🔄 진행 |
| 5b | 공용 벡터DB (로컬 캐시 복사 방식) | 🔲 예정 |

> **5b 결론**: SMB 위에서 LanceDB 직접 읽기/쓰기 불가(RustPanic, `scripts/test_shared_db.py`로 확인). → 마스터가 로컬 인덱싱 후 공용 폴더로 **복사 배포**, 사용자는 파트 최상위 `_aegisdesk/`를 상위 탐색으로 발견해 **로컬 캐시로 복사 후 읽기**. 검색은 지정 폴더 범위로 접두 필터.
> **Outlook PST/.msg**: COM/전용 파서 필요, COM 보안 정책 선결 검증 후 착수. 상세는 `docs/EMAIL_DESIGN.md` §8.
> **날짜 기반 검색 필터**: ✅ 완료. `rag/date_filter.py`(규칙기반 한국어 파서)로 질의의 "지난주/3월/25주차" 등을
> 기간으로 변환해 chunks(`mtime`)·emails(`mail_date_ts`) 검색에 적용. 상세는 `docs/DESIGN.md` §검색 파라미터.
> **차후 과제**: 추출·임베딩 병렬화(P3), batch_size 튜닝(P4), 기간 나열형 전용 정렬 모드(v2).

---

## 환경

- **OS**: Windows 10/11 · **Python**: 3.11
- **임베딩 운영 모드**: 반드시 `mode: api` (config 기본값도 api). `local` 모드는 폐쇄망에서 모델 다운로드 불가로 무한 대기 발생.
- **패키지**: PyQt6, PyQt6-WebEngine, lancedb, pyarrow, pywin32, cryptography, PyMuPDF, python-docx, openpyxl, python-pptx, pytest. 빌드 시 pyinstaller 추가.
- **배포**: `build.bat` → `dist/AegisDesk/`(exe + `_internal` 폴더) 를 통째로 zip 배포. exe만 단독 배포 불가.
- **네트워크 드라이브**: 일반 SMB(K: 등)는 정상. EFSS2 DRM 드라이브(M: 등)는 화이트리스트 프로세스만 접근 가능해 인덱싱 불가. 나스카 DRM 문서는 SSO 로그인 유지 중에만 복호화.

---

## 코딩 규칙

- 함수는 단일 책임. 파일 300줄 초과 시 분리 제안.
- 모든 public 함수에 타입 힌트 + 한 줄 docstring.
- 예외는 삼키지 않는다. 수집기는 파일 1건 실패가 사이클 전체를 멈추지 않도록 건별 try/except.
- 설정값 하드코딩 금지 — 전부 config.yaml.
- UI 작업 시 `UI_SPEC.md` · `mockup.html` 먼저 읽고, 스펙과 다른 판단 필요 시 먼저 묻는다.
- 로그 레벨: DEBUG(흐름 추적) / INFO(정상 결과) / WARNING(복구 가능) / ERROR(즉시 확인).

<!-- ai-dev-workflow:review-recipe (init_project.py가 자동 주입·갱신 — 이 블록은 직접 수정하지 말 것) -->
## 설계 리뷰 요청 처리 (ai-dev-workflow)

> ⚠️ **모델 규칙 (필수)**: 이 설계·리뷰 워크플로(Chief Architect 판단)는 **반드시 Opus 이상 모델**로
> 수행한다. **현재 세션이 Sonnet 이하이면, 리뷰·설계 작업을 시작하기 전에 사용자에게 "Opus 이상으로
> 모델을 변경해 달라"고 먼저 요청**하고, 변경 전까지 진행하지 않는다. (설계 확정 후의 코딩·구현은 Sonnet도 허용.)

사용자가 "설계 리뷰" / "GPT 리뷰" / "리뷰 받아줘" 등을 요청하면 — **네가 직접 리뷰하지 말고** 아래대로 한다:

1. 리뷰 대상 설계를 `docs/ai-workflow/`(architecture.md·requirements.md·adr/ 등)에 작성·갱신한다.
2. 새 브랜치로 커밋·푸시한다.
3. base=main으로 **설계 PR을 연다.**
   → `.github/workflows/gpt-design-review.yml`(채널 B)가 자동으로 **GPT 독립 리뷰**를 PR 코멘트로 단다
   (repo Secret `OPENAI_API_KEY` 필요 — 없으면 무해 skip).
4. 리뷰가 달리면 항목별 수용/기각을 판단·반영하고 `docs/ai-workflow/reviews/`에 처리 기록을 남긴다.

핵심: **"리뷰 요청 = 설계 PR 열기"** 이며, 리뷰는 GitHub Action이 GPT를 호출해 수행한다(에이전트가 직접 하지 않음). 워크플로는 이미 이 저장소에 설정돼 있으니 새로 만들지 않는다.

- **PC 즉시 실행**(API 키 환경변수 있을 때): `python scripts/ai_workflow/gpt_review.py <문서> [--source <dir>]`
- **정액 구독만 있을 때**(채널 C): 위 명령에 `--emit`을 붙여 나온 파일을 ChatGPT에 붙여넣는다.
- 정본·상세: `docs/ai-workflow/README.md` (규약 정본은 `brovior/ai-dev-workflow`).
<!-- /ai-dev-workflow:review-recipe -->
