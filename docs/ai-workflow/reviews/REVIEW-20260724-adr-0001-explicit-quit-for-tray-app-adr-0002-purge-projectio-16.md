# REVIEW-20260724-adr-0001-explicit-quit-for-tray-app-adr-0002-purge-projectio-16

| 생성일 | 모델 | 채널 | 리뷰 대상 |
|---|---|---|---|
| 2026-07-24 | gpt-5.6-sol | B(action) | `docs/ai-workflow/adr/ADR-0001-explicit-quit-for-tray-app.md`, `docs/ai-workflow/adr/ADR-0002-purge-projection-and-skip.md`, `docs/ai-workflow/architecture.md` |

> GPT 산출 원문(수정 금지). Claude의 판단은 하단 '처리 기록'으로만 추가한다 — reviews/README.md.

---

## 총평
두 ADR의 핵심 방향인 명시적 종료와 컬럼 projection은 문제 원인에 직접 대응하며, 실패 경로와 운영상 안전장치도 상당히 구체적으로 설계되어 있다. 특히 hard-exit 경로에서 블로킹 가능 파일 I/O를 제거한 결정과 purge 메타를 보수적으로 처리한 결정은 타당하다. 다만 unsupported 상태 해제 후 기존 성공 메타가 재검증을 가로막는 상태 전이 결함과, 종료 안전성의 핵심인 `stop_worker()` 계약에 문서 간 모순이 남아 있다. 영구 억제 오분류를 막기 위한 `AttributeError` 처리 범위도 명시해야 하므로 수정 후 승인하는 것이 적절하다.

**판정**: REQUEST_CHANGES

## 동의하는 결정
- `setQuitOnLastWindowClosed(False)`와 `_shutdown()`의 명시적 `quit()`을 결합한 것은 트레이 앱의 창 가시성과 프로세스 수명을 분리하는 타당한 해결책이다.
- `terminate()`를 한 번이라도 사용했다면 `isRunning()==False`여도 정상 종료로 간주하지 않고 `hard_exit`로 보내는 정책은 QThread 강제 중단의 락·데이터 무결성 위험을 올바르게 반영한다.
- hard-exit 직전 동기 파일 I/O를 없애고, dirty marker를 시작 시 선기록한 뒤 `app.exec()` 정상 반환 후에만 해제하는 구조는 “최후 안전망이 블록되면 안 된다”는 불변식과 잘 맞는다.
- purge에서 `file_path`만 projection하고 Arrow를 직접 순회하는 것은 불필요한 벡터·암호문 로드를 제거하는 직접적인 성능 개선이다.
- purge 메타를 기존 state와 분리하고, 손상·유실 시 스킵하지 않고 재실행하는 보수적 정책은 마이그레이션 및 장애 복구 측면에서 적절하다.

## 지적 사항

### [M-1] `stop_worker()` 변경 계약에 대한 문서 간 모순 (Major)
- **관점**: 1. 요구사항 누락, 3. 아키텍처 결함
- **지적**: A-0001 컴포넌트 표는 `lifecycle.stop_worker()`를 “기존 유지, 변경 없음”이라고 설명하지만, ADR-0001과 A-0001 실패 모드는 `terminate()` 사용 여부를 bool로 반환하도록 계약을 변경했다고 명시한다.
- **근거**: 이 반환값은 `_shutdown()`이 `force_hard_exit=True`를 전달하는 근거이며, 누락되면 `terminate()` 성공 후 `isRunning()==False`인 워커가 정상 종료로 오분류되는 리뷰15 B-1 결함이 재발한다. 단순한 설명 차이가 아니라 종료 안전성에 영향을 주는 public contract 변경이다.
- **대안**: 컴포넌트 표를 “기존 에스컬레이션 순서는 유지하되, `terminate()` 사용 여부를 bool로 반환하도록 계약 변경”으로 정정하고 반환값 의미를 명시한다. 가능하면 `StopWorkerResult` 같은 명시적 결과 타입을 사용해 bool 의미가 뒤집히는 실수를 방지한다.

