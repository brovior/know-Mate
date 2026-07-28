# REVIEW-20260724-adr-0001-explicit-quit-for-tray-app-adr-0002-purge-projectio

| 생성일 | 모델 | 채널 | 리뷰 대상 |
|---|---|---|---|
| 2026-07-24 | gpt-5.6-sol | B(action) | `docs/ai-workflow/adr/ADR-0001-explicit-quit-for-tray-app.md`, `docs/ai-workflow/adr/ADR-0002-purge-projection-and-skip.md`, `docs/ai-workflow/architecture.md`, `docs/ai-workflow/requirements.md` |

> GPT 산출 원문(수정 금지). Claude의 판단은 하단 '처리 기록'으로만 추가한다 — reviews/README.md.

---

## 총평
두 ADR 모두 관측된 문제의 직접 원인을 비교적 명확히 설명하고 있으며, 명시적 Qt 종료와 LanceDB 컬럼 projection이라는 핵심 방향은 타당하다. 특히 종료 경로를 `_shutdown()`으로 수렴시키고 강제 종료를 최후 안전망으로 유지하는 결정에는 동의한다. 다만 purge 스킵 설계는 외부 불일치를 무기한 방치할 수 있고, 실패·dry-run·대량 삭제 차단 이후에도 서명을 갱신하면 필요한 purge가 영구적으로 재시도되지 않아 R-0002 FR-3을 위반한다. 또한 종료 설계는 R-0001 NFR-1의 state 저장 및 정상 정리 보장이 구체적인 단계와 테스트로 연결되지 않았다. 따라서 purge 상태 전이와 재검증 정책을 보완한 뒤 진행해야 한다.

**판정**: REQUEST_CHANGES

## 동의하는 결정
- `quitOnLastWindowClosed(False)`와 명시적 `QApplication.quit()`을 함께 사용하는 것은 창 가시성과 프로세스 수명주기를 분리하는 트레이 앱에 적합하며, 현재 증상에 대한 직접적인 해결책이다.
- 종료 처리를 `_shutdown()`이라는 단일 수렴점에 두고 기존 `stop_worker` 에스컬레이션을 유지한 결정은 정상 종료와 행오버 복구의 책임을 불필요하게 이원화하지 않는다.
- purge에서 `file_path`만 projection하는 결정은 벡터와 암호화 원문을 반복 로드하는 명백한 낭비를 제거하며, 보안상 불필요한 원문 취급 범위도 줄인다.
- projection과 스킵을 각각 검증하고 watch_folder 제거 회귀 테스트 및 RSS 실측을 포함한 검증 방향은 적절하다.

## 지적 사항

### [B-1] 조건부 스킵이 state-DB 불일치 복구를 무기한 중단할 수 있음 (Blocker)
- **관점**: 1. 요구사항 누락, 8. 장애 대응
- **지적**: `"watch_folders 서명 동일 && 처리 0건"`이면 purge를 계속 스킵하므로, 외부 요인으로 DB에만 남은 고아 경로는 다음 파일 또는 구성 변경이 영원히 없을 경우 복구되지 않는다. 문서의 표현처럼 단순히 복구가 “지연”되는 것이 아니라 무기한 방치될 수 있다.
- **근거**: R-0002 FR-3은 `state-DB 불일치 복구`를 동작 변화 없이 유지하도록 요구한다. ADR-0002와 A-0002는 복구 기회를 “구성 변경 또는 파일 변경이 있는 사이클”로 제한하지만, 유휴 상태가 지속되는 개인 PC에서는 그런 사이클이 발생한다는 보장이 없다.
- **대안**: 빠른 경로는 유지하되 주기적 강제 reconciliation을 추가한다. 예를 들어 마지막 성공 purge 이후 24시간 또는 N회 사이클이 지나면 처리 건수와 무관하게 projection purge를 실행한다. 더 강한 대안은 실패·비정상 종료 시 기록되는 `purge_dirty` 플래그를 두고, dirty 상태에서는 성공할 때까지 스킵하지 않는 것이다.

### [B-2] purge 성공 여부와 무관한 서명 갱신으로 재시도 경로가 사라짐 (Blocker)
- **관점**: 7. 동시성, 8. 장애 대응
- **지적**: A-0002 데이터 흐름은 purge 분기 후 무조건 `state["_watch_sig"] = watch_sig`를 수행한다. projection 조회나 삭제가 실패하거나, 대량 삭제 차단기가 작동하거나, dry-run으로 실제 삭제하지 않은 경우에도 서명이 갱신되면 다음 0건 사이클부터 purge가 스킵되어 미완료 작업이 재시도되지 않는다.
- **근거**: watch_folder 제거 직후의 첫 purge가 예외 또는 대량 삭제 차단으로 끝나면, 다음 사이클은 서명이 동일하고 처리 0건일 가능성이 높다. 또한 dry-run 상태에서 제거를 미리 확인한 뒤 자동 삭제를 켜더라도 `dry_run` 변경은 watch 서명에 포함되지 않으므로 실제 삭제가 실행되지 않을 수 있다. 이는 R-0002 FR-3의 제거·대량 삭제 차단·dry-run 동작 유지와 충돌한다.
- **대안**: `_watch_sig`를 단순 “직전 관측 서명”이 아니라 `_last_successfully_reconciled_watch_sig`로 정의하고, purge가 성공적으로 완료된 경우에만 원자적으로 갱신한다. `dry_run`, `max_delete_ratio` 등 결과에 영향을 주는 설정 변경도 별도 operation signature 또는 dirty 플래그에 반영해, dry-run 해제나 차단 정책 변경 시 반드시 다시 실행되게 한다. 실패·차단 시에는 원인과 pending 상태를 저장하고 다음 사이클에 재시도해야 한다.

