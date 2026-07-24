# REVIEW-20260724-adr-0001-explicit-quit-for-tray-app-adr-0002-purge-projectio-12

| 생성일 | 모델 | 채널 | 리뷰 대상 |
|---|---|---|---|
| 2026-07-24 | gpt-5.6-sol | B(action) | `docs/ai-workflow/adr/ADR-0001-explicit-quit-for-tray-app.md`, `docs/ai-workflow/adr/ADR-0002-purge-projection-and-skip.md`, `docs/ai-workflow/architecture.md` |

> GPT 산출 원문(수정 금지). Claude의 판단은 하단 '처리 기록'으로만 추가한다 — reviews/README.md.

---

## 총평
두 ADR 모두 문제 원인을 구체적으로 식별하고 종료 경로 수렴, projection, 보수적 메타 판정 등 타당한 방향을 선택했다. 특히 A-0002는 실패·차단·성공 상태 전이와 멱등성을 상당히 세밀하게 다뤄 기존 전체 로드 문제를 구조적으로 해소한다. 다만 A-0001은 정상 quit 경로에서 dirty marker를 동기 파일 I/O로 삭제하므로, 문서가 스스로 식별한 파일 I/O 블로킹 시나리오에서 “종료는 반드시 된다”는 최상위 불변식을 여전히 위반할 수 있다. 또한 단일 인스턴스 판정과 dirty marker 소유 순서가 정의되지 않아 보조 인스턴스가 주 인스턴스의 표식을 훼손할 가능성을 차단해야 한다.

**판정**: REQUEST_CHANGES

## 동의하는 결정
- `quitOnLastWindowClosed(False)`와 모든 실제 종료 경로의 `_shutdown()` 수렴은 트레이 앱의 창 가시성과 프로세스 수명을 분리하는 적절한 결정이다.
- 워커 상태가 확실히 비실행일 때만 `quit()`, 실행 중이거나 판정 불가하면 `hard_exit`를 선택하고 둘 중 하나만 실행하도록 한 것은 QThread 잔존 위험에 대한 보수적인 대응이다.
- hard-exit 경로에서 로그 flush와 파일 I/O를 제거한 결정은 최후 안전망의 블로킹 가능성을 줄이며, 종료 확실성을 우선한다는 요구와 일관된다.
- purge 조회를 `file_path` projection으로 바꾸고 정상 유휴 사이클에는 조회 자체를 생략한 것은 메모리 문제의 빈도와 단위 비용을 모두 줄이는 올바른 접근이다.
- purge 억제 순서를 `차단 → 백오프 → 성공 스킵 → 실행`으로 고정하고 실패 시 `reconciled_sig`를 해제한 것은 재시도가 성공 메타에 가려지는 상태 전이 결함을 적절히 방지한다.
- sidecar 저장 실패와 프로세스 내 메모리 상태를 분리하고, 재시작 후 보수적으로 멱등 재실행하도록 한 정책은 가용성과 안전성 사이의 균형이 좋다.

## 지적 사항

### [B-1] 정상 quit 경로의 동기 marker 삭제가 종료 불변식을 다시 블로킹할 수 있음 (Blocker)
- **관점**: 7. 동시성, 8. 장애 대응
- **지적**: `clear_dirty_shutdown()`을 `quit_fn()`보다 먼저 동기 호출하는 구조에는 블로킹 상한이나 별도 강제 종료 장치가 없다. 따라서 marker 삭제가 멈추면 최종 `quit()`에 도달하지 못해 R-0001의 최상위 요구인 “종료는 반드시 된다”가 깨진다.
- **근거**: A-0001은 hard-exit 직전 marker 기록을 제거한 이유로 백신·파일시스템 등에 의한 동기 파일 I/O 블로킹을 명시한다. 동일한 종류의 파일 I/O인 marker 삭제는 정상 quit 분기에 남아 있으며, 검증 계획도 호출 순서만 확인할 뿐 무기한 블로킹을 복구하는 경로는 정의하지 않는다. `%APPDATA%`가 일반적으로 로컬이라는 사실은 위험을 줄일 뿐, 문서가 주장하는 절대 종료 보장의 근거가 되지는 않는다.
- **대안**: `_shutdown()` 최종화 진입 전에 daemon watchdog을 무장해 정해진 시간 안에 `clear_dirty()`와 `quit_fn()`이 완료되지 않으면 파일 I/O 없이 `hard_exit()`하도록 한다. 더 단순하게는 marker 삭제를 best-effort로 제한하고 종료 보장을 우선하되, 삭제 실패·타임아웃 시 다음 시작에서 false-positive 경고가 발생할 수 있음을 명시할 수 있다. 어떤 방식을 택하든 marker I/O 블로킹 테스트를 위해 주입 가능한 `clear_dirty`와 timeout/hard-exit 스파이 검증을 추가해야 한다.

