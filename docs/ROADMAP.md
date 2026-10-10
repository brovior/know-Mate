# ROADMAP.md — 구현 단계와 남은 과제

> CLAUDE.md에서 분리. 진행 상태가 바뀌면 이 파일을 갱신한다.

## 현재 위치 (2026-10-06 문서 정리 기준)

Phase 1~4 완료(RAG 지식검색), 5a 완료(Knox `.mysingle` + 표준 `.eml` 메일 인덱싱),
5c 완료(PyInstaller 포터블 빌드). 현재 소수 대상 **베타 배포 단계**.
버전은 `knowmate/version.py`.

베타 중 진행된 **COM 안정화 흐름**(3~6단계, 상세는 `docs/design/DESIGN.md`):
실패 이력 기록 → 유형별 백오프 → [확인 필요한 문서] 화면 → 실패 분류 정교화(6a).
정상종료 유예·행오버 워치독·세이프모드 표식 정리와 RPC 단절 복구가 적용됐다.
9월에는 상태 저장 집약·종료 복구·유휴 메일 스캔·적응형 DB 정리가 추가됐다.

핵심 시나리오: "작년 A설비 알람 폭주 때 처리 절차 찾아줘" → 로컬 문서·메일 의미 검색 → 요약 답변 + 출처 제시.

## 단계

| Phase | 내용 | 상태 |
|---|---|---|
| 1 | UI 셸 + 에이전트 골격 | ✅ 완료 |
| 2 | RAG 파이프라인 (chunker/embedding/indexer/retriever) | ✅ 완료 |
| 3 | 수집기 (증분스캔 + orphan정리 + 스케줄러) | ✅ 완료 |
| 4 | 보안 모듈 (COM 싱글톤 + AES-GCM + DPAPI) | ✅ 완료 |
| 5a | 메일 인덱싱 (`.mysingle` Knox + `.eml` 표준) | ✅ 완료 |
| 5c | PyInstaller 포터블 빌드(onedir) + 파일 로깅·버전·설정 패널·트레이 상주 | ✅ 완료 |
| 베타 | 소수 테스터 배포 (`docs/guides/BETA_GUIDE.md`) | 🔄 진행 |
| 5b | 공용 벡터DB (로컬 캐시 복사 방식) | 🔲 예정 |

## 결정·메모

- **5b 결론**: SMB 위에서 LanceDB 직접 읽기/쓰기 불가(RustPanic, `scripts/test_shared_db.py`로 확인).
  → 마스터가 로컬 인덱싱 후 공용 폴더로 **복사 배포**, 사용자는 파트 최상위 `_aegisdesk/`를
  상위 탐색으로 발견해 **로컬 캐시로 복사 후 읽기**. 검색은 지정 폴더 범위로 접두 필터.
- **Outlook PST/.msg**: COM/전용 파서 필요, COM 보안 정책 선결 검증 후 착수. 상세는 `docs/design/EMAIL_DESIGN.md` §8.
- **날짜 기반 검색 필터**: ✅ 완료. `rag/date_filter.py`(규칙기반 한국어 파서)로 질의의 "지난주/3월/25주차" 등을
  기간으로 변환해 chunks(`mtime`)·emails(`mail_date_ts`) 검색에 적용. 상세는 `docs/design/DESIGN.md` §검색 파라미터.
- **차후 과제**: 기간 나열형 전용 정렬 모드(v2). SQLite·검색 인덱스·조회 경량화·병렬화·배치 조정은
  [진단 문서](DIAGNOSTICS.md)의 실측 조건을 충족한 뒤 별도 설계한다.

## 확정된 결함 (2026-09-06)

성능 분석([`PERF_ANALYSIS.md`](archive/PERF_ANALYSIS.md))을 Codex와 교차검증하다 발견한 코드 결함.
성능 문제가 아니라 **결과 누락·데이터 문제**라 성능 작업보다 앞서 수정했다. 회귀 테스트는
각 기능 테스트 파일로 옮겨 일반 테스트로 유지한다.

- **✅ [#81](https://github.com/brovior/know-Mate/issues/81) 재인덱싱 순서 수정 완료**: 새 청크를
  먼저 저장하고 기존 청크를 물리 삭제하도록 변경했다. 삭제 실패 시 기존 ID를 state의
  `pending_delete_chunk_ids`에 보존해 다음 수집 사이클에서 재시도한다. 회귀 테스트는
  `test_phase2.py`와 `test_phase3.py`로 옮겼다.
- **✅ [#82](https://github.com/brovior/know-Mate/issues/82) 메일 스캔 절단 순서 수정 완료**:
  모든 메일 후보를 순환 커서로 훑고, 실제 파싱·DB 확인·인덱싱 시도만
  `max_mails_per_scan`으로 제한한다. 성공 캐시와 실패 백오프를 별도 sidecar에
  저장해, 한도를 넘는 오래된 메일도 다음 사이클에서 빠짐없이 처리한다.
- **✅ #82 후속 계측 완료**: 메일 수집 사이클마다 상태 읽기·후보 선별·파싱·DB 확인·임베딩·저장과
  상태 저장의 시간 및 건수만 한 줄로 남긴다. 메일 본문은 로그에 남기지 않으며, 이 데이터로 다음
  최적화 우선순위를 판단한다.

## 다음 작업·보류 조건

원인 분석과 검증 상세는 [DIAGNOSTICS.md](DIAGNOSTICS.md)에 모은다.

| 항목 | 상태·다음 행동 | 시작 조건 |
|---|---|---|
| 앱 크래시 | 사내 로그·Windows 오류 기록 확보 | 기존 기록이 부족하면 진단 보강을 먼저 설계 |
| 메일 처리 중 종료 후 Office 잔류 | 문서 완료 시 Office 정리·임베딩 취소·단계별 종료 로그 구현, fake·localhost 회귀 통과 | [진단 §4](DIAGNOSTICS.md#4-office-종료-정리-확인)의 사내 종료·PID 확인 대기 |
| 상태 JSON·유휴 스캔 | 단계별 실측 대기 | JSON 처리와 실제 파일 접근 비용을 구분한 뒤 개선 선택 |
| 검색·임베딩·COM 성능 | 실측 대기 | 진단 문서의 검증 자료 확보 |
| 질문 비동기화 | 미착수 | [상세 설계](design/ISSUE_B_query_async.md)에 따른 별도 변경·검증 |
| 실패 원인 판별 6b | 보류 | 오류 코드 실측, 워치독 밖 probe I/O의 행오버 위험 해소 |
| 공용 인덱스 5b | 예정 | 로컬 캐시 복사 배포 설계·검증 |
| DRM 저장 정책 | 사내 확인 대기 | 코드 변경이 아닌 정책 확인. [DESIGN.md](design/DESIGN.md) 참고 |
