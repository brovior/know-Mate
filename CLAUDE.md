# CLAUDE.md — Aegis Desk

> **개인 PC용 사내 지식 AI 비서 데스크톱 앱.** PyQt6 + QWebEngineView 셸 위 멀티 에이전트 구조 + 로컬 RAG.
> 제품명 **Aegis Desk (이지스 데스크)** — 구 KnowMate. 버전 `knowmate/version.py`. 데이터 폴더 `%APPDATA%/AegisDesk`.
> 현재 **베타 배포 단계** (Phase 1~4 · 5a · 5c 완료, 5b 예정).

이 파일은 **작업 규칙만** 담는다. 배경 정보는 필요할 때 아래 문서를 읽는다.

| 필요한 것 | 문서 |
|---|---|
| **문서 지도 (전체 목록)** · 런타임 구조 · 디렉토리 | `docs/ARCHITECTURE.md` |
| **리뷰를 거친 확정 설계 (정본)** | `docs/ai-workflow/requirements.md` (R-) · `architecture.md` (A-) · `adr/` · `reviews/` |
| 설계 결정 근거 | `docs/DESIGN.md` · `docs/RAG_ARCHITECTURE.md` · `docs/EMAIL_DESIGN.md` |
| 진행 단계 · 남은 과제 · 5b 결론 | `docs/ROADMAP.md` |
| OS · 패키지 · 버전 고정 · 빌드/배포 | `docs/ENVIRONMENT.md` |
| 모델 사용 정책 · 수정노트 작성법 | `docs/WORKFLOW.md` |
| 화면 사양 · 룩앤필 | `UI_SPEC.md` · `knowmate/app/ui/mockup.html` |
| 테스터 배포 · 수정노트 | `docs/BETA_GUIDE.md` · `docs/UPDATE_NOTES.md` |

**이미 리뷰를 거친 주제를 다시 건드릴 때는 `docs/ai-workflow/`를 먼저 읽는다.** 요구·설계와 그 리뷰 이력이 거기 있어서, 한 번 기각된 접근을 되풀이하지 않을 수 있다. 현재 등록된 것:

| ID | 주제 | 상태 |
|---|---|---|
| R-0001 / A-0001 / ADR-0001 | 트레이 [종료]가 반드시 프로세스를 끝낸다 — 명시적 quit | Approved / Accepted |
| R-0002 / A-0002 / ADR-0002 | 유휴 사이클의 전체 테이블 로드 제거 — 컬럼 projection + 조건부 스킵 | Approved / Accepted |
| R-0003 / A-0003 | 반복 Open 실패가 `UNKNOWN_TRANSIENT`에 머물지 않게 — 원인축·누적축 분리 | **Draft** (GPT 리뷰 3회 반영, 6a 구현·배포됨 / 6b 보류) |

> R-0003·A-0003이 아직 `Draft`인 것은 **의도된 상태가 아니라 갱신 누락**이다. 6a는 구현·배포까지 끝났고 6b는 실측 데이터 대기로 보류 중이니, 이 주제를 다음에 손댈 때 상태 문언을 실제에 맞게 정리한다.

---

## 작업 전 확인 (2가지)

1. **모델**: 설계·리뷰는 **Opus 이상 필수**(Sonnet 금지 — 먼저 `/model` 전환을 요청할 것), 구현은 Sonnet.
   전문은 `docs/WORKFLOW.md`.
2. **수정노트**: 코드(기능·버그수정) 변경 커밋에는 **항상 `docs/UPDATE_NOTES.md`를 함께 갱신**한다.
   오늘 날짜 `## YYYY-MM-DD (요일)` 섹션에 사용자 관점 1문장. 문서만 고치는 커밋은 제외.
   형식 규칙은 `docs/WORKFLOW.md`.

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

10. **LanceDB API**: `optimize()` 사용(`compact_files()` deprecated 금지). **전체 테이블 로드 금지** — 필요한 컬럼만 `table.search().select([...]).to_arrow()`로 projection한 뒤 `.to_pandas()`로 변환한다. `table.to_pandas()` 직접 호출과 `select()` 없는 `table.to_arrow()`는 둘 다 금지(벡터·암호화 원문까지 전부 메모리에 올라간다). 근거·사고 경위는 `docs/ai-workflow/adr/ADR-0002-purge-projection-and-skip.md` · R-0002/A-0002.

11. **임베딩은 `mode: api`로만 운영한다.** `local`은 폐쇄망에서 모델 다운로드 불가로 무한 대기.

---

## 코딩 규칙

- 함수는 단일 책임. 파일 300줄 초과 시 분리 제안.
- 모든 public 함수에 타입 힌트 + 한 줄 docstring.
- 예외는 삼키지 않는다. 수집기는 파일 1건 실패가 사이클 전체를 멈추지 않도록 건별 try/except.
- 설정값 하드코딩 금지 — 전부 config.yaml (설정 추가는 번들 템플릿 `knowmate/config.yaml`에만).
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