### [M-1] 정상 종료의 데이터 무결성 보장이 설계 단계에 명시되지 않음 (Major)
- **관점**: 1. 요구사항 누락, 8. 장애 대응
- **지적**: 종료 흐름은 `scheduler.stop() → tray.hide() → stop_worker() → quit()`만 제시하며, R-0001 NFR-1이 요구한 state 저장 완료와 로그 flush가 어느 컴포넌트에서 어떻게 보장되는지 정의하지 않는다.
- **근거**: ADR-0001은 모든 종료에 `os._exit`을 사용하지 않는 이유로 state 저장과 로그 flush를 들지만, A-0001의 컴포넌트 책임·실패 모드·검증 계획에는 이를 확인하는 단계나 수용 테스트가 없다. `scheduler.stop()`이 단순 타이머 정지만 하는지, `stop_worker()`의 정상 경로가 진행 중 state 쓰기를 완료하는지는 전달된 문서만으로 확인할 수 없다.
- **대안**: 정상 종료 계약을 명시해 `신규 작업 차단 → 진행 중 작업 취소/완료 → state 원자적 저장 완료 → DB 핸들 정리 → 로그 flush → quit` 순서를 정의한다. 단위 테스트에는 `quit()`이 state 저장 완료 전에 호출되지 않는지와 앞 단계 예외 시에도 가능한 정리를 수행하는지 검증을 추가한다. 기존 코드가 이미 이를 보장한다면 해당 메서드와 근거를 A-0001에 연결하면 된다.

### [M-2] `index_state.json` 메타데이터 도입의 스키마와 마이그레이션이 미확정임 (Major)
- **관점**: 2. 숨은 가정, 3. 아키텍처 결함, 8. 장애 대응
- **지적**: 기존 파일 경로 엔트리와 같은 최상위 객체에 문자열 `_watch_sig`를 넣는 방안은 모든 기존 reader/writer가 비경로 엔트리와 비딕셔너리 값을 허용한다는 가정에 의존한다. A-0002도 “기존 로직이 경로 키만 순회하는지 확인 후 도입”이라고 남겨 핵심 저장 형식이 아직 확정되지 않았다.
- **근거**: 참조된 기존 state 예시는 최상위의 모든 값이 파일 상태 객체인 구조다. CleanupManager나 scanner가 모든 값을 `mtime`, `size`, `chunk_ids` 등을 가진 객체로 간주한다면 scalar 메타 키 추가로 런타임 오류가 발생할 수 있다. state 파일 저장 중 실패했을 때 서명과 파일 상태가 서로 다른 세대가 되는 문제도 다뤄지지 않았다.
- **대안**: 명시적인 버전형 스키마인 `{"schema_version": 2, "meta": {...}, "files": {...}}`로 전환하고 기존 평면 구조에서의 마이그레이션 및 롤백을 정의한다. 변경 범위가 과하면 별도 sidecar 파일에 purge 메타데이터를 원자적 교체 방식으로 저장할 수 있다. 최소한 모든 state 소비자가 예약 메타 키를 안전하게 건너뛰는지 테스트하고, 파일 상태와 reconciliation 상태의 커밋 순서를 정의해야 한다.

### [m-1] 서명 계산과 사이클 입력 스냅샷의 결정성이 부족함 (Minor)
- **관점**: 2. 숨은 가정, 7. 동시성
- **지적**: “정렬·정규화된 목록의 해시”만으로는 해시 알고리즘, 문자열 인코딩, 중복 경로 처리, Windows 경로의 대소문자·구분자·UNC 규칙이 확정되지 않는다. 또한 purge 대상 판정과 서명이 동일한 watch_folder 스냅샷에서 계산된다는 보장이 없다.
- **근거**: UI에서 사이클 도중 설정이 변경되거나, 서명 계산과 purge 호출이 서로 다른 설정 값을 읽으면 실제로 처리한 구성과 저장된 서명이 달라질 수 있다. Python 내장 `hash()`를 사용하면 프로세스 재시작마다 값이 달라져 불필요한 purge가 발생한다.
- **대안**: 사이클 시작 시 immutable snapshot을 만들고 서명 계산과 purge 모두 그 값을 사용한다. `normcase/normpath`에 준하는 Windows 규칙, 중복 제거, 정렬, UTF-8 직렬화 및 SHA-256 같은 프로세스 간 안정 해시를 명시한다.