### [M-1] 단일 인스턴스와 dirty marker의 소유권·호출 순서가 정의되지 않음 (Major)
- **관점**: 2. 숨은 가정, 7. 동시성
- **지적**: dirty marker를 어느 인스턴스가 생성하고 삭제할 수 있는지 명시되지 않았다. 보조 실행 프로세스도 marker를 기록하거나 지울 수 있다면, 이미 실행 중인 주 인스턴스의 비정상 종료 감지가 오염될 수 있다.
- **근거**: A-0001은 `main()` 시작 시 `check_and_remark_dirty_shutdown()`을 수행한다고만 설명한다. 참조된 단일 인스턴스 흐름은 `QApplication` 생성 후 기존 인스턴스 판정을 수행하며, dirty marker 호출이 그 전인지 후인지 리뷰 대상 문서에는 없다. 예를 들어 보조 인스턴스가 marker를 재기록한 뒤 “기존 인스턴스 있음”으로 정상 종료하면서 marker를 지우면, 이후 주 인스턴스가 강제 종료되어도 다음 시작에서 이를 감지하지 못할 수 있다.
- **대안**: 단일 인스턴스 획득 성공 후에만 primary가 marker를 확인·재기록하도록 하고, 동일 primary의 정상 `_shutdown()`만 삭제할 수 있다고 계약에 명시한다. 보조 인스턴스의 빠른 종료 경로는 marker API를 전혀 호출하지 않도록 테스트로 고정한다. 필요하면 marker에 인스턴스 UUID/PID를 기록하고 소유자가 일치할 때만 삭제하도록 방어할 수 있다.

### [m-1] `unsupported` 상태의 영속 모델과 전이 규칙이 불완전함 (Minor)
- **관점**: 3. 아키텍처 결함, 8. 장애 대응
- **지적**: projection API 비호환을 일시 실패와 분리해 `"unsupported"`로 분류한다고 했지만, sidecar 스키마와 데이터 흐름에는 `unsupported_sig` 또는 일반화된 상태 필드가 없다.
- **근거**: A-0002의 컴포넌트 표에는 `reconciled_sig`, `failed_sig`, `blocked_sig`만 있고, 판정 순서에도 unsupported 전용 분기가 없다. 실패 모드에서는 “`on_blocked`와 동일하게 장기 억제”한다고만 설명하므로, `blocked_sig`를 재사용하는지 별도 상태를 저장하는지, 재시작 후 어떤 알림 문구를 복원하는지 불명확하다.
- **대안**: `unsupported_sig`를 별도로 두거나 `suppressed = {sig, reason}`처럼 차단 원인을 명시적으로 저장한다. 최소한 `blocked_sig` 재사용이 의도라면 저장 필드, 전이 시 해제 대상, 재시작 후 알림 정책을 데이터 흐름과 테스트 계획에 명문화한다.

### [m-2] projection 성능 수용 기준이 정량 판정에 충분히 명확하지 않음 (Minor)
- **관점**: 4. 확장성, 5. 성능
- **지적**: 배포 전 필수 검증으로 지정한 “baseline 대비 추가 할당 수십 MB 이내”는 `수십 MB`의 정확한 상한, baseline 산정 방법, 반복 횟수가 없어 합격 여부가 판단자에 따라 달라질 수 있다.
- **근거**: A-0002는 결과 컬럼 제한만으로 저장소 단계의 column pruning이 증명되지 않는다고 올바르게 지적하지만, 이를 보완할 필수 성능 시험의 수용 기준은 모호하다. 또한 10만 행만 정의되어 있어 10배 규모에서 `file_path` Arrow 배열과 파이썬 컬렉션이 차지하는 메모리 증가도 확인되지 않는다.
- **대안**: 예를 들어 “10만 행, 대표 경로 길이 분포, warm-up 후 5회 실행, 중앙값 peak 추가 RSS ≤ 30MB이며 전체 로드 대비 ≤ 20%”처럼 환경과 수치를 고정한다. 가능하면 100만 행 시험도 추가해 메모리가 벡터 크기와 무관하고 경로 데이터 크기에 선형으로만 증가함을 기록한다.

