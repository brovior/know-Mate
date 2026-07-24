# REVIEW-20260724-adr-0001-explicit-quit-for-tray-app-adr-0002-purge-projectio-14

| 생성일 | 모델 | 채널 | 리뷰 대상 |
|---|---|---|---|
| 2026-07-24 | gpt-5.6-sol | B(action) | `docs/ai-workflow/adr/ADR-0001-explicit-quit-for-tray-app.md`, `docs/ai-workflow/adr/ADR-0002-purge-projection-and-skip.md`, `docs/ai-workflow/architecture.md` |

> GPT 산출 원문(수정 금지). Claude의 판단은 하단 '처리 기록'으로만 추가한다 — reviews/README.md.

---

## 총평
두 ADR 모두 문제 원인을 구체적으로 식별하고, 종료 경로 수렴과 purge projection 도입이라는 핵심 방향도 타당하다. 특히 예외 시 fail-safe 종료, purge 상태 전이 순서, sidecar 원자 교체 등은 반복 리뷰 결과가 잘 반영되어 있다. 다만 unsupported 상태가 `op_sig`에 종속되어 앱 업데이트 후에도 복구되지 않을 수 있고, dirty-shutdown 표식의 daemon 비동기 삭제는 정상 종료에서도 완료가 보장되지 않는다. 또한 purge 메모리 수용시험의 warm-up이 측정 대상 메모리를 baseline에 포함해 거짓 합격을 만들 수 있으므로, Accepted 상태를 유지하기 전에 이 세 항목을 수정하는 것이 안전하다.

**판정**: REQUEST_CHANGES

## 동의하는 결정
- `quitOnLastWindowClosed(False)`와 모든 실제 종료 경로의 `_shutdown()` 수렴은 트레이 앱의 수명주기를 명시적으로 만드는 적절한 결정이다. 워커 상태를 확인할 수 없을 때도 `quit()`에 낙관적으로 의존하지 않고 `hard_exit`로 종료 확실성을 우선한 정책도 문서의 최상위 요구와 일관된다.
- hard-exit 직전 동기 파일 I/O와 `logging.shutdown()`을 제거한 결정에 동의한다. 종료 최후 안전망 앞에 잠재적 무한 블로킹 연산을 두지 않는 것이 맞다.
- purge에서 `file_path`만 projection하고, 차단·백오프 판정을 성공 스킵보다 먼저 수행하며, 실패 시 `reconciled_sig`를 해제한 설계는 성능과 재시도 정확성을 함께 개선한다.
- 기존 `index_state.json` 스키마를 변경하지 않고 sidecar를 사용하며, 손상·유실 시 스킵하지 않는 보수적 동작을 택한 것도 마이그레이션 위험을 낮추는 합리적인 선택이다.

## 지적 사항

### [M-1] unsupported 억제가 앱 업데이트 후에도 해제되지 않을 수 있음 (Major)
- **관점**: 1. 요구사항 누락, 7. 동시성, 8. 장애 대응
- **지적**: projection API 미지원 상태를 동일 `op_sig`에 대해 장기 억제하면서, `op_sig`에는 런타임 또는 LanceDB 버전이 포함되지 않는다. 따라서 안내대로 앱을 업데이트해 호환 버전을 설치해도 watch folders·`dry_run`·차단율이 같으면 억제가 계속될 수 있다.
- **근거**: ADR-0002는 `op_sig`를 폴더 스냅샷, `dry_run`, `max_delete_ratio`, 스키마 버전으로 정의한다. 반면 실패 모드는 `AttributeError`를 `"unsupported"`로 분류해 동일 `op_sig`에서 반복 실행하지 않고 “앱 업데이트 필요”를 안내한다고 한다. 앱 또는 LanceDB 업데이트 자체는 현재 정의된 `op_sig`를 변경하지 않으므로, 사용자에게 제시한 복구 조치가 실제 상태 전이를 일으키지 않는다. 또한 컴포넌트 표와 데이터 흐름에는 `unsupported_sig`가 없고 `blocked_sig`만 있어, 두 상태가 어떻게 구별·해제되는지도 불명확하다. 이는 “24시간 상한으로 무기한 방치되지 않는다”는 결과 서술과도 충돌한다.
- **대안**:
  1. `unsupported_sig`를 별도 필드로 두고, 앱 버전·LanceDB 버전·projection 구현 버전으로 만든 capability fingerprint가 변경되면 자동 해제하고 1회 재검증한다.
  2. 또는 dependency/capability fingerprint를 `op_sig`에 포함하되, 대량삭제 차단과 unsupported의 UI 메시지 및 상태를 별도로 유지한다.
  3. `AttributeError` 전체를 미지원으로 간주하지 말고, 시작 시 `select`의 존재와 호출 가능성을 명시적으로 검사하거나 정확한 API 호출 지점에서만 미지원으로 변환한다. 내부 구현 버그에서 발생한 `AttributeError`는 일반 오류로 관측되게 해야 한다.
  4. 문서의 “최대 24시간” 보장에 unsupported가 예외라면 이를 ADR 결과와 architecture의 강제 reconciliation 설명에 명시한다.

