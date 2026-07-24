# REVIEW-20260724-adr-0001-explicit-quit-for-tray-app-adr-0002-purge-projectio-17

| 생성일 | 모델 | 채널 | 리뷰 대상 |
|---|---|---|---|
| 2026-07-24 | gpt-5.6-sol | B(action) | `docs/ai-workflow/adr/ADR-0001-explicit-quit-for-tray-app.md`, `docs/ai-workflow/adr/ADR-0002-purge-projection-and-skip.md`, `docs/ai-workflow/architecture.md` |

> GPT 산출 원문(수정 금지). Claude의 판단은 하단 '처리 기록'으로만 추가한다 — reviews/README.md.

---

## 총평
두 ADR 모두 문제의 직접 원인을 구체적으로 식별하고, 정상 경로와 강제 종료·실패·억제 경로를 분리해 설계한 점이 타당하다. 특히 A-0001의 `terminate()` 사용 이력을 정상 종료와 구분한 결정과 A-0002의 projection, 보수적 메타 처리 및 capability 기반 영구 장애 억제는 이전 결함을 실질적으로 해소한다. 요구사항을 뒤집거나 재설계를 요구할 Blocker/Major는 발견되지 않았다. 다만 종료의 “정확히 한 번” 보장은 `hard_exit`의 비복귀 계약에 의존하므로 이를 인터페이스에 명시해야 하며, 성능 수용 기준도 사후 해석이 불가능하도록 고정할 필요가 있다.

**판정**: APPROVE_WITH_CHANGES

## 동의하는 결정
- `quitOnLastWindowClosed(False)`와 모든 실제 종료 경로의 `_shutdown()` 수렴은 트레이 앱의 창 가시성과 프로세스 수명을 분리하는 적절한 설계다.
- `QThread.terminate()`가 사용된 경우 `isRunning()==False`여도 정상 종료로 보지 않고 즉시 `hard_exit`하도록 한 결정에 동의한다. 임의 지점에서 중단된 스레드가 락을 보유할 가능성을 반영한 보수적인 판단이다.
- dirty-shutdown marker를 hard-exit 직전에 쓰지 않고 앱 시작 시 미리 기록하며, `app.exec()` 정상 반환 후에만 해제하는 구조는 하드 종료 경로의 무블로킹 불변식과 탐지 정확성을 함께 만족시킨다.
- purge에서 벡터·암호문을 제외한 `file_path` projection을 사용하고, 정상 스킵에도 24시간 reconciliation 상한을 둔 것은 성능과 자가 복구 기회의 균형이 적절하다.
- 실패·차단 메타를 sidecar 저장 성공 여부와 무관하게 메모리에 먼저 반영하고, 재시작 시 보수적으로 재실행하는 방식은 단일 인스턴스·멱등 purge라는 전제 아래 합리적이다.
- unsupported 판정을 호출 체인 전체의 `AttributeError`가 아니라 메서드 존재 여부로 좁힌 것은 내부 결함을 영구 장애로 오분류하는 위험을 줄인다.

## 지적 사항

### [m-1] hard_exit 비복귀 계약이 인터페이스에 명시되지 않음 (Minor)
- **관점**: 3. 아키텍처 결함, 7. 동시성
- **지적**: 문서는 quit과 hard_exit가 프로세스 수명 동안 정확히 하나만 호출된다고 규정하지만, `stop_worker()`와 `finalize_shutdown()` 양쪽 모두 hard_exit를 호출할 수 있다. 이 보장은 첫 hard_exit가 절대 반환하지 않는다는 암묵적 전제에 의존한다.
- **근거**: A-0001은 `stop_worker`가 정상 대기→`terminate()`→`os._exit()`까지 에스컬레이션한다고 하면서, `_shutdown()`도 이후 `finalize_shutdown(force_hard_exit=...)`을 호출한다고 설명한다. 운영 구현이 `os._exit`이면 반환하지 않지만, 주입된 테스트 더블이나 향후 래퍼가 반환하면 hard_exit가 두 번 관측될 수 있어 “정확히 하나”라는 계약이 함수 구조만으로는 성립하지 않는다.
- **대안**: `hard_exit` 타입과 문서 계약을 `Callable[[], NoReturn]`으로 명시하고, 테스트 더블도 예외를 발생시켜 비복귀를 모사하도록 고정한다. 더 강한 대안은 `stop_worker()`가 `GRACEFUL | TERMINATED | STILL_RUNNING` 같은 결과만 반환하고 실제 hard_exit 호출은 `finalize_shutdown()` 한 곳에서만 수행하도록 책임을 단일화하는 것이다.

### [m-2] 필수 성능 수용 기준의 단회 상한과 피크 측정 방식이 확정적이지 않음 (Minor)
- **관점**: 4. 확장성, 5. 성능
- **지적**: 필수 수용 시험에서 개별 측정 상한이 “예: 60MB”로 남아 있고, Windows 피크 워킹셋 측정도 선택 사항이다. 따라서 시험 결과를 본 뒤 상한이나 측정 방식을 선택할 여지가 있다.
- **근거**: A-0002 검증 계획은 “어느 것도 명시적 단회 상한(예: 60MB)을 넘지 않아야” 한다고 규정하면서 실제 상한을 확정하지 않는다. 또한 10~50ms RSS 샘플링은 짧은 allocation peak를 놓칠 수 있음을 문서 스스로 인정하지만, 배포 대상이 Windows 10/11임에도 `PeakWorkingSetSize`는 “가능한 플랫폼에서는”이라는 보조 측정으로 남아 있다.
- **대안**: 배포 전 게이트를 `중앙값 ≤ 30MB, 각 실행 ≤ 60MB`처럼 숫자로 확정하고, Windows 릴리스 시험에서는 OS 피크 워킹셋을 필수 측정값으로 지정한다. 단계별 원인 분석까지 정확히 하려면 projection-only와 end-to-end를 별도 프로세스에서 측정해 프로세스 수명 누적 peak가 두 단계를 혼합하지 않도록 한다.

## 확인 필요
- 소스 코드가 제공되지 않아 문서에서 “구현 완료”라고 한 `stop_worker()` 반환값 전파, hard-exit 분기의 파일 I/O 부재, marker 처리 순서, purge 메타 전이 및 projection 호출 구현은 실제 코드와 대조하지 못했다.
- 배포 의존성이 실제로 LanceDB 0.34.0으로 고정되는지 확인이 필요하다. 고정되지 않는다면 `search`와 `select`는 존재하지만 `to_arrow` 계약이나 무제한 전건 반환 의미가 다른 버전에 대한 호환성 시험도 필요하다.