### [M-2] unsupported 해제 후 기존 성공 메타가 capability 재검증을 지연시킴 (Major)
- **관점**: 7. 동시성, 8. 장애 대응
- **지적**: unsupported 상태에서 `capability_sig`가 변경되어 억제가 해제되어도, 동일한 `op_sig`의 기존 `reconciled_sig`와 최근 `last_purge_ts`가 남아 있으면 판정 3의 성공 스킵이 성립해 projection API를 즉시 재검증하지 않는다.
- **근거**: 데이터 흐름에서 unsupported 전이는 `blocked_sig`, `blocked_reason`, `blocked_capability_sig`만 설정하고 `reconciled_sig`를 해제한다고 명시하지 않는다. 이후 capability가 변경되면 판정 0은 “아래로 진행”하지만, 판정 3은 blocked 상태를 배제하지 않으므로 기존 성공 시각 기준 최대 24시간 동안 다시 스킵할 수 있다. 이는 “capability_sig 변화로만 재검증” 및 업데이트 후 억제가 풀린다는 설명과 맞지 않는다.
- **대안**: 다음 중 하나를 채택한다.
  1. unsupported 기록 시 `reconciled_sig`와 `last_purge_ts`를 무효화한다.
  2. 판정 0에서 capability 변화가 감지되면 `force_execute=True`로 두고 해당 사이클에서는 판정 1~3을 건너뛴다.
  3. 상태 전이를 명시적 상태 머신으로 만들고 `UNSUPPORTED(capability=A) → capability 변경 → PROBE_REQUIRED` 전이를 정의한다.  
  두 번째 방식이 기존 성공 메타를 보존하면서도 즉시 재검증할 수 있어 가장 명확하다.

### [M-3] 모든 `AttributeError`를 unsupported로 오분류할 위험 (Major)
- **관점**: 8. 장애 대응
- **지적**: 문서는 projection API 비호환을 `AttributeError`로 분류한다고만 정의하며, 예외를 잡는 정확한 범위를 규정하지 않는다.
- **근거**: `_purge_removed_folders()` 전체나 projection 결과 처리까지 넓게 `except AttributeError`로 감싸면 LanceDB 내부 결함, 테스트 더블 오류 또는 Arrow 순회 코드의 프로그래밍 오류도 영구 unsupported로 기록될 수 있다. 그러면 30분 재시도와 24시간 reconciliation 모두 적용되지 않고 capability가 바뀔 때까지 purge가 억제된다.
- **대안**: `.search()` 및 `.select` capability 확인 부분만 좁은 try 블록으로 감싼다. 예를 들어 `select = getattr(query, "select", None)` 및 `callable(select)`를 확인해 API 부재만 unsupported로 분류하고, `select(...).to_arrow()` 실행 중 발생하는 다른 예외는 일시 실패 또는 예상하지 못한 결함으로 별도 처리한다. 지원 여부를 시작 시 한 번 확인하는 capability probe도 대안이다.

### [m-1] 독립 프로세스 성능 시험의 DB 격리 조건이 빠져 있음 (Minor)
- **관점**: 5. 성능, 7. 동시성
- **지적**: 5개의 독립 프로세스가 각각 실행하는 end-to-end 측정에 삭제와 `optimize()`가 포함되지만, 각 프로세스가 동일한 초기 DB 복제본을 사용하는지 명시되어 있지 않다.
- **근거**: 같은 DB를 순차 재사용하면 첫 실행이 제거 대상을 삭제하거나 저장 구조를 최적화하여 이후 실행의 행 수, 삭제 후보 및 메모리 비용이 달라진다. 같은 DB에 병렬 접근하면 단일 writer 및 파일 락 조건과도 충돌할 수 있다.
- **대안**: 측정 프로세스마다 동일한 seed DB를 별도 디렉터리로 복제하고 단독 실행하도록 절차에 명시한다. 각 실행 전 행 수·스키마·제거 대상 수의 해시 또는 카운트를 확인해 동일한 초기 조건도 기록한다.