### [M-2] daemon 스레드에 맡긴 표식 삭제는 정상 종료에서도 완료가 보장되지 않음 (Major)
- **관점**: 7. 동시성, 8. 장애 대응
- **지적**: `app.exec()` 반환 후 `clear_dirty_shutdown()`이 삭제를 daemon 스레드에 위임하고 즉시 반환하면, 곧바로 `main()`과 인터프리터가 종료되면서 삭제 스레드가 실행되기 전 또는 `unlink()` 완료 전에 중단될 수 있다.
- **근거**: A-0001은 daemon 스레드가 인터프리터 종료를 막지 않는다는 사실을 정상 종료 계약의 근거로 사용한다. 같은 성질 때문에 표식 삭제 daemon도 완료를 보장하지 않는다. 현재 검증 계획은 “비동기 즉시 반환”만 확인하며, 정상 종료 후 실제 flag가 없어지는지 또는 다음 시작에서 dirty로 오탐하지 않는지를 검증하지 않는다. 이는 단순한 드문 파일 삭제 실패와 달리 프로세스 종료와 스레드 스케줄링 사이의 구조적인 경쟁 조건이다.
- **대안**:
  1. `app.exec()` 반환 후 삭제 스레드를 시작하고 짧은 제한 시간 동안 `join`한다. 제한 안에 완료되지 않으면 종료를 계속하되 best-effort 계약과 false-positive 가능성을 명시한다.
  2. 종료 프로세스와 독립적으로 완료할 수 있는 소형 helper 프로세스에 삭제를 위임한다. 다만 이 앱 규모에서는 운영 복잡도가 커질 수 있으므로 필요성을 먼저 평가해야 한다.
  3. Windows에서 블로킹 위험을 수용할 수 있다는 실측 근거가 있으면 로컬 `%APPDATA%` 파일을 동기 삭제하되, 종료 시간 수용 기준으로 검증한다.
  4. 어떤 방식을 택하든 “정상 `app.exec()` 반환 → 프로세스 종료 → 다음 실행에서 dirty 아님”을 실제 프로세스 통합 테스트로 추가한다. 삭제를 완전히 보장할 수 없다면 표식은 “확정된 강제 종료”가 아니라 “이전 세션이 정상 종료되지 않았을 가능성”을 나타낸다고 UI 문구도 조정한다.

### [M-3] 메모리 측정 전 full-query warm-up이 추가 할당량을 baseline에 흡수함 (Major)
- **관점**: 4. 확장성, 5. 성능
- **지적**: 독립 프로세스마다 “DB open + 쿼리 1회 warm-up” 후 baseline RSS를 측정하면, warm-up 쿼리가 projection/Arrow 순회에 필요한 버퍼와 Python allocator arena를 이미 확보해 baseline에 포함할 수 있다. 이후 동일 작업의 `max RSS - baseline`이 작게 나와 실제 첫 purge 메모리 비용을 과소평가할 가능성이 크다.
- **근거**: ADR-0002의 문제 원인 자체가 CPython/pyarrow가 해제한 힙을 OS에 즉시 반환하지 않아 RSS가 최고점에 남는다는 것이다. 따라서 측정 대상과 같은 full projection 쿼리를 baseline 전에 수행하는 것은 이 설계가 지적한 메모리 잔류 특성 때문에 구조적으로 거짓 합격을 만들 수 있다. 50ms RSS 표본 역시 짧은 peak를 놓칠 수 있다.
- **대안**:
  1. DB open과 메타데이터 접근까지만 warm-up하고, 전체 projection/Arrow 순회는 baseline 이후 프로세스 최초 1회로 측정한다.
  2. 캐시 효과도 확인해야 한다면 cold-run과 warm-run을 별도 지표로 측정하되, NFR 합격 판정은 사용자 세션의 첫 강제 reconciliation을 대표하는 cold-run 기준으로 한다.
  3. Windows의 `PeakWorkingSetSize` 등 OS가 누적하는 프로세스 peak 카운터를 사용해 50ms 표본 사이의 peak도 포착한다. 독립 프로세스 방식은 그대로 유지할 수 있다.
  4. 테스트 절차에 warm-up 쿼리의 정확한 범위를 명시하여 구현자가 실수로 측정 대상 전체를 미리 실행하지 않게 한다.

