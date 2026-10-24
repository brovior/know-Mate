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
| COM poison 복구 뒤 점유 오판·오류 혼합 | 2초 캐시 epoch·정확한 앱 증명·최초/닫기 오류 분리 및 fake 회귀 구현 | [진단 §COM poison](DIAGNOSTICS.md#com-poison-복구-일관성-2026-10-15)의 실제 Windows·Office·DRM 검증 대기 |
| 메모리 진단 부하 분리 | OFF 유지·basic 수치 로그·명시적 detailed 추적과 진단 예외 격리 구현, 로컬 합성 비교 완료 | [진단 §1](DIAGNOSTICS.md#1-앱-크래시와-메모리-진단)의 실제 Windows OFF/basic 성능 비교·장시간/취소 시험 대기. 크래시 해결은 미확정 |
| 앱 크래시 | 사내 로그·Windows 오류 기록 확보 | 기존 기록이 부족하면 진단 보강을 먼저 설계 |
| 메일 처리 중 종료 후 Office 잔류 | 문서 완료 시 Office 정리·임베딩 취소·단계별 종료 로그 구현, fake·localhost 회귀 통과 | [진단 §4](DIAGNOSTICS.md#4-office-종료-정리-확인)의 사내 종료·PID 확인 대기 |
| 상태 JSON·유휴 스캔 | 단계별 실측 대기 | JSON 처리와 실제 파일 접근 비용을 구분한 뒤 개선 선택 |
| 검색·임베딩·COM 성능 | 실측 대기 | 진단 문서의 검증 자료 확보 |
| PowerPoint 추가 기능 운영 적용 | 1.0.4.0 요청 전용 native 저장 후보, 사내 ACL PASS·writer 일부 통과 보고. 링크 fixture·정리·stdout·최종 결과 요약 보완, 기본 disabled 유지 | [저장 검증](guides/PPT_READER.md#ppt-요청-저장과-충돌-진단)과 다른 관리자 UAC 후 원래 helper 완료 저장을 우선 확인. [운영 설계](design/PPT_ADDIN_INDEXING.md)·[검증 절차](guides/PPT_READER.md)의 최종 released 파일 승인 시험 후 동일 파일 배포. 기존 orphan-only 임시파일 복구는 별도 과제 |
| PowerPoint 인덱싱 후 자동종료 #116 | 같은 STA의 FileExit·반환 이후 닫기 보호 후보 구현. frozen 인덱싱 및 동일 호스트의 마지막 검사 직후 열림 경합 4사례 확인. 기본 disabled 유지 | [정밀 경합 검증](DIAGNOSTICS.md#같은-프로세스의-마지막-검사-직후-열림-경합-재현-2026-10-24): 현재 Windows x64·Office에서 저장/미저장 문서 1개·2개 보호 확인. 사내 DRM·다른 Office·일반 배포 승인 보류 |
| PowerPoint 종료 3방식 비교 | 설치된 Reader로 외부 Quit·내부 Quit·참조 해제를 새 프로세스에서 비교하는 명시적 진단 | [격리된 VDI 실행](guides/PPT_READER.md#powerpoint-종료-3방식-비교-진단)의 실제 Windows x86/x64·frozen·Office·DRM 검증. 일반 배포 종료 안전성 승인을 대신하지 않음 |
| PowerPoint 종료 진단 한 번 수집 | core 우선 관측·단회 요청·외부 보존·schema3 판독 및 별도1.0.6 진단 빌드 후보 구현. live 잠금 유지 | [새 진단 안내](guides/PPT_CAPTURE.md)의 x86/x64 native·정확한 패키지 pin·Office 합성 gate 대기. 기존 checkpoint121 잠금·일반 배포 기본값 유지 |
| PowerPoint 참조 수명·원 handle 비교 | native MTA scope·예외 프레임 정리·원 handle 관측과 별도1.0.7 진단 후보. mock/replay만 허용 | [진단 안내](guides/PPT_CAPTURE.md#별도-참조-수명-비교-후보-1070)의 Windows·Office·사용자 보호 증거 대기. live·빌드·설치·등록·일반 배포 활성화 미실행 |
| 질문 비동기화 | 대화 탐색 시 요청 유지·원래 대화 응답 저장·복귀 시 로딩 복원 수정. JS 40사례·관련 pytest 41개·실제 Qt 합성 2사례 확인. 전체 2,175 PASS, 기존 링크 권한 3 FAIL | [대화 이동 응답 검증](DIAGNOSTICS.md#대화-이동-뒤-응답-중단-2026-10-24)의 사내 API·장시간 질문 검증 대기 |
| 실패 원인 판별 6b | 보류 | 오류 코드 실측, 워치독 밖 probe I/O의 행오버 위험 해소 |
| 공용 인덱스 5b | 예정 | 로컬 캐시 복사 배포 설계·검증 |
| DRM 저장 정책 | 사내 확인 대기 | 코드 변경이 아닌 정책 확인. [DESIGN.md](design/DESIGN.md) 참고 |
