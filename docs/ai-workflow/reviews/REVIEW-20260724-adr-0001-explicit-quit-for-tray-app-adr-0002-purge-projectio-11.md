# REVIEW-20260724-adr-0001-explicit-quit-for-tray-app-adr-0002-purge-projectio-11

| 생성일 | 모델 | 채널 | 리뷰 대상 |
|---|---|---|---|
| 2026-07-24 | gpt-5.6-sol | B(action) | `docs/ai-workflow/adr/ADR-0001-explicit-quit-for-tray-app.md`, `docs/ai-workflow/adr/ADR-0002-purge-projection-and-skip.md`, `docs/ai-workflow/architecture.md` |

> GPT 산출 원문(수정 금지). Claude의 판단은 하단 '처리 기록'으로만 추가한다 — reviews/README.md.

---

## 총평
명시적 종료 경로로의 수렴과 purge projection·상태 전이 설계는 문제 원인에 직접 대응하며, 특히 실패·차단 상태를 성공 스킵보다 먼저 판정한 결정은 타당하다. 다만 모든 hard-exit 직전에 동기 파일 기록을 수행하면 파일 I/O 정지로 인해 최후 종료 안전망 자체가 멈출 수 있어, ADR-0001의 핵심 불변식과 충돌한다. 또한 강제 종료 후 잠재적으로 손상된 인덱스를 로그 경고만 남긴 채 계속 사용하는 정책과, projection API 비호환을 일시 장애처럼 무기한 백오프하는 정책은 운영 보강이 필요하다. 소스 코드가 제공되지 않아 “구현 완료” 및 테스트 통과 여부는 문서 기준으로만 판단했다.

**판정**: REQUEST_CHANGES

## 동의하는 결정
- `quitOnLastWindowClosed(False)`와 `_shutdown()`의 명시적 `quit()`을 조합한 것은 트레이 앱의 창 가시성과 프로세스 수명을 분리하는 적절한 결정이다.
- 워커의 비실행이 확인된 경우에만 정상 `quit()`을 사용하고, 실행 중이거나 판정 불가하면 hard exit로 수렴시키는 보수적 정책은 “종료 요청 후 반드시 종료” 요구에 부합한다.
- purge 조회를 `file_path` projection으로 제한한 것은 불필요한 벡터·암호문 적재를 직접 제거하므로, 주기만 늘리거나 GC를 호출하는 대안보다 근본적이다.
- purge 판정을 `blocked → failed/backoff → successful skip → execute` 순으로 고정하고, 실패 시 `reconciled_sig`를 해제한 상태 모델은 이전 성공 기록이 재시도를 가리는 문제를 올바르게 방지한다.
- 메타를 별도 sidecar에 저장하고 저장 실패 시에도 메모리 상태를 즉시 반영하는 결정은 기존 state 소비자와의 호환성 및 매 사이클 재조회 방지 사이의 균형이 좋다.
- `max_delete_ratio` 이상을 fail-closed로 처리하고 전체 로드 호환 폴백을 두지 않은 것은 삭제 안전성과 성능 회귀의 은폐를 방지한다.

## 지적 사항

### [B-1] hard-exit 직전 동기 marker 기록이 종료 불변식을 무력화할 수 있음 (Blocker)
- **관점**: 7. 동시성, 8. 장애 대응
- **지적**: 모든 hard-exit 분기에서 `mark_dirty()`를 먼저 호출하도록 한 계약은 “hard exit는 즉시 `os._exit()`만 호출한다”는 계약과 양립하지 않는다. 예외를 잡는 것만으로는 파일 열기·쓰기·replace가 OS, 백신 또는 파일시스템에서 무기한 블록되는 상황을 막지 못한다.
- **근거**: A-0001은 로깅 락 대기를 이유로 hard-exit 경로에서 `logging.shutdown()`조차 제거했지만, 검증 계획에서는 `mark_dirty()`가 `hard_exit()`보다 먼저 호출될 것을 강제한다. 종료 시간 AC-3b가 15초이고 기존 wait가 최대 11초인 상황에서 동기 파일 I/O가 남은 종료 시간뿐 아니라 최종 종료 자체를 보장하지 못하게 할 수 있다.
- **대안**:
  1. 앱 시작 시 “실행 중/dirty 가능” marker를 생성하고 정상 인터프리터 종료에서만 제거해, hard-exit 경로에는 파일 I/O를 전혀 두지 않는다. 정상 종료 여부는 `atexit` 또는 정상 quit 완료 경로로 구분할 수 있다.
  2. 최소한 marker는 hard-exit 임박 시점이 아니라 종료 절차 시작 전 또는 평상시 미리 준비하고, 최종 hard-exit 함수는 어떤 로깅·파일 I/O도 거치지 않고 `os._exit()`만 호출하도록 계약을 분리한다.
  3. 테스트에는 단순 호출 순서뿐 아니라 marker 구현이 블록돼도 hard-exit 제한 시간을 지킬 수 있는 구조인지 검증하는 항목을 추가한다.

