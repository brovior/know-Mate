# REVIEW-20260724-adr-0001-explicit-quit-for-tray-app-adr-0002-purge-projectio-15

| 생성일 | 모델 | 채널 | 리뷰 대상 |
|---|---|---|---|
| 2026-07-24 | gpt-5.6-sol | B(action) | `docs/ai-workflow/adr/ADR-0001-explicit-quit-for-tray-app.md`, `docs/ai-workflow/adr/ADR-0002-purge-projection-and-skip.md`, `docs/ai-workflow/architecture.md` |

> GPT 산출 원문(수정 금지). Claude의 판단은 하단 '처리 기록'으로만 추가한다 — reviews/README.md.

---

## 총평
명시적 `quit()`으로 종료 경로를 수렴시키고 purge 조회를 projection으로 바꾸는 핵심 방향은 타당하며, 실패·차단 상태와 메타 손상을 보수적으로 처리한 점도 좋다. 다만 `QThread.terminate()`가 성공해 `isRunning()==False`가 된 경우 정상 `quit()` 경로로 넘어가는 설계는, 강제 중단된 스레드가 락을 남길 수 있다는 문서 자체의 분석과 충돌하며 “반드시 종료” 및 dirty-shutdown 탐지를 동시에 훼손할 수 있다. 또한 unsupported 억제의 데이터 모델과 상태 전이가 현재 sidecar 정의에 빠져 있고, capability signature가 LanceDB 버전에만 의존하면 앱 코드만 업데이트된 경우 억제가 해제되지 않는다. 소스 코드와 requirements.md가 제공되지 않아 구현 반영 및 요구사항 전체 대응 여부는 별도 확인이 필요하다.

**판정**: REQUEST_CHANGES

## 동의하는 결정
- `quitOnLastWindowClosed(False)`와 중앙화된 `_shutdown()`의 명시적 `quit()` 조합은 트레이 앱의 창 가시성과 프로세스 수명을 분리하므로 적절하다.
- 종료 정리 단계를 독립적으로 수행하고 마지막에 `quit` 또는 `hard_exit` 중 하나를 선택하는 구조는 부분 실패 시에도 종료 판정에 도달하도록 만든다는 점에서 합리적이다.
- purge에서 `file_path`만 projection하고, 정상 유휴 사이클은 조건부로 DB 조회 자체를 생략하는 두 단계 최적화는 근본 원인과 반복 비용을 모두 다룬다.
- purge 메타를 기존 state와 분리하고 손상·저장 실패 시 보수적으로 재실행하며, 메모리 상태는 즉시 반영하는 정책은 기존 소비자 호환성과 반복 부하 사이의 균형이 좋다.
- hard-exit 경로에서 로깅 종료나 marker 파일 I/O를 수행하지 않는 결정은 최후 종료 수단의 블로킹 가능성을 제거한다는 점에서 타당하다.

## 지적 사항

### [B-1] `QThread.terminate()` 성공 후 정상 종료로 분류되는 경로 (Blocker)
- **관점**: 7. 동시성, 8. 장애 대응
- **지적**: graceful 대기 실패 후 `QThread.terminate()`가 스레드를 멈추는 데 성공하면 `worker.isRunning()`은 False가 되고, 현재 최종 판정은 `QApplication.quit()`을 선택한다. 그러나 강제 종료된 스레드는 Python·Qt·로깅·LanceDB 락을 보유한 상태로 사라질 수 있으므로 이후 정상 인터프리터 종료가 블록될 수 있고, `app.exec()`가 반환하면 dirty-shutdown marker까지 정상 해제될 수 있다.
- **근거**: A-0001은 “워커 비실행 확인 → quit”으로만 분류하며, `stop_worker`는 “정상 종료 → terminate → 그래도 잔존 시 os._exit”로 정의한다. 동시에 정상 종료 계약에서는 `QThread.terminate()`가 로깅 핸들러 락을 보유한 채 스레드를 중단할 수 있어 `logging.shutdown()`이 영구 대기할 수 있다고 명시한다. 즉 `terminate()` 성공 뒤 정상 quit을 수행하는 경로에서도 같은 위험이 존재하며, 강제 종료 중 LanceDB 쓰기가 중단됐을 가능성이 있는데 marker가 지워지는 false negative도 발생한다.
- **대안**: `stop_worker()`가 `GRACEFUL | FORCED | HARD_EXIT` 같은 종료 결과를 반환하게 하고, `terminate()`가 한 번이라도 호출된 `FORCED` 결과는 `isRunning()==False`여도 즉시 `hard_exit()`으로 끝내라. 더 단순하게는 graceful 타임아웃 후 `terminate()`를 생략하고 바로 `hard_exit()`하도록 설계할 수도 있다. 두 경우 모두 강제 경로에서는 `app.exec()`가 반환하지 않게 하여 marker가 남도록 하고, “terminate 성공 후 quit하지 않음”을 회귀 테스트로 고정해야 한다.