### [N-1] 리뷰 이력에 15차 반영 기록이 누락됨 (Nit)
- **관점**: 9. 운영 복잡도
- **지적**: ADR 상태 표와 본문은 리뷰15 변경을 명시하지만 architecture.md의 리뷰 이력은 14차까지만 열거한다.
- **근거**: `terminate()` 사용 시 hard-exit 강제와 capability signature 확장은 안전성에 중요한 변경이므로 추적 이력에서 누락되면 Accepted 근거를 확인하기 어렵다.
- **대안**: A-0001과 A-0002 리뷰 이력에 15차 검토 및 반영 내용을 추가한다.

## 확인 필요
- `requirements.md`가 제공되지 않아 R-0001/R-0002의 전체 요구사항 및 AC/NFR 대응 완전성은 확인할 수 없다.
- 소스 코드가 제공되지 않아 `_shutdown()`의 최종 판정 도달 보장, `stop_worker()` 반환값 전달, unsupported 예외 처리 범위, 메타 상태 전이 및 테스트 구현 여부는 확인이 필요하다.
- `table.search().select(["file_path"]).to_arrow()`가 실제 저장소 단계에서 컬럼 pruning을 수행하는지는 문서의 필수 성능 수용 시험 결과로 최종 확인해야 한다.

## 처리 기록 (중립 검토)

| ID | 판단 | 사유/반영 |
|---|---|---|
| M-1 | 수용 | A-0001 컴포넌트 표의 `lifecycle.stop_worker()` 설명을 "기존 유지, 변경 없음"에서 "에스컬레이션 순서는 유지하되 반환값 계약이 변경됨(terminate() 사용 여부 bool 반환)"으로 정정, ADR-0001 결정문에도 동일 계약을 명시. `StopWorkerResult` 같은 전용 타입 도입은 현재 이 값을 소비하는 곳이 `finalize_shutdown` 1곳뿐이라 과설계로 판단해 기각(bool 반환 유지, docstring으로 의미 고정). `docs/ai-workflow/architecture.md`, `docs/ai-workflow/adr/ADR-0001-explicit-quit-for-tray-app.md`. |
| M-2 | 수용(코드는 이미 정확, 문서·회귀 테스트 보강) | 실제 `on_blocked()`는 unsupported 전이에서도 항상 `reconciled_sig=None`으로 새 `PurgeMeta`를 반환해 지적된 시나리오가 이미 발생하지 않음을 재확인(수동 검증 + 신규 회귀 테스트 2건 추가). 다만 architecture.md의 unsupported 전이 의사코드에 `reconciled_sig` 해제가 누락돼 있어 문서만 보강 — 코드-문서 불일치를 인정. `docs/ai-workflow/architecture.md`, `knowmate/tests/test_purge_meta.py`. |
| M-3 | 수용 | `_purge_removed_folders`의 unsupported 판정을 `.search().select().to_arrow()` 호출 체인 전체를 감싸던 넓은 `except AttributeError`에서, `getattr`+`callable`로 메서드 존재 자체만 확인하는 좁은 capability probe로 변경. `select(...).to_arrow()` 실행 중 예외는 "failed"(일시적)로 분류하도록 분리, 기존 테스트를 시나리오에 맞게 수정하고 "존재하지만 예외" 케이스에 대한 신규 테스트를 추가. `knowmate/collector/scheduler.py`, `knowmate/tests/test_phase3.py`. |
| m-1 | 수용 | 성능 수용 절차에 "5개 측정 프로세스는 동일 seed DB를 각자 별도 디렉터리로 복제해 단독 실행, 실행 전 행 수·스키마·제거 대상 수 확인"을 명시. `docs/ai-workflow/architecture.md`. |
| N-1 | 수용 | A-0001/A-0002 리뷰 이력에 15·16차 반영 내용 추가. `docs/ai-workflow/architecture.md`. |

**종결 판정**: M-1·M-2·M-3(Major 3건) 모두 검토해 반영(M-2는 코드가 이미 정확했음을 확인하고 문서·테스트만 보강). m-1·N-1도 수용. Blocker/Major 잔존 0건.