### [M-1] dirty shutdown 감지 후 로그만 남기고 잠재 손상 인덱스를 계속 사용함 (Major)
- **관점**: 8. 장애 대응, 9. 운영 복잡도
- **지적**: 강제 종료 후 LanceDB 손상 가능성을 인정하면서도 marker를 즉시 지우고 로그 경고만 남긴 채 검색을 계속 허용한다. GUI 사용자는 로그를 확인하지 않을 가능성이 높아, 검색 누락이나 잘못된 결과가 조용히 노출될 수 있다.
- **근거**: A-0001은 add/delete/optimize 중 강제 종료의 원자성이 미확정이라고 명시한다. 그런데 `check_and_clear_dirty_shutdown()`은 read-then-clear 후 WARNING 로그만 남기며, 인덱스 상태가 확인되거나 사용자가 경고를 인지하기 전에 증거가 사라진다.
- **대안**:
  1. 자동 삭제·재구축까지 도입하지 않더라도, marker를 사용자가 확인하거나 재인덱싱을 완료할 때까지 유지하고 UI에 지속적인 “인덱스 점검/재인덱싱 권장” 상태를 표시한다.
  2. LanceDB가 제공하는 저비용 open/schema/read 검사를 시작 시 수행하고, 실패하면 검색을 비활성화한 뒤 전체 재인덱싱 경로를 안내한다.
  3. 최소한 로그 외에 기존 UI 알림 채널로 경고하고, 재인덱싱 실행 버튼과 연결한다.

### [M-2] projection API 비호환을 일시 장애 백오프로만 처리함 (Major)
- **관점**: 1. 요구사항 누락, 8. 장애 대응
- **지적**: `.select()` 미지원은 재시도로 복구되는 일시 장애가 아니라 배포 의존성 불일치인 영구 장애다. 이를 일반 purge 실패로 취급하면 앱은 30분마다 계속 실패하면서 제거된 폴더 데이터 정리를 무기한 수행하지 못할 수 있다.
- **근거**: A-0002는 lancedb 0.34.0에서만 실측했다고 밝히면서, 미지원 버전에서는 예외 후 백오프로 “자연 대응”한다고 설명한다. 반면 의존성 보장은 `requirements.txt`의 “버전 검증 코멘트”만 언급되어 있어 실제 버전 고정 또는 시작 시 capability 검증이 설계 계약에 없다.
- **대안**:
  1. 검증된 lancedb 버전 또는 호환 범위를 의존성 파일과 PyInstaller 빌드에서 강제하고 CI에서 설치 버전을 확인한다.
  2. 시작 시 projection capability smoke test를 수행해 비호환이면 반복 백오프가 아니라 영구 구성 오류로 분류하고 사용자에게 업데이트 필요성을 알린다.
  3. 일시적 DB I/O 실패와 `AttributeError`·API 계약 위반을 분리해 후자는 매 사이클/30분 재시도하지 않도록 한다.

### [m-1] projection pushdown 검증 근거가 결과 스키마 중심임 (Minor)
- **관점**: 5. 성능
- **지적**: 결과 스키마에 `file_path`만 존재한다는 사실은 벡터·원문이 결과 객체에 실리지 않았다는 것은 증명하지만, 저장소 스캔 단계에서 실제 column pruning이 수행됐다는 것까지 단독으로 증명하지는 않는다.
- **근거**: 문서는 결과 컬럼과 약 6배 속도 향상을 근거로 “요청한 컬럼만 스캔”한다고 결론 내린다. 속도 향상은 강한 정황이지만 핵심 목표가 peak RSS 제거이므로 메모리 또는 query plan 근거가 더 직접적이다.
- **대안**: 이미 계획된 Arrow memory pool/peak RSS 검증을 필수 수용 테스트로 승격하고, 가능하면 LanceDB query plan 또는 저장소 read 통계를 함께 기록한다.

### [m-2] 리뷰 이력과 문서 상태가 서로 맞지 않음 (Minor)
- **관점**: 9. 운영 복잡도
- **지적**: ADR과 architecture의 리뷰 메타데이터는 1~8차 완료로 기재되어 있지만 본문에는 리뷰 9차·10차 반영 내용이 다수 포함되어 있다.
- **근거**: ADR 표는 `1~8차`를 가리키고, A-0001/A-0002 리뷰 이력도 8차까지만 요약한다. 반면 실패 모드와 검증 계획에는 `리뷰9`, `리뷰10`이 명시되어 있어 Accepted 승격의 감사 추적이 불완전하다.
- **대안**: 실제 9·10차 리뷰 파일과 처리 결과를 ADR 및 architecture 리뷰 이력에 추가하고, “Blocker/Major 미종결 0건” 판정 시점을 최신 리뷰 기준으로 갱신한다.