### [M-1] capability signature가 앱 코드 업데이트를 식별하지 못함 (Major)
- **관점**: 2. 숨은 가정, 8. 장애 대응
- **지적**: unsupported 억제 해제 키인 `compute_capability_sig()`가 “lancedb 버전 지문”으로만 정의되어 있어, LanceDB 버전은 그대로 두고 앱의 projection 호출 방식이나 호환 코드를 수정한 업데이트는 억제를 해제하지 못한다.
- **근거**: ADR-0002와 A-0002는 UI에서 “앱 업데이트 필요”를 안내하고 “앱/lancedb 업데이트”로 재검증된다고 설명하지만, 실제 서명 입력은 LanceDB 버전으로만 명시한다. 동일한 0.34.0을 유지한 새 앱 빌드가 API 호출 오류를 수정해도 기존 sidecar의 unsupported 서명과 같아 purge가 계속 영구 억제될 수 있다.
- **대안**: capability signature에 최소한 앱 버전 또는 purge capability schema 버전을 함께 넣어라. 예를 들어 canonical JSON `{"v": 1, "app_version": ..., "lancedb_version": ..., "projection_strategy": 1}`의 SHA-256을 사용하면 의존성 변경과 앱 측 호환 구현 변경 모두 재검증을 유도할 수 있다.

### [M-2] unsupported 상태의 sidecar 필드와 전이 규칙이 설계에서 누락됨 (Major)
- **관점**: 1. 요구사항 누락, 7. 동시성, 8. 장애 대응
- **지적**: unsupported를 capability signature 기준으로 장기 억제한다고 결정했지만, sidecar의 “전체 필드” 목록과 데이터 흐름에는 이를 저장할 필드, 판정 순서, 성공·실패·차단 전이 시 해제 규칙이 없다.
- **근거**: A-0002 컴포넌트 표는 전체 필드를 `reconciled_sig`, `last_purge_ts`, `failed_sig`, `next_retry_ts`, `blocked_sig`, 스키마 버전으로 열거한다. 데이터 흐름도 `blocked_sig`와 `failed_sig`만 먼저 검사하며 unsupported 분기가 없다. 반면 실패 모드는 op_sig와 다른 capability_sig로 억제한다고 하므로 기존 `blocked_sig`만으로는 두 종류의 억제 키를 모호함 없이 표현할 수 없다. 리뷰 이력에 언급된 `blocked_reason` 역시 현재 데이터 모델과 전이에는 반영되어 있지 않다.
- **대안**: `unsupported_capability_sig`를 별도 필드로 두고 판정 순서를 `unsupported capability 억제 → 대량삭제 차단 → 일시 실패 백오프 → 성공 스킵 → 실행`으로 명시하라. capability signature가 달라지면 unsupported 상태를 해제하고 재검증하며, 성공 시 unsupported를 포함한 모든 억제 필드를 해제하고, 다른 실패 전이에서도 상호 배타적으로 어떤 필드를 남기거나 지우는지 완전한 상태 전이표로 정의하는 것이 안전하다.

### [m-1] RSS 샘플링 방식이 짧은 메모리 피크를 놓칠 수 있음 (Minor)
- **관점**: 5. 성능
- **지적**: `psutil`로 예시 50ms 간격 샘플링한 최댓값을 단회 상한 판정에 사용하면, 그보다 짧게 유지되는 Arrow 변환·삭제 후보 구성의 피크를 놓쳐 거짓 합격할 수 있다. 또한 projection 검증과 `optimize()`까지 포함한 end-to-end 수치만으로는 어느 단계가 메모리를 사용했는지 분리하기 어렵다.
- **근거**: 검증 계획은 “어느 개별 측정값도 60MB 초과 금지”라는 강한 판정을 sampled RSS에 의존하지만 샘플링 누락 오차의 상한을 정의하지 않는다. `optimize()`는 projection과 별개의 DB 유지보수 작업이므로 전체 수치가 불합격했을 때 projection 회귀인지 optimize 비용인지 판별하기 어렵다.
- **대안**: Windows의 peak working set 같은 OS 수준 프로세스 피크 계측을 함께 기록하거나 샘플링 간격을 충분히 낮추고 계측 오차를 사전 검증하라. projection+Arrow 순회 단계와 전체 purge+optimize 단계를 별도 측정하되, 최종 NFR 판정은 end-to-end 결과로 유지하면 원인 분석 가능성과 수용 기준을 모두 보존할 수 있다.

## 확인 필요
- `requirements.md`가 제공되지 않아 R-0001/R-0002의 AC·NFR·FR 문언과 설계의 완전한 대응 관계는 확인할 수 없다.
- 소스 코드가 제공되지 않아 `terminate()` 성공 후 실제 반환값, `finalize_shutdown()` 분기, unsupported 메타 필드 및 판정 순서가 문서와 동일하게 구현됐는지 확인이 필요하다.
- `AttributeError` 분류가 `.select()` 메서드 부재에만 좁게 적용되는지 확인이 필요하다. 넓은 호출 구간의 모든 `AttributeError`를 unsupported로 분류하면 데이터·드라이버의 다른 결함을 영구 억제로 오분류할 수 있다.
- watch folder에 상대 경로가 허용되는지 확인이 필요하다. 허용된다면 `abspath()` 결과가 프로세스 시작 디렉터리에 의존하므로 op_sig와 소속 판정의 안정성을 위해 기준 디렉터리를 고정해야 한다.
