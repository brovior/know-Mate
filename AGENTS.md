# AGENTS.md — Aegis Desk

Codex와 다른 개발 에이전트가 이 저장소에서 작업할 때 따르는 진입 지침이다.

## 공통 계약

**[`CLAUDE.md`](CLAUDE.md)가 이 저장소의 공통 개발 계약 정본이다.** 작업을 시작하기 전에 읽고,
보안·검색·LanceDB·수집기 불변식과 코드 변경 규칙을 그대로 따른다. 두 파일의 내용이 다르면
`CLAUDE.md`를 우선한다.

## 작업 순서

1. [`docs/ROADMAP.md`](docs/ROADMAP.md)에서 현재 단계와 보류 조건을 확인한다.
2. 요청과 관련된 설계 문서는 `CLAUDE.md §4`의 문서 표에서 찾아 읽는다.
3. 사용자 변경을 보존하고 요청 범위만 수정한다.
4. 코드 변경은 관련 테스트로 검증하고 [`docs/UPDATE_NOTES.md`](docs/UPDATE_NOTES.md)를 함께 갱신한다.
   문서만 바꾸는 변경은 수정노트 대상이 아니다.

## 핵심 주의사항

- 문서·메일 원문과 복호화 평문을 파일이나 로그에 남기지 않는다.
- LanceDB 전체 테이블을 메모리에 올리지 않고 필요한 컬럼만 projection 조회한다.
- 수집기는 QThread를 사용하며 `multiprocessing`을 도입하지 않는다.
- UI 변경 전 `UI_SPEC.md`와 `knowmate/app/ui/mockup.html`을 확인한다.
- 사외 검증은 fake 모드를 사용하고, Windows·Office·사내망 의존 검증은 미실행 범위를 명시한다.

## 문서 찾기

- 전체 구조와 문서 지도: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- 상세 설계: [`docs/DESIGN.md`](docs/DESIGN.md)
- 현재 계획: [`docs/ROADMAP.md`](docs/ROADMAP.md)
- 개발·배포 환경: [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md)
- 화면 사양: [`UI_SPEC.md`](UI_SPEC.md)

폐지된 `docs/ai-workflow/` 절차는 사용하지 않는다. 과거 논의가 필요할 때만 Git 이력을 확인한다.