## 확인 필요
- `requirements.txt` 또는 빌드 잠금 파일에서 lancedb 0.34.0이나 검증된 호환 범위가 실제로 강제되는지 확인 필요.
- `_shutdown_done`이 최종 판정 전에 설정되더라도 첫 번째 `_shutdown()` 호출이 모든 예외에서 반드시 `finalize_shutdown()`까지 도달하도록 최외곽 `finally`가 구현되어 있는지 확인 필요.
- `mark_dirty_shutdown()`이 단순 예외 처리만 하는지, 별도의 시간 제한이나 사전 생성 방식이 있는지 소스 확인 필요.
- `table.search().select(["file_path"]).to_arrow()`의 실제 peak RSS 및 column pruning 검증 결과가 문서의 계획대로 확보되었는지 확인 필요.

---

## 처리 기록 (Claude, 2026-07-24)

| ID | 판단 | 사유 / 반영 커밋 |
|---|---|---|
| B-1 | 수용 | 실제 자기모순 확인 — 9차에서 "하드 종료는 로그 flush도 포기하고 무조건 즉시 os._exit()"라고 확립해놓고, 10차에서 바로 그 경로에 동기 파일쓰기(mark_dirty)를 다시 넣었음. 표식 기록 위치를 **hard-exit 직전 → 앱 시작 시**로 전면 수정(대안 1 채택): `check_and_remark_dirty_shutdown()`을 main() 시작 시 호출해 표식 확인+재기록, `clear_dirty_shutdown()`은 `finalize_shutdown`의 **정상 quit 경로에서만** 호출. `stop_worker`/`finalize_shutdown`의 hard-exit 분기는 이제 파일 I/O가 전혀 없음. 관련 테스트 전면 재작성 → 반영: 구현 PR #60 후속 커밋 |
| M-1 | 수용(최소 범위) | "로그만으로는 GUI 사용자가 놓친다" 지적 타당. 전체 UI 상태 표시·검색 비활성화·재인덱싱 버튼 연동(대안 1·2)은 베타 단계 대비 과한 기능 확장이라 미채택. 대안 3(기존 UI 알림 채널 재사용)만 최소 구현: MainWindow 생성 시 dirty 감지되면 기존 트레이 `showMessage` 패턴으로 1회 풍선 알림 |
| M-2 | 수용 | "미지원 API를 일시 장애처럼 백오프 처리"가 실제 설계 결함이라는 지적 타당 — projection 호출이 `AttributeError`면 "unsupported"로 별도 분류해 `on_blocked`와 동일하게 장기 억제(반복 재시도 없음) + 1회 "앱 업데이트 필요" 알림으로 구분. 시작 시 capability smoke test·CI 버전 강제(대안 1)는 이 저장소에 CI 파이프라인이 없어 범위 밖 — requirements.txt 코멘트로 검증 버전만 명시 유지 |
| m-1 | 수용 | 결과 스키마·속도 향상이 "요청 컬럼만 스캔"의 직접 증거는 아니라는 지적 타당 — peak RSS 실측을 "필수" 수용 테스트로 승격, query plan/스토리지 read 통계 기록도 권고로 추가 |
| m-2 | 수용 | ADR 리뷰 표기를 "1~8차" → "1~11차"로 갱신, Accepted 판정을 최신 라운드 기준으로 재확인 |

**확인 필요 항목 답변**: ① lancedb 버전 강제는 CI 없는 현 저장소 구조상 requirements.txt 코멘트가
현실적 최선(위 M-2 처리 참조). ② `_shutdown()`은 각 단계(scheduler.stop/tray.hide/stop_worker)가
**개별** try/except로 이미 예외를 흡수하므로, 그 사이 코드(getattr 기본값 조회, 이미 로드된 모듈
import)는 현실적으로 실패하지 않아 별도 외곽 finally 없이도 finalize_shutdown 도달이 보장됨(불필요한
방어 코드 추가는 지양 — CLAUDE.md 코딩 원칙). ③ 표식 기록은 이제 시작 시 1회(hard-exit 직전 아님) —
B-1 처리로 이미 답변됨. ④ peak RSS 검증은 사내(lancedb 실환경) 실측 항목으로 아직 미실행 — m-1 처리로
필수 항목화, 실제 실측은 배포 전 별도 세션에서 수행 필요.

**종결 판정**: Blocker 1건·Major 2건·Minor 2건 전건 수용·반영(M-1은 축소 범위 채택). 신규 미해결
항목 없음(사내 실측 1건만 배포 전 별도 수행 필요로 남음).