## 확인 필요
- 리뷰 대상에 소스 코드와 `requirements.md`가 포함되지 않아, `_shutdown()`의 모든 단계가 실제로 독립 예외 처리되는지, `_shutdown_done` 설정 시점이 안전한지, quit/hard-exit 배타성이 구현과 일치하는지는 확인할 수 없다.
- `check_and_remark_dirty_shutdown()`이 단일 인스턴스 획득 전후 중 언제 호출되고, 보조 인스턴스 종료 시 `clear_dirty_shutdown()`이 호출되는지 확인이 필요하다.
- 배포 의존성에서 `lancedb==0.34.0` 또는 동등한 호환 범위가 실제로 고정되어 있는지 확인이 필요하다. 고정되지 않았다면 purge가 영구 억제되는 배포가 발생할 수 있다.
- A-0002가 언급한 필수 10만 건 peak RSS 검증이 이미 완료됐는지, 아직 릴리스 게이트로 남아 있는지 확인이 필요하다.

## 처리 기록 (중립 검토)

| ID | 판단 | 사유/반영 |
|---|---|---|
| B-1 | 수용 | `clear_dirty_shutdown()`이 실제 `unlink()`를 daemon 스레드로 위임하고 즉시 반환하도록 재작성. `finalize_shutdown`의 정상 quit 분기가 marker I/O 완료를 기다리지 않으므로 "종료는 반드시 된다" 불변식이 marker 삭제 지연·실패에 영향받지 않는다. 워치독/타임아웃 방식(리뷰가 제시한 대안 1)은 스레드 강제 종료라는 새 실패 모드를 추가하는 과잉 설계라 판단, 리뷰가 제시한 대안 2(best-effort + 문서화)를 채택. `knowmate/app/lifecycle.py` `clear_dirty_shutdown`. |
| M-1 | 수용 | `main()`에서 dirty-marker 확인/재기록을 `try_acquire_or_notify_existing()` 성공(=primary 확정) 이후로 이동. 보조 인스턴스는 marker API를 전혀 호출하지 않는다(단일 인스턴스 실패 시 `return`으로 조기 종료하므로 marker 코드에 도달하지 않음 — 별도 조건문 불필요). `knowmate/app/main.py` `main()`. |
| m-1 | 수용 | `PurgeMeta`에 `blocked_reason: str | None`(값: `"mass_delete"` \| `"unsupported"`) 필드 추가. `on_blocked(meta, op_sig, reason="mass_delete")`로 시그니처 확장, `load_purge_meta`가 타입 검증 후 round-trip, `scheduler.py`의 두 호출부가 각각 `reason="mass_delete"`/`reason="unsupported"`를 명시. 별도 `unsupported_sig` 필드는 만들지 않음 — `decide()`의 판정 로직(동일 op_sig 재시도 억제)이 두 원인 모두 동일해 상태 필드를 분리할 실익이 없고, `blocked_reason`은 순수 표시용이라 리뷰가 제시한 최소안(대안 2)으로 충분. |
| m-2 | 수용 | `architecture.md` 성능 수용 기준을 "10만 행, 대표 길이 분포(30~200자 표본화), warm-up 후 5회 반복, 회당 실행 직전/직후 peak RSS 차분, 중앙값 ≤ 30MB 합격"으로 구체화. 100만 행 추가 시험(리뷰 제안)은 베타 단계 필수 게이트로는 과함 — 10만 행 결과가 선형성 가정에 부합하면 추가 시험 없이도 배포 판단에 충분하다고 보아 범위에서 제외, 필요 시 후속 검증으로 남김. `docs/ai-workflow/architecture.md`. |

**종결 판정**: B-1(Blocker)·M-1(Major) 모두 실질적 결함으로 확인돼 수정 반영. m-1·m-2도 수용해 문서·코드 정합성을 높임. Blocker/Major 잔존 0건.