### [m-2] 종료 검증이 실제 Qt 이벤트 루프 회귀를 충분히 포착하지 못함 (Minor)
- **관점**: 8. 장애 대응
- **지적**: 주입된 quit 콜백 스파이 테스트는 `_shutdown()`이 함수를 호출했다는 사실만 검증하며, `closeEvent` 분기와 실제 이벤트 루프 반환을 검증하지 못한다.
- **근거**: 이번 결함은 Qt의 창 가시성 및 이벤트 루프 동작에서 발생했다. PyQt6 비의존 단위 테스트만으로는 `_quit_app → closeEvent → _shutdown → app.exec() 반환` 연결이 다시 끊기는 회귀를 잡기 어렵다.
- **대안**: 기존 사외 단위 테스트에 더해 offscreen Qt 플랫폼에서 숨김/표시/`close_action=quit` 경로별 이벤트 루프 종료 통합 테스트를 추가한다. 환경 제약으로 불가능하면 최소한 `closeEvent` 분기 테스트와 실기 검증 절차를 릴리스 체크리스트로 고정한다.

## 확인 필요
- 실제 `MainWindow.closeEvent`, `_quit_app`, `_shutdown()` 구현에 재진입 방지와 단계별 독립 예외 처리가 이미 존재하는지 확인이 필요하다.
- 설치된 LanceDB 버전에서 `table.to_lance().to_table(columns=["file_path"])`가 공개·지원 API인지, 실제로 vector/text 컬럼을 읽지 않는 projection pushdown이 이루어지는지 실측이 필요하다.
- “이번 사이클 처리 0건”이 성공한 신규·변경 파일만 의미하는지, 삭제·실패·연기·취소·orphan 처리도 포함하는지 정의가 필요하다.
- `index_state.json`의 현재 저장이 원자적 교체 방식인지, 모든 state 소비자가 예약 메타 키를 안전하게 처리할 수 있는지 확인이 필요하다.

---

## 처리 기록 (Claude, 2026-07-24)

| ID | 판단 | 사유 / 반영 커밋 |
|---|---|---|
| B-1 | 수용 | 스킵 지속 시 불일치 무기한 방치 지적 타당 → `last_purge_ts` 기준 24h 강제 reconciliation 추가(0건이어도 실행). 메타 부재/손상 시 스킵 안 함(보수적) → 반영: PR #59 설계 보강 커밋 (A-0002·ADR-0002) |
| B-2 | 수용 | 무조건 서명 갱신은 재시도 경로 소멸 지적 타당 → 서명을 "성공적으로 reconcile된 op_sig"로 재정의, purge 예외 없이 완료 시에만 원자 갱신. `dry_run`·`max_delete_ratio`를 op_sig에 포함해 설정 변경 시 재실행 보장 → 반영: 동일 커밋 |
| M-1 | 수용 | A-0001에 "정상 종료 계약" 신설 — 신규 작업 차단(scheduler.stop) → 진행 작업 state 저장(취소 분기 save_state, tmp→replace 원자 교체는 기존 테스트로 보장) → COM 정리(run finally) → 로그 flush → quit 순서와 기존 코드 근거를 명시. quit이 stop_worker 반환 전에 불리지 않음을 단위 테스트 항목화 → 반영: 동일 커밋 |
| M-2 | 수용 | `index_state.json` 최상위 scalar 메타 키의 스키마 가정 위험 지적 타당 → **sidecar 파일**(`index_state.meta.json`, 원자 교체)로 변경. 기존 state 스키마·소비자 코드 무변경, 마이그레이션 불필요 → 반영: 동일 커밋 |
| m-1 | 수용 | 서명 규칙 확정: 사이클 시작 시 불변 스냅샷(normcase/normpath·구분자 통일·중복 제거·정렬) + UTF-8 직렬화 + SHA-256(내장 hash() 미사용 — 프로세스 간 안정). 서명 계산과 purge 판정이 동일 스냅샷 사용 → 반영: 동일 커밋 |
| m-2 | 수용(조건부) | `QT_QPA_PLATFORM=offscreen` 통합 테스트(숨김/표시/close_action=quit 3분기 이벤트 루프 종료)를 검증 계획에 추가. offscreen 불가 환경이면 closeEvent 분기 단위 테스트 + 실기 3경로를 릴리스 체크리스트로 고정 → 반영: 동일 커밋 |

**확인 필요 항목 답변**: ① `_shutdown`은 단계별 독립 try/except 기존재(main.py), 재진입 가드는
구현 시 `_really_quit` 플래그와 함께 점검. ② lancedb projection pushdown은 사내 실측 항목으로
검증 계획에 명시. ③ "처리 0건" = 소비자 루프가 꺼낸 태스크(성공·실패·연기 포함) 0건 && 미취소로
정의(A-0002). ④ `save_state`는 tmp→replace 원자 교체(기존 테스트 `test_atomic_save_uses_tmp_then_replace`).

**종결 판정**: Blocker 2건·Major 2건 모두 수용·설계 반영 완료 → 재리뷰(Action 재트리거) 통과 시
A-0001/A-0002 상태 Draft→Reviewed 승격 가능.