### [m-1] Accepted 상태의 리뷰 이력 범위가 문서 내부에서 불일치함 (Minor)
- **관점**: 8. 장애 대응, 9. 운영 복잡도
- **지적**: ADR 헤더는 1~13차 리뷰와 구현 반영을 Accepted 근거로 제시하지만, architecture의 각 블록 리뷰 이력은 1~8차까지만 설명하면서 Blocker/Major 미종결 0건이라고 기록한다.
- **근거**: architecture 본문에는 리뷰 9~13차에서 반영된 변경이 다수 명시되어 있으므로, 하단 리뷰 이력만 보면 어떤 리뷰가 최종 승인 근거인지 추적하기 어렵다.
- **대안**: A-0001과 A-0002의 리뷰 이력을 9~13차까지 갱신하고, 각 차수의 주요 수용·보류 결과와 미종결 건수를 짧게 정리한다.

## 확인 필요
- 소스 코드가 제공되지 않아 `_shutdown_done` 설정 시점, 각 단계의 독립 예외 처리, `quit()`/`hard_exit` 상호배타성, sidecar 메모리 캐시 전이, projection 결과 전건 반환 등 “구현 완료” 주장은 확인할 수 없다.
- `unsupported`가 실제 코드에서 `blocked_sig`를 재사용하는지 별도 필드를 사용하는지, 그리고 앱/LanceDB 업데이트 시 해당 상태를 초기화하는 별도 로직이 있는지 확인이 필요하다.
- 메모리 측정 절차의 “쿼리 1회 warm-up”이 전체 projection 쿼리인지 단순 연결·메타데이터 쿼리인지 확인이 필요하다. 전체 projection이 아니라면 M-3의 범위는 측정 절차 문언 명확화로 축소할 수 있다.

## 처리 기록 (중립 검토)

| ID | 판단 | 사유/반영 |
|---|---|---|
| M-1 | 수용 | unsupported 억제 해제 판정을 op_sig에서 `compute_capability_sig()`(lancedb 버전 지문)로 전환. `PurgeMeta.blocked_capability_sig` 필드 추가, `on_blocked(..., capability_sig=)`로 기록, `decide(..., capability_sig=)`가 `blocked_reason == "unsupported"`일 때만 이 값으로 판정(불명/변경 시 억제 해제 → 안전한 방향). `scheduler.py`가 매 사이클 `purge_meta.compute_capability_sig()`를 계산해 `decide`/`on_blocked`/알림 1회 판정에 일관되게 사용하도록 배선. 리뷰가 제시한 대안 3("AttributeError 전체를 미지원으로 간주하지 않고 정확한 API 호출 지점에서만 변환")은 이미 구현대로였음(`table.search().select()` 호출부에서만 `AttributeError`를 잡음) — 별도 조치 불필요. `knowmate/collector/purge_meta.py`, `knowmate/collector/scheduler.py`. |
| M-2 | 수용 | daemon 스레드 삭제에 최대 1초(`_CLEAR_DIRTY_JOIN_TIMEOUT_SEC`) `join()` 추가 — 리뷰13 M-1로 호출 위치가 `app.exec()` 정상 반환 후로 이미 이동했으므로(이벤트 루프가 끝난 뒤), 짧은 상한 대기가 "종료는 반드시 된다" 불변식을 재위협하지 않는다고 판단해 리뷰가 제시한 대안 1(짧은 제한시간 join)을 채택. helper 프로세스 위임(대안 2)은 이 앱 규모에 과한 운영 복잡도라 기각. `knowmate/app/lifecycle.py`. |
| M-3 | 수용 | 성능 수용 절차의 warm-up 범위를 "DB open + count_rows() 등 메타데이터 조회"로 명시적으로 제한하고, 측정 대상인 전체 projection 쿼리는 baseline 확정 후 프로세스 최초 1회로만 실행하도록 `architecture.md`를 재작성 — 지적대로 동일 쿼리를 warm-up에서 먼저 돌리면 pyarrow의 미반환 힙이 baseline에 흡수돼 거짓 합격 가능성이 있었음을 인정. |
| m-1 | 수용 | ADR 헤더(1~14차)와 architecture.md 리뷰 이력을 9~14차까지 갱신, 각 라운드 핵심 반영 사항을 요약해 최종 승인 근거 추적성을 회복. |

**종결 판정**: M-1·M-2·M-3(Major 3건) 모두 실질적 결함으로 확인돼 수정 반영. m-1도 수용. Blocker/Major 잔존 0건.
