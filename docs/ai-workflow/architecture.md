# architecture.md — 아키텍처 설계

| 상태 | 마지막 갱신 | 연결 문서 |
|---|---|---|
| Accepted (A-0001·A-0002 모두) | 2026-07-24 | requirements.md, adr/, reviews/ |

> **사용법**: requirements.md의 Approved 요구를 받아 설계 블록을 작성한다.
> 상태 흐름: Draft → (GPT 독립 검증 = reviews/REVIEW-*.md) → Reviewed → (Claude 최종 확정) → Accepted.
> **개별 결정의 "왜"는 여기가 아니라 ADR에 쓴다** — 이 문서는 "무엇·어떻게"의 현재 스냅샷이고,
> ADR은 결정의 불변 이력이다. 설계가 바뀌면 이 문서는 덮어쓰고, ADR은 Superseded로 잇는다.

---

## 블록 템플릿

```markdown
## A-XXXX: <설계 제목>  (상태: Draft|Reviewed|Accepted / 대응 요구: R-XXXX)

### 개요
(설계 한 단락 요약 — 처음 읽는 리뷰어 기준)

### 컴포넌트와 책임
| 컴포넌트 | 책임 | 위치(모듈/경로) |
|---|---|---|

### 데이터 흐름
(입력 → 처리 → 출력. 텍스트 다이어그램 권장. 실패 경로·quarantine 분기 포함)

### 핵심 결정과 트레이드오프
- 결정: ... → 근거·대안 비교는 ADR-XXXX
(중요 결정마다 ADR 링크. ADR 없는 중요 결정 = 리뷰에서 지적 대상)

### 실패 모드
| 실패 | 감지 | 대응(격리/재시도/중단) |
|---|---|---|

### 검증 계획
(이 설계가 맞았는지 무엇으로 확인하나 — 테스트·실측 지표)

### 리뷰 이력
- reviews/REVIEW-YYYYMMDD-*.md → 처리 결과 요약 (수용 N / 기각 M)
```

---

<!-- 여기부터 실제 설계 블록을 추가한다. -->

## A-0001: 트레이 상주 앱 종료 모델 — 명시적 quit  (상태: Accepted / 대응 요구: R-0001)

### 개요
현재 앱은 이벤트 루프 종료를 Qt의 암묵 규칙 `quitOnLastWindowClosed`(기본 True)에만 의존하고,
코드 어디에서도 `QApplication.quit()`을 호출하지 않는다. Qt는 "마지막으로 **보이는** 창이 닫힐
때"만 루프를 끝내므로, 창이 트레이로 숨겨진(hide) 상태에서 [종료]→`close()`를 하면 "보이는 창
닫힘" 사건이 발생하지 않아 `app.exec()`가 영영 반환되지 않는다 — 프로세스 잔존의 직접 원인.
수정: 트레이 앱 표준 관용구대로 암묵 종료를 끄고(`setQuitOnLastWindowClosed(False)`), 모든 종료
경로가 수렴하는 `_shutdown()` 끝에서 명시적으로 `quit()`을 호출한다.

### 컴포넌트와 책임
| 컴포넌트 | 책임 | 위치(모듈/경로) |
|---|---|---|
| `main()` | `app.setQuitOnLastWindowClosed(False)` 설정 | `knowmate/app/main.py` |
| `MainWindow._shutdown()` | 스케줄러 정지 → 트레이 숨김 → 워커 종료 → **최종 판정: 워커 비실행 확인 시 `quit()`, 실행 중·판정 불가 시 `hard_exit`(정확히 하나)** | `knowmate/app/main.py` |
| `lifecycle.stop_worker()` | 행오버 워커 에스컬레이션(정상→terminate→하드 종료 순서는 기존 유지). **반환값 계약이 변경됨**(리뷰15 B-1) — `terminate()`가 이번 호출에서 쓰였는지 `bool`로 반환하고, 호출부(`_shutdown()`)가 이 값을 `finalize_shutdown(force_hard_exit=)`로 전달해야 한다(누락 시 강제 중단된 워커가 정상 종료로 오분류됨, 리뷰16 M-1) | `knowmate/app/lifecycle.py` |
| `HardExit` (타입 별칭) | `hard_exit` 콜백의 계약을 `Callable[[int], NoReturn]`으로 명시(리뷰17 m-1) — "quit/hard_exit 정확히 하나만 실행"이라는 보장은 `stop_worker()`가 부른 hard_exit가 **절대 반환하지 않는다**는 전제에 의존한다(반환하면 그 다음 `finalize_shutdown()`이 다시 판정해 두 번째 hard_exit 또는 quit이 관측될 수 있음). 운영 기본값(`os._exit`)은 이 계약을 만족. 테스트 더블은 비복귀를 모사하지 않지만, `stop_worker()`의 `bool` 반환값 기반 설계가 "hard_exit가 반환하더라도 다음 판정이 다시 보수적으로 hard_exit로 수렴"하는 방어선을 이미 갖춰 안전 | `knowmate/app/lifecycle.py` |

### 데이터 흐름
```
[종료](트레이) ─┐
X (close_action=quit) ─┴→ close() → closeEvent → _shutdown()
                                      ├ scheduler.stop()
                                      ├ tray.hide()
                                      ├ stop_worker(worker)   # 행오버면 os._exit까지 에스컬레이션
                                      └ 최종 판정(★ 신규 — 항상 도달, 창 가시성과 무관):
                                          worker 비실행 확인 → QApplication.quit()
                                          실행 중·판정 불가 → hard_exit  (정확히 하나만 실행)
X (close_action=tray) → event.ignore() + hide()               # 종료 아님(기존 유지)
```

### 핵심 결정과 트레이드오프
- 결정: 암묵 `quitOnLastWindowClosed` 의존을 버리고 명시적 `quit()`으로 전환 → 근거·대안은 ADR-0001
- 트레이드오프: 이후 모든 종료 경로가 `_shutdown()` 경유를 강제받는다(경로 누락 시 종료 안 됨).
  현재 종료 경로는 트레이 [종료]와 `close_action=quit` 둘뿐이며 둘 다 `closeEvent→_shutdown`으로
  수렴함을 코드로 확인.

### 정상 종료 계약 (데이터 무결성 — R-0001 NFR-1의 보장 근거, 리뷰 M-1 반영)
`quit()`은 `_shutdown()`의 **마지막** 단계이며 앞 단계들이 다음을 보장한 뒤에만 도달한다:
1. **신규 작업 차단**: `scheduler.stop()`이 유휴 타이머·복귀 워처를 정지(`IdleScheduler.stop`) →
   새 사이클 트리거 없음.
2. **진행 중 작업의 state 저장**: `stop_worker`의 graceful 경로(`cancel()` + `wait(8s)`)는 워커가
   현재 파일을 마친 뒤 취소 분기에서 `save_state()`를 호출하고 반환하는 것까지 기다린다
   (`CollectorWorker._run_cycle`의 취소 분기·정상 완료 경로 모두 `save_state` 후 종료 — 기존 코드).
   `save_state`는 tmp→replace **원자 교체**(기존 `test_atomic_save_uses_tmp_then_replace`로 보장).
3. **COM 정리**: 워커 `run()`의 finally가 `quit_com_apps()`로 소유 Office를 정리(기존 코드).
4. **로그 flush**: 정상 경로(quit → 정상 인터프리터 종료)는 `logging.shutdown()`(atexit)이 flush를
   보장한다. **하드 종료 경로는 flush를 포기하고 즉시 `os._exit()`만 호출한다**(설계 리뷰 9차 B-1) —
   `logging.shutdown()`을 먼저 부르면, `QThread.terminate()`가 로깅 핸들러 락을 쥔 채로 스레드를
   강제 중단시켰을 경우 그 락을 영원히 기다려 하드 종료(최후 안전망) 자체가 멈추는 모순이 생긴다.
   하드 종료는 "반드시 종료된다"는 불변식이 로그 보존보다 우선한다.
**단계별 실패 정책 (리뷰3 M-1 — "보장 후 quit"과 "예외 무관 quit"의 관계 명확화)**:
위 계약은 **정상 경로**의 보장이고, 앞 단계가 실패한 경우는 데이터 무결성 예외로 다음과 같이
처리한다(무결성 완주보다 "종료는 반드시 된다"를 우선 — R-0001 FR-1이 최상위):
- `scheduler.stop()` 실패 → 계속 진행. quit()으로 이벤트 루프가 끝나면 Qt 타이머는 더 이상
  발화하지 않으므로 신규 사이클 차단은 결과적으로 성립.
- `stop_worker()` 자체가 예외 → 계속 진행해 `_shutdown()` 마지막 판정으로: `worker.isRunning()`이
  False로 **확인되면** quit, **True이거나 조회 자체가 예외면(판정 불가)** 보수적으로
  hard_exit(quit만으로는 QThread 잔존 가능 — 리뷰5 M-2, "실행 중 또는 판정 불가 → hard_exit").
  quit/hard_exit는 정확히 하나만 호출된다(ADR-0001 결정과 일치 — 리뷰5 M-1로 ADR 갱신).
- terminate/`os._exit` 강제 경로에서는 계약 2·3(그 사이클의 state 저장·COM 정리)이 생략될 수
  있다. 그 사이클의 state 갱신 유실은 확실하며 다음 사이클 재인덱싱으로 자가 복구된다. **다만
  LanceDB 쓰기(add/delete/optimize) 도중 강제 종료됐을 경우의 영향 범위는 현재 미확정이다** —
  커밋 원자성이 검증되지 않았으므로 "state 갱신만 잃는다"고 단정하지 않는다(리뷰9 M-2로 이전의
  단정적 서술을 정정). 검증·복구 계획은 아래 "남은 한계" 항목 및 `docs/DESIGN.md` § 종료 확실화
  참조 — 현재는 최악의 경우 인덱스 폴더 삭제 후 전체 재인덱싱이 항상 유효한 복구 경로임을
  근거로 후속 과제화했다(추측성 자동 복구 로직 대신). 또한 8초 graceful 대기는 "정상적으로 오래
  걸리는 추출"에도 만료될 수 있음을 인정한다 — 이 경우에도 종료 우선 원칙은 동일하다.

**보조 실행 단위의 종료 계약 (리뷰4 M-1)**: 워커가 만드는 파이썬 스레드는 전부 **daemon**이다 —
스캔 생산자 스레드는 `daemon=True`(scheduler.py `scan-producer`), 워치독 타이머는 daemon
Timer(com_watchdog, 기존 `test_default_timer_is_daemon`로 고정). 따라서 QThread 종료 후 인터프리터
종료를 막는 non-daemon 잔존 스레드는 존재하지 않으며, `_shutdown()`의 종료 판정은
`worker.isRunning()`(QThread)만 보면 충분하다. **이후 non-daemon 스레드를 새로 만드는 것은 이
계약 위반**이며, daemon 속성 테스트를 회귀로 유지한다(별도 has_live_aux_workers류 추적 장치는
daemon 사실이 성립하는 한 불필요 — 미도입).

### 실패 모드
| 실패 | 감지 | 대응(격리/재시도/중단) |
|---|---|---|
| `quit()` 후에도 잔존(비Qt 요인: 워커 행오버) | `stop_worker`의 wait 타임아웃 | 기존 에스컬레이션이 `os._exit(0)` (유지) |
| `_shutdown()` 도중 예외 | 각 단계 독립 try/except(기존) | 다음 단계 계속 → **최종 판정에는 항상 도달**해 quit 또는 hard_exit 중 하나를 실행(리뷰6 M-1) |
| 새 종료 경로 추가 시 `_shutdown()` 미경유 | 코드 리뷰 규칙 | `_quit_app`/`closeEvent` 외 종료 경로 금지 문서화 |
| `_shutdown()` 중복 진입(근접한 이중 종료 요청) | `_shutdown_done` 플래그 | 멱등 가드 — 두 번째 진입은 즉시 반환. "정확히 하나"는 **프로세스 수명 기준**이며 중복 호출 테스트로 고정(리뷰6 m-1) |
| 하드 종료 직전 `logging.shutdown()`이 로깅 핸들러 락 대기로 영구 블록 | 코드 검토 | `_default_hard_exit`는 `logging.shutdown()`을 호출하지 않고 즉시 `os._exit()`만 실행(리뷰9 B-1) — 로그 유실을 감수하고 종료 확실성을 우선 |
| LanceDB 쓰기(add/delete/optimize) 도중 강제 종료 시 손상 범위 미확정 | **`lifecycle.check_and_remark_dirty_shutdown()`** — 앱 **시작 시**, 단일 인스턴스 소유권을 획득한 프로세스에서만(리뷰12 M-1) 표식을 확인·재기록(read-then-remark). `app.exec()`가 정상 반환한 뒤 `main()`의 정상 반환 경로에서만(리뷰13 M-1) `clear_dirty_shutdown()`으로 지운다 | 자동 감지·복구는 여전히 추측성 위험 판단으로 미구현이나(후속 과제, 리뷰8 M-1), **강제 종료가 있었다는 사실만은 저비용으로 기록** — 다음 시작 시 WARNING 로그 + 트레이 풍선 알림(리뷰11 M-1 — 로그만으로는 GUI 사용자가 놓치기 쉬움)으로 "검색 결과가 이상하면 재인덱싱 권장" 안내. 자동 격리·재구축은 하지 않는다 — "인덱스는 재생성 가능한 파생 데이터"라 사용자가 폴더 재추가로 직접 재인덱싱 가능 |
| **(리뷰11 B-1)** 표식 기록이 hard-exit 직전 동기 파일 I/O였다면, 그 I/O 자체가 블록(백신·네트워크 드라이브 등)될 때 최후 안전망인 하드 종료가 멈출 수 있음 | 코드 검토 | 표식 기록 위치를 **hard-exit 직전 → 앱 시작 시**로 이동. hard-exit 경로(`stop_worker`/`finalize_shutdown`의 hard_exit 분기)는 이제 파일 I/O를 전혀 거치지 않고 `hard_exit()`만 호출한다 — 9차 B-1로 확립한 "하드 종료는 무조건·즉시" 불변식과 재정합 |
| **(리뷰12 B-1)** `clear_dirty_shutdown()`의 동기 `unlink()`가 블록되면 정상 quit 경로도 지연될 수 있음 | 코드 검토 | 삭제를 daemon 스레드에 위임하고 즉시 반환(결과를 기다리지 않음) — 삭제 실패·지연은 다음 시작 시 오탐(false positive) 위험만 있고, 그 편이 종료 지연보다 우선순위가 낮다 |
| **(리뷰14 M-2)** daemon 스레드에 삭제를 맡기고 대기 없이 반환하면, 호출 직후 인터프리터가 곧바로 종료돼(`main()`의 마지막 단계) 스레드가 실행되기 전/`unlink()` 완료 전에 잘려 정상 종료에서도 삭제가 보장되지 않음 | 코드 검토 | daemon 스레드 시작 후 **최대 1초(`_CLEAR_DIRTY_JOIN_TIMEOUT_SEC`)만 `join()`**(best-effort 상한). 이 위치(리뷰13 M-1로 `app.exec()` 정상 반환 후로 이동됨)는 이벤트 루프가 이미 끝난 뒤라, 짧은 상한 대기가 "종료는 반드시 된다" 불변식을 재위협하지 않는다 — 1초를 넘겨도 종료를 계속한다 |
| **(리뷰13 M-1)** `quit_fn()`(QApplication.quit) 호출은 이벤트 루프 종료를 "요청"할 뿐 완료를 보장하지 않는데, `finalize_shutdown()`이 그 직전에 표식을 지우면 요청~실제 반환 사이 크래시 시 다음 시작에서 강제 종료를 탐지 못하는 false negative가 생김 | 코드 검토 | `finalize_shutdown()`에서 `clear_dirty()` 호출을 완전히 제거. 표식 해제는 `main()`에서 `app.exec()`가 **정상 반환한 뒤**로 이동 — hard-exit 경로는 `os._exit()`로 `app.exec()`에 절대 반환하지 않으므로 "app.exec() 반환 = 정상 quit 확정"이 성립해, 이 위치가 표식 해제의 유일하게 안전한 시점이다 |
| **(리뷰12 M-1)** 보조 인스턴스가 단일 인스턴스 판정 전에 표식을 건드리면 실행 중인 주 인스턴스의 표식을 오염시켜 오탐/누락 유발 | 코드 검토 | 시작 흐름을 `QApplication 생성 → 단일 인스턴스 획득/기존 인스턴스 통지(실패 시 즉시 return) → 소유권 획득 성공 시에만 표식 확인·재기록 → MainWindow 생성 → app.exec()`로 고정. 기존 인스턴스를 발견한 보조 프로세스는 표식 API를 전혀 호출하지 않고 `return`으로 조기 종료한다 |
| **(리뷰15 B-1)** `QThread.terminate()`가 스레드를 멈추는 데 성공하면(`isRunning()==False`) 기존 최종 판정이 이를 "정상 종료"로 분류해 `quit()`으로 넘어감 — 그러나 강제 중단된 스레드는 임의 지점에서 멈춘 것이라 로깅 핸들러 락·LanceDB 파일 락 등을 쥔 채 죽었을 수 있어, `quit()` 이후 인터프리터 종료가 블록되거나 `app.exec()`가 그래도 반환돼 dirty-shutdown marker가 false negative로 지워질 수 있음 | 코드 검토 | `stop_worker()`가 `terminate()` 사용 여부를 bool로 반환하도록 변경, `_shutdown()`이 이 값을 `finalize_shutdown(force_hard_exit=...)`로 그대로 전달. `force_hard_exit=True`면 `isRunning()` 값과 무관하게 항상 `hard_exit()`한다 — `terminate()`가 한 번이라도 쓰이면 그 이후는 "정상 종료" 분류를 절대 하지 않는다 |

### 검증 계획
- 사외 단위: `_shutdown()`이 각 단계(스케줄러 stop → stop_worker → quit) **순서대로** 호출하고,
  quit이 stop_worker 반환 전에 불리지 않음을 주입 스파이로 검증(PyQt6 미의존 형태로 분리).
- 사외 단위(예외 매개변수화, 리뷰4 m-2): scheduler.stop / tray.hide / stop_worker / isRunning
  조회가 **각각** 예외를 던지는 케이스에서 후속 단계가 계속 실행되고 최종적으로 quit(워커 미실행)
  또는 hard_exit(워커 잔존)가 **정확히 하나만** 호출됨을 검증. 보조 스레드 daemon 속성 테스트 유지.
- 사외 단위(리뷰9 B-1): `_default_hard_exit`가 `logging.shutdown()`을 호출하지 않고 즉시
  `os._exit()`만 부르는지 스파이로 검증.
- 사외 단위(리뷰10 M-1 → 리뷰11 B-1 → 리뷰13 M-1로 갱신): `stop_worker`/`finalize_shutdown`의
  모든 hard_exit 분기가 파일 I/O 없이(콜백 미주입) 즉시 `hard_exit()`만 호출하는지 검증.
  `finalize_shutdown()`은 표식 해제 콜백을 아예 갖지 않음을(시그니처 검증) 확인 — 표식 해제는
  `main()`의 `app.exec()` 정상 반환 후 경로에서만 일어나므로, `finalize_shutdown` 단위 테스트
  범위 밖이다. `check_and_remark_dirty_shutdown`의 왕복(첫 실행=표식 없음이나 기록됨 / 표식
  남은 상태=dirty 보고 후 재기록 / clear 후=다시 없음), `clear_dirty_shutdown`의 비동기
  즉시 반환(리뷰12 B-1) 단위 테스트.
- 사외 단위(리뷰12 M-1): 단일 인스턴스 획득 실패(보조 인스턴스) 시 `check_and_remark_dirty_shutdown`/
  `clear_dirty_shutdown` 어느 쪽도 호출되지 않음을 `main()` 흐름 검토·통합 테스트로 고정.
- 사외 단위(리뷰15 B-1): `stop_worker()`가 `terminate()`를 사용한 모든 경우(성공/실패 둘 다)
  `True`를 반환하고, 정상 종료(첫 wait 성공) 시에는 `False`를 반환함을 검증. `finalize_shutdown`이
  `force_hard_exit=True`이면 `isRunning()`이 False로 확인돼도 `quit_fn()`을 호출하지 않고
  `hard_exit()`만 호출함을(반대로 `force_hard_exit=False`면 기존처럼 정상 quit) 스파이로 검증.
- 사외 통합(가능 시): `QT_QPA_PLATFORM=offscreen`으로 QApplication을 띄워 창 숨김/표시/
  `close_action=quit` 세 분기에서 이벤트 루프가 실제 종료되는지 통합 테스트(리뷰 m-2 반영).
  offscreen 불가 환경이면 closeEvent 분기 단위 테스트 + 아래 실기 3경로를 릴리스 체크리스트로 고정.
- 사내 실기(리뷰5 m-3·리뷰7 M-2 — 경로(창 상태)와 워커 상태를 분리한 매트릭스): ① 창 숨김 ②
  창 표시 ③ `close_action=quit` 세 경로를 **워커 미실행 상태**에서 실행해 **3초 이내**(AC-1·AC-2)
  판정. ④ **정상 인덱싱 실행 중** [종료] — graceful 취소 대기 포함 **12초 이내**(AC-3a).
  ⑤ **행오버 중** [종료] — 에스컬레이션 경유 **15초 이내**(AC-3b). elapsed는 종료 명령 시점부터
  프로세스 소멸까지 측정, 릴리스 체크리스트에 고정.

### 리뷰 이력
- reviews/REVIEW-20260724-...-projectio{,-2,...,-8}.md (GPT 채널 B, 8회) → 1~4·7차
  REQUEST_CHANGES 전건 처리(수용 위주, 일부 근거 기각), 5·6차 APPROVE_WITH_CHANGES 전건 수용,
  8차(PR #59 머지 후 도착) M-1 보류(후속 과제화)·나머지 수용 → Blocker/Major 미종결 0건,
  구현 완료 후 Accepted 승격 (2026-07-24)
- reviews/REVIEW-20260724-...-projectio-{9,10,11,12,13,14}.md (PR #60 구현 반영 중 재트리거,
  6회, 전건 REQUEST_CHANGES) — 9차 hard-exit logging.shutdown 제거(B-1), 10차
  max_delete_ratio fail-closed 검증·dirty-shutdown 마커 최초 도입(B-1/M-1), 11차 마커
  기록 위치를 hard-exit 직전에서 앱 시작 시로 재설계(B-1)·purge unsupported 분류(M-2),
  12차 마커 삭제 daemon 비동기화(B-1)·단일 인스턴스 확정 후로 마커 처리 순서 이동(M-1)·
  blocked_reason 필드 도입(m-1), 13차 마커 해제 시점을 app.exec() 정상 반환 후로 재이동
  (M-1)·성능 수용 절차를 독립 프로세스 psutil 샘플링으로 재작성(M-3), 14차 unsupported
  억제를 op_sig가 아닌 capability_sig(lancedb 버전) 기준으로 재설계(M-1)·마커 삭제
  daemon에 짧은 join 상한 추가(M-2)·성능 수용 절차의 warm-up 범위를 DB open까지로
  제한(M-3) — 전 라운드 Blocker/Major 전건 처리, 미종결 0건 유지
- reviews/REVIEW-20260724-...-projectio-{15,16}.md (2회, 전건 REQUEST_CHANGES) — 15차
  QThread.terminate() 성공을 정상 종료로 오분류하던 결함 수정(stop_worker가 terminate
  사용 여부 반환 → finalize_shutdown force_hard_exit로 전파, B-1)·capability_sig에
  앱 버전·projection 호출방식 버전 포함(M-1)·sidecar 필드/판정순서 의사코드 갱신(M-2)·
  성능 수용 절차를 projection 단계/end-to-end 단계로 분리 측정(m-1), 16차 stop_worker
  반환값 계약 변경을 컴포넌트 표에 반영(M-1)·unsupported 판정을 API 메서드 존재
  여부만으로 좁게 확인하도록 capability probe 도입(M-3)·성능 시험의 DB 격리(프로세스별
  seed DB 복제) 명시(m-1) — 전 라운드 Blocker/Major 전건 처리, 미종결 0건 유지
- reviews/REVIEW-20260724-...-projectio-17.md (APPROVE_WITH_CHANGES — 최초로 Blocker/Major
  없이 Minor만 도착) — hard_exit 콜백 계약을 `HardExit = Callable[[int], NoReturn]` 타입
  별칭으로 명시(m-1)·성능 수용 기준을 "예시" 문구 없이 중앙값 ≤30MB·개별 ≤60MB로 확정하고
  Windows 릴리스 시험에서 `PeakWorkingSetSize` 병행 측정을 필수로 승격(m-2). 이 라운드로
  Blocker/Major/Minor 전건 처리 완료, Accepted 재확인

## A-0002: purge 조회 경량화 — 컬럼 projection + 조건부 스킵  (상태: Accepted / 대응 요구: R-0002)

### 개요
`_purge_removed_folders`는 "watch_folders에서 제거된 폴더의 청크를 DB에서 삭제"하는 정리
단계인데, 판단에 `file_path` 목록만 필요함에도 매 사이클 전체 테이블(벡터+암호문 포함)을
pandas로 로드한다. 수정 ①: 조회를 `file_path` 단일 컬럼 projection으로 교체. 수정 ②:
"동일 op_sig(구성+dry_run+차단율)·처리 0건·마지막 성공 purge 후 24h 미만이며 실패/차단 억제
상태가 아닌 경우"에만 purge를 스킵(최종 조건 전체 — 상세는 데이터 흐름). CleanupManager(파일 단위 orphan)는
state 기반이라 무관 — 변경 없음.

### 컴포넌트와 책임
| 컴포넌트 | 책임 | 위치(모듈/경로) |
|---|---|---|
| `_purge_removed_folders` | `file_path`만 projection 조회, 삭제 판단·실행(기존 안전장치 유지), 성공 완료 여부 반환 | `knowmate/collector/scheduler.py` |
| `_run_cycle` | 사이클 시작 시 불변 스냅샷·op_sig 계산, 스킵 판정(서명·0건·24h), 성공 시에만 meta 갱신 | `knowmate/collector/scheduler.py` |
| purge 메타 sidecar | 전체 필드 보관(tmp→replace 원자 교체): `reconciled_sig`(성공 서명)·`last_purge_ts`(성공 시각)·`failed_sig`+`next_retry_ts`(일시 실패 백오프)·`blocked_sig`+`blocked_reason`(대량삭제 차단 또는 unsupported, 리뷰12 m-1)·`blocked_capability_sig`(unsupported 전용 억제 키, 리뷰14/15 M-1) + 스키마 버전. 필드별 유효성 규칙은 데이터 흐름 참조. 기존 state 스키마 불변 | `index_state.meta.json` (신규) |

### 데이터 흐름
```
사이클 시작:
  snapshot = normalize_folders(watch_folders)
      # 공용 함수 1개로 통일(리뷰3 m-1): 절대경로화(abspath) → normpath → normcase →
      # 구분자 '/' 통일 → 후행 구분자 제거 → 중복 제거 → 정렬. 환경변수 확장은 하지 않음
      # (config에 리터럴 경로만 허용 — 기존 동작). UNC와 매핑 드라이브·junction은 **문자열이
      # 다르면 다른 실체로 취급**(파일시스템 해석 안 함 — 오판 시 결과는 불필요 purge 1회로 무해).
      # 서명 계산과 purge 소속 판정 모두 이 함수의 결과만 사용(이원화 금지).
      # 소속 판정은 **경계 인식 비교**: `p == root or p.startswith(root + "/")` — 구분자를
      # 붙여 비교하므로 `C:/watch`가 `C:/watch-old/...`를 포함한다고 오판하지 않는다(기존
      # _purge_removed_folders의 belongs_to_any와 동일 규칙, 리뷰4 m-1). 경계·드라이브 상이·
      # 중첩 watch folder 케이스를 회귀 테스트로 고정.
  op_sig = SHA-256(canonical JSON)   # {"v":1, "folders":[...], "dry_run":bool,
                                     #  "max_delete_ratio":float} 를 sort_keys=True·고정
                                     # separator·UTF-8로 직렬화(필드 경계 모호성 제거, 리뷰2 m-2)
사이클 종료부 — 판정 순서 고정(리뷰15 M-2로 unsupported capability 억제를 최우선으로 명문화):
  capability_sig = compute_capability_sig()   # lancedb 버전 + 앱 버전 +
                                               # PROJECTION_STRATEGY_VERSION의 SHA-256(리뷰15 M-1)
  # 시각 필드 검증은 필드별로 다르다(리뷰4 B-1 — next_retry_ts는 정의상 미래값이 정상):
  #  - last_purge_ts: **모든 미래값 무효**(스킵 불가) — 스킵 조건 0 <= (now-last_purge_ts)와
  #    동일 문언으로 통일, 오차허용 없음(리뷰6 m-3)
  #  - next_retry_ts: now < 값 <= now + 설정백오프 범위만 유효.
  #    그보다 먼 미래값은 손상으로 간주 → 백오프 무시(억제 해제)
  0) if meta["blocked_sig"] is not None and meta["blocked_reason"] == "unsupported":
         if meta["blocked_capability_sig"] == capability_sig:
             return (DB 조회 없음)  # unsupported 영구 장애 억제 — op_sig와 무관, capability_sig로만 판정
         # capability_sig 불명/변경(앱·lancedb 업데이트) → 억제 해제, 아래로 진행
  1) elif meta["blocked_sig"] == op_sig:
         return (DB 조회 없음)      # 대량삭제 차단 상태 — 동일 설정으론 자동 재시도 안 함
  2) if meta["failed_sig"] == op_sig and next_retry_ts가 유효 범위 and now < next_retry_ts:
         return (DB 조회 없음)      # 일시적 실패 백오프(기본 30분, config화)
  3) if op_sig == meta["reconciled_sig"] and 처리 0건
        and 0 <= (now - meta["last_purge_ts"]) < 강제주기(기본 24h):
         return (DB 조회 없음)      # ★ 정상 빠른 경로 — O(1)
  4) purge 실행:
      file_paths = chunks 테이블에서 file_path 컬럼만 projection 조회
                   # Arrow 컬럼 직접 순회 — pandas 변환 생략
      (이하 기존과 동일: 소속 판정 → 대량삭제 차단 → dry_run → 삭제 → optimize)
      성공 완료: meta["reconciled_sig"]=op_sig; meta["last_purge_ts"]=now;
                 failed_sig·blocked_sig·blocked_reason·blocked_capability_sig·next_retry_ts 모두 해제  # 원자 갱신
                 # 커밋 규칙(리뷰6 m-2, 대안1): 성공 메타는 **메모리에 즉시 승격**하고 sidecar
                 # 저장은 결과에 영향 없음 — 저장 실패 시 현 프로세스는 정상 스킵을 계속하고
                 # (매분 O(N) 재조회 방지), **재시작 후에만** 보수적으로 재실행된다(멱등이라
                 # 안전). 저장 실패는 ERROR 로그. 실패·차단 상태도 동일하게 메모리 즉시 반영
      일시적 예외: meta["failed_sig"]=op_sig; meta["next_retry_ts"]=now+백오프;
                 **meta["reconciled_sig"] 해제**   # 실패한 op_sig의 성공 스킵 자격 무효화 —
                 # 백오프 만료 후 이전 성공 메타가 3)을 참으로 만들어 재시도를 24h까지
                 # 가로막는 결함 방지(리뷰4 B-2, 대안2 채택: 별도 강제분기보다 상태가 단순)
      대량삭제 차단: meta["blocked_sig"]=op_sig; meta["blocked_reason"]="mass_delete" + 기존 UI 알림(1회) —
                 구성·차단율 변경으로 op_sig가 바뀌어야 재실행
      unsupported(projection API 메서드 자체가 없음 — 판정 범위는 아래 핵심 결정 참조):
                 meta["blocked_sig"]=op_sig; meta["blocked_reason"]="unsupported";
                 meta["blocked_capability_sig"]=capability_sig;
                 **meta["reconciled_sig"]도 함께 해제**(다른 전이와 동일하게 성공 스킵
                 자격을 무효화 — 명시하지 않으면 이전 op_sig의 성공 메타가 남아 있다가
                 capability_sig 변경으로 억제가 풀린 뒤에도 판정 3(성공 스킵)이 최대 24h
                 재검증을 가로막을 수 있다는 리뷰16 M-2 지적을 코드는 이미 반영하고
                 있었으나(on_blocked이 항상 새 PurgeMeta로 교체) 이 의사코드에 빠져
                 있었다 — 문서 정정)
                 + UI 알림(capability_sig 변화 기준 1회) — **24h 강제 reconciliation 예외**
                 (시간이 아니라 capability_sig 변화로만 재검증, 리뷰14/15 M-1)
  # 실패·차단 상태는 sidecar 저장과 **무관하게 프로세스 내 메모리에 즉시 반영**(리뷰4 M-2) —
  # sidecar 저장 실패(권한·디스크·백신 잠금)여도 현재 프로세스에서는 억제·알림 1회가 유지된다.
  # 저장 실패는 ERROR 로그로 관측, 다음 메타 갱신 기회에 자연 재시도.

meta 저장: index_state.json 이 아니라 **별도 sidecar 파일**(index_state.meta.json, tmp→replace
원자 교체). 기존 state 스키마(경로→dict)와 소비자 코드를 일절 건드리지 않는다(마이그레이션 불필요).
```

- **처리 0건의 정의**: 이번 사이클에서 소비자 루프가 꺼낸 태스크(성공·실패·연기 포함)가 0건이고
  취소되지 않았음. 실패·연기가 있던 사이클은 스킵하지 않는다(보수적).
- **강제 reconciliation**: 스킵이 계속되더라도 `last_purge_ts` 기준 24h(기본, config화) 경과 시
  0건이어도 purge를 1회 실행 — 외부 요인으로 생긴 state-DB 불일치가 무기한 방치되지 않는 상한.
  이 사이클은 O(N)이되 경로 데이터만 다룬다(R-0002 NFR-1 개정 문언과 일치 — 리뷰2 B-1/M-1 반영).

### 핵심 결정과 트레이드오프
- 결정(확정, 구현 완료 — 리뷰9 M-1 반영): projection 방식은 **`table.search().select(["file_path"]).to_arrow()`**로
  확정했다. 검토했던 `table.to_lance().to_table(columns=[...])`는 별도 `pylance` 패키지 설치가
  추가로 필요해 기각. lancedb 0.34.0(이 저장소 개발 환경)에서 실측 검증: ① `.select()`로 지정한
  컬럼(`file_path`)만 결과 스키마에 실림(vector·text 컬럼 자체가 응답에 없음 — "결과에서만 버리는"
  방식이 아니라 요청한 컬럼만 스캔) ② 벡터 컬럼 포함 전체 로드 대비 20,000행 기준 약 6배 빠름
  (0.046s vs 0.273s) ③ 벡터 쿼리 없이 호출해도 숨은 기본 limit이 없어 전건이 반환됨(500/500행
  확인). → 근거·대안은 ADR-0002
- 결정: 스킵 조건은 "op_sig(구성+dry_run+차단율) 불변 && 처리 0건 && 24h 미경과". 서명은 purge가
  **성공 완료된 경우에만** 갱신한다(실패·차단 시 미갱신 → 재시도 보존). → 근거는 ADR-0002
- 결정: 메타는 sidecar 파일(index_state.meta.json) — 기존 state 스키마·소비자 무변경 (리뷰 M-2 반영)
- 트레이드오프: 불일치 복구가 최대 24h(강제 주기)까지 지연될 수 있다 — 무기한 방치는 강제
  reconciliation으로 차단(리뷰 B-1 반영). sidecar 파일이 1개 늘어난다.

### 실패 모드
| 실패 | 감지 | 대응(격리/재시도/중단) |
|---|---|---|
| projection API가 배포 고정 lancedb 버전에 없음 | `table.search`/`search().select` **메서드 존재 자체**를 `getattr`+`callable`로 좁게 확인(리뷰16 M-3) — `select(...).to_arrow()` **호출 도중** 발생하는 예외(AttributeError 포함)는 API 부재가 아니라 내부 결함일 수 있으므로 "failed"(일시적)로 분류하고, "unsupported"는 메서드가 아예 없을 때만 판정한다. 넓은 try로 호출 체인 전체를 감싸면 다른 원인의 AttributeError까지 영구 억제로 오분류할 위험이 있었다(리뷰16 M-3 지적, 이전엔 체인 전체를 하나의 `except AttributeError`로 감쌌음) | **"unsupported"로 분류**(리뷰11 M-2) — 재시도로 복구되지 않는 영구 장애이므로 일시적 DB I/O 실패("failed", 30분 백오프)와 구분한다. `on_blocked`와 동일한 형태로 장기 억제(반복 재시도 없음) + 1회 UI 알림("앱 업데이트 필요")하되, 억제 해제 판정은 **op_sig가 아니라 `compute_capability_sig()`(lancedb 버전 지문)**로 한다(리뷰14 M-1 — unsupported는 watch_folders 구성과 무관한 환경 문제라, op_sig 기준이면 폴더 구성이 그대로일 때 앱 업데이트 안내가 실제로는 억제를 풀지 못하는 결함이 있었다). 즉 **unsupported는 24시간 강제 reconciliation 예외** — 시간이 아니라 capability_sig 변화(앱/lancedb 업데이트)로만 재검증한다. 호환 전체-로드 모드 없음 — 요구와 모순되는 폴백 자체를 두지 않음(리뷰3 M-2). lancedb 0.34.0에서 실측 검증 완료(위 핵심 결정 참조) |
| **(리뷰19 M-2)** 메서드는 있지만 인자·반환 형태가 다른(호출 규약 비호환) lancedb 버전은 위 capability probe로 잡히지 않고 "failed"(일시 실패)로만 분류돼, 30분 백오프를 무기한 반복하며 purge가 계속 실패함 | 배포는 PyInstaller onedir(빌드 시점에 exe에 버전 고정, 사용자가 임의 변경 불가) | 서킷 브레이커 대신 예방으로 대응(ADR-0002 결과 참조): `requirements.txt`를 실측 검증 버전으로 **정확히 고정**(`lancedb==0.34.0` — 범위를 허용하면 실측하지 않은 0.34.x가 빌드에 섞여 고정의 목적이 무너진다, 리뷰20 M-1) + 앱 시작 시 `knowmate.lancedb_compat.check_lancedb_version()`이 번들 버전을 이 값과 대조해 다르면 로그 ERROR·트레이 알림(앱은 계속 실행 — 강제 종료할 만큼 확실한 장애 아님). 런타임 방어가 아니라 빌드 실수 조기 발견이 목적이라, 정상적인 빌드 절차를 따르면 이 실패 모드에 도달할 가능성 자체가 낮음 |
| purge 도중 일시적 예외 | purge 반환/예외 | failed_sig+next_retry_ts 기록, 백오프(기본 30분) 중 **DB 조회 없이 return**(판정 1·2가 성공 스킵보다 선행 — 리뷰3 B-1) |
| 대량삭제 차단 지속 | 차단 판정 | blocked_sig 기록 — 동일 op_sig 자동 재시도 안 함, 구성·차단율 변경 시에만 재실행, UI 알림 1회. 이 상태의 미복구는 FR-3 예외로 요구에 명문화(리뷰3 B-2) |
| last_purge_ts가 미래값 | now < last_purge_ts | 성공 스킵 무효 → purge 실행(모든 미래값 무효) |
| next_retry_ts가 유효 범위 밖(> now+설정백오프) | 로드 시 범위 검증 | 손상 취급 → 백오프 억제 해제(유효 범위 내 미래값은 **정상 억제** — 리뷰7 M-1로 문언 통일) |
| sidecar 의미적 손상(타입·범위 이상) | 로드 시 필드별 검증 | 메타 부재와 동일 취급 → 스킵 없이 purge 후 재생성(리뷰3 m-2) |
| purge 성공 후 meta 저장 실패 | sidecar replace 실패 로그 | **현재 프로세스는 성공 메타를 메모리 캐시로 즉시 반영해 정상 스킵을 지속**(매 사이클 재조회 방지) — 다음 사이클 재실행이 아니라 **재시작 후** sidecar가 구버전임을 보고 재실행된다(리뷰6 m-2로 확정, 리뷰10 m-2로 표 문언 통일). 삭제(file_path 기준)·optimize는 멱등이라 재실행돼도 안전 |
| 대량삭제 차단율(`max_delete_ratio`)이 비정상 값(NaN·범위 밖) | `purge_meta.is_valid_ratio` 검증 | fail-closed — 0.0(사실상 전체 삭제 차단)으로 대체 + ERROR 로그 + UI 알림. 조용히 기본값(0.30)으로 폴백하지 않는다(리뷰10 B-1) |
| `purge_force_reconcile_sec`/`purge_backoff_sec`이 비정상 값 | `purge_meta.is_valid_positive_seconds` 검증 | fail-open — 삭제 안전장치가 아니므로 안전한 기본값(24h/30분)으로 폴백 + 로그(리뷰10 B-1과 동일 근거로 도입, 심각도는 낮음) |
| 스킵 오판(purge 필요한데 스킵) | op_sig 비교 로직 테스트 | SHA-256 + 정규화 스냅샷(프로세스 간 안정, 리뷰 m-1 반영). 잔여 위험은 24h 강제 reconciliation이 상한 |
| sidecar 메타 파일 손상/유실 | 로드 실패 | meta 없음 = "스킵 불가"로 간주(보수적) → 그 사이클 purge 실행 후 재생성 |
| 장기 유휴로 스킵만 반복 | last_purge_ts 경과 | 24h 초과 시 0건이어도 강제 purge(리뷰 B-1 반영) |

### 검증 계획
- 사외: ① projection 결과에 vector/text 부재 검증 ② 스킵 조건 단위 테스트(서명 동일+0건+24h 미경과
  → 조회 스파이 미호출 / 서명·dry_run·차단율 변경 또는 24h 경과 → 호출) ③-a 일시적 예외 후
  백오프 동안 조회 없음·만료 후 1회 재시도 검증, ③-b 차단 후 동일 op_sig 조회 없음·op_sig 변경
  시 재실행 검증(성공 메타만 미갱신, 실패·차단 메타는 갱신됨 — 리뷰7 M-1) ④ meta 파일 부재/손상 시 purge 실행(보수적 폴백) ⑤
  watch_folder 제거 시나리오 회귀(기존 테스트 유지 통과) ⑥ op_sig가 경로 대소문자·구분자 차이에
  불변임을 검증 ⑦(리뷰10 B-1) `is_valid_ratio`/`is_valid_positive_seconds`에 NaN·Infinity·
  음수·범위 초과·bool·비숫자 케이스, `compute_op_sig`에 NaN/Infinity를 넣으면 `ValueError`인지,
  `max_delete_ratio=NaN`인 config로 전체 사이클을 실행했을 때 삭제가 전혀 일어나지 않고
  `indexing_needed` 알림이 발행되는지(fail-closed) 통합 검증.
- 사내 실측: 유휴 방치 1시간 동안 작업관리자 RSS 추이 — 수정 전(우상향 눌러앉음) 대비 평탄화 확인.
  lancedb 실환경에서 projection API의 컬럼 미로드(pushdown) 실측(리뷰 '확인 필요' 반영).
- **성능 수용 — 필수(리뷰5 m-2, 리뷰11 m-1로 승격, 리뷰12 m-2 → 리뷰13 M-3/m-1 → 리뷰14 M-3 →
  리뷰15 m-1로 절차 재정정)**: 결과 스키마에 벡터/원문이 없다는 사실과 약 6배 속도 향상은 강한
  정황이지만, projection이 저장소 스캔 단계에서 실제로 컬럼을 건너뛴다는 직접 증거는 아니다
  (리뷰11 m-1 지적) — 배포 전 아래 절차로 **필수** 확보한다. (리뷰12 m-2가 제시한 "동일
  프로세스에서 `ru_maxrss` 5회 반복 차분" 방식은 `ru_maxrss`가 프로세스 수명 **누적** peak라
  첫 회 이후 차분이 구조적으로 0에 수렴해 거짓 합격할 수 있음이 리뷰13 M-3에서 지적돼 폐기했다.)
  - 테스트 DB: 10만 행, file_path는 사내 실사용 경로 분포를 대표하는 길이(30~200자 구간에서
    표본화)로 생성. 제거 대상 경로를 테이블의 앞·중간·끝 위치에 각각 배치해 반환 행 수와
    삭제 후보 분류의 완전성도 함께 확인한다(리뷰13 m-1). **5개 측정 프로세스는 동일한 seed
    DB를 각자 별도 디렉터리로 복제해 단독으로 실행**한다(리뷰16 m-1) — 같은 DB를 순차
    재사용하면 첫 실행의 삭제·`optimize()`가 이후 실행의 행 수·삭제 후보·저장 구조를
    바꿔 초기 조건이 달라지고, 동시 접근은 LanceDB의 단독 writer·파일 락 가정과도
    충돌한다. 각 실행 전 행 수·스키마·제거 대상 수를 확인해 5개 복제본의 초기 조건이
    동일함을 기록한다.
  - 측정: **5개의 독립 프로세스**에서 각각 1회씩 실행한다(동일 프로세스 반복이 아님 — 리뷰13
    M-3). 각 프로세스의 warm-up은 **DB open과 스키마/행 수 조회(`count_rows()` 등)로만
    한정**하고 baseline RSS를 기록한다 — measurement 대상인 `file_path` projection 쿼리·Arrow
    순회는 warm-up에 절대 포함하지 않는다(리뷰14 M-3: 측정 대상과 동일한 전체 쿼리를
    warm-up에서 먼저 실행하면 CPython/pyarrow가 해제 후에도 OS에 반환하지 않는 메모리가
    baseline에 흡수돼, 이후 "max - baseline" 차분이 실제 첫 purge 비용을 과소평가하는
    거짓 합격을 만든다). baseline 확정 후 **두 단계를 분리 측정**한다(리뷰15 m-1 — 샘플링
    누락으로 인한 짧은 피크 오차와, projection/optimize 중 어느 단계가 메모리를 쓰는지 원인
    분석 불가 문제를 함께 완화):
    1. **projection + Arrow 순회 단계**(`file_path` 컬럼 projection 쿼리 → Arrow 컬럼 추출)만
       별도로 측정.
    2. 이어서 **전체 end-to-end**(1 + 소속 판정 + 삭제 대상 구성 + `optimize()`)를 측정.
    각 단계는 짧은 간격(10ms)으로 RSS를 샘플링해 `max(sampled_rss) - baseline_rss`를
    기록한다. RSS 측정은 `psutil.Process().memory_info().rss`(크로스플랫폼) 사용. 배포
    대상이 Windows 10/11이므로, **배포 전 릴리스 시험에서는 `psutil` 샘플링에 더해
    Windows OS 수준 프로세스 피크 카운터(`GetProcessMemoryInfo`의
    `PeakWorkingSetSize`)를 필수로 함께 기록**한다(리뷰17 m-2 — 샘플링 간격보다 짧게
    유지되는 피크를 놓칠 위험을 보조 측정이 아니라 필수 상호 검증으로 승격. 개발
    샌드박스 등 Windows가 아닌 환경에서 하는 사전 점검은 `psutil` 단독으로 충분). **최종
    NFR 합격 판정은 end-to-end(2) 결과로 한다** — 두 단계를 나눠 기록하는 목적은 판정이
    아니라, 불합격 시 projection 자체의 회귀인지 `optimize()`의 별도 비용인지 원인
    분석을 가능하게 하는 것.
    - 결과 검증: projection 반환 행 수가 실제 테이블 행 수와 일치하고, 앞·중간·끝에 배치한
      제거 대상 경로가 모두 삭제 후보로 분류되는지 확인(리뷰13 m-1 — 불일치 시 그 자체로
      불합격, 메모리 기준과 무관하게 회귀로 취급).
    - 5개 프로세스 측정값의 **중앙값과 최댓값을 함께 기록**한다. 합격 기준은 시험 결과를 본
      뒤 정하지 않고 사전에 다음으로 고정한다(리뷰17 m-2 — 이전엔 "예: 60MB"로 예시일
      뿐이라 사후 해석 여지가 있었음): **중앙값 ≤ 30MB, 그리고 5개 중 어느 측정값도
      60MB를 넘지 않아야** 합격(NFR-1 상한 충족). 두 수치 모두 예시가 아니라 확정 게이트다.
  - 가능하면 LanceDB의 query plan/스토리지 read 통계도 함께 기록해 컬럼 프루닝을 직접
    근거로 남긴다(보조 근거, 합격 판정의 필수 조건은 아님).

### 리뷰 이력
- reviews/REVIEW-20260724-...-projectio{,-2,...,-8}.md (GPT 채널 B, 8회) → 1~4·7차
  REQUEST_CHANGES 전건 처리(수용 위주, 일부 근거 기각), 5·6차 APPROVE_WITH_CHANGES 전건 수용,
  8차(PR #59 머지 후 도착) M-1 보류(후속 과제화)·나머지 수용 → Blocker/Major 미종결 0건,
  구현 완료 후 Accepted 승격 (2026-07-24)
- reviews/REVIEW-20260724-...-projectio-{9,10,11,12,13,14}.md (PR #60 구현 반영 중 재트리거,
  6회, 전건 REQUEST_CHANGES) — 9차 hard-exit logging.shutdown 제거(B-1), 10차
  max_delete_ratio fail-closed 검증·dirty-shutdown 마커 최초 도입(B-1/M-1), 11차 마커
  기록 위치를 hard-exit 직전에서 앱 시작 시로 재설계(B-1)·purge unsupported 분류(M-2),
  12차 마커 삭제 daemon 비동기화(B-1)·단일 인스턴스 확정 후로 마커 처리 순서 이동(M-1)·
  blocked_reason 필드 도입(m-1), 13차 마커 해제 시점을 app.exec() 정상 반환 후로 재이동
  (M-1)·성능 수용 절차를 독립 프로세스 psutil 샘플링으로 재작성(M-3), 14차 unsupported
  억제를 op_sig가 아닌 capability_sig(lancedb 버전) 기준으로 재설계(M-1)·마커 삭제
  daemon에 짧은 join 상한 추가(M-2)·성능 수용 절차의 warm-up 범위를 DB open까지로
  제한(M-3) — 전 라운드 Blocker/Major 전건 처리, 미종결 0건 유지
- reviews/REVIEW-20260724-...-projectio-{15,16}.md (2회, 전건 REQUEST_CHANGES) — 15차
  QThread.terminate() 성공을 정상 종료로 오분류하던 결함 수정(stop_worker가 terminate
  사용 여부 반환 → finalize_shutdown force_hard_exit로 전파, B-1)·capability_sig에
  앱 버전·projection 호출방식 버전 포함(M-1)·sidecar 필드/판정순서 의사코드 갱신(M-2)·
  성능 수용 절차를 projection 단계/end-to-end 단계로 분리 측정(m-1), 16차 stop_worker
  반환값 계약 변경을 컴포넌트 표에 반영(M-1)·unsupported 판정을 API 메서드 존재
  여부만으로 좁게 확인하도록 capability probe 도입(M-3)·성능 시험의 DB 격리(프로세스별
  seed DB 복제) 명시(m-1) — 전 라운드 Blocker/Major 전건 처리, 미종결 0건 유지
- reviews/REVIEW-20260724-...-projectio-17.md (APPROVE_WITH_CHANGES — 최초로 Blocker/Major
  없이 Minor만 도착) — hard_exit 콜백 계약을 `HardExit = Callable[[int], NoReturn]` 타입
  별칭으로 명시(m-1)·성능 수용 기준을 "예시" 문구 없이 중앙값 ≤30MB·개별 ≤60MB로 확정하고
  Windows 릴리스 시험에서 `PeakWorkingSetSize` 병행 측정을 필수로 승격(m-2). 이 라운드로
  Blocker/Major/Minor 전건 처리 완료, Accepted 재확인

---

## A-0003: 실패 분류 정교화 — 원인축과 누적축의 분리  (상태: Draft / 대응 요구: R-0003)

### 개요
6단계 요청은 분류를 다음 8종으로 세분화하는 것이다.

```
OPEN_ERROR · OPEN_ERROR_REPEATED · OPEN_TIMEOUT · READ_TIMEOUT
DRM_DENIED · PASSWORD_PROTECTED · SOURCE_CHANGED · NEEDS_USER_ACTION
```

이 설계는 요청의 **의도**(반복 Open 오류가 UNKNOWN_TRANSIENT에 머물지 않게)를 그대로 받되,
목록을 그대로 `kind` enum에 넣지 않는다. 목록에 **서로 다른 두 축이 섞여 있고**, 한 축을 다른
축의 값으로 표현하면 4차·#68에서 고친 버그가 재발하기 때문이다(§핵심 결정 1).

### 컴포넌트와 책임
| 컴포넌트 | 책임 | 변경 |
|---|---|---|
| `failure_state.classify()` | 예외 → (원인, HRESULT) | `failed_stage` 실제 사용 + 시그니처 판별 훅 |
| `failure_state.escalation_state()` | 레코드+정책 → 3상태 판정 | **신설**(단일 진실원 — UI·백오프가 공유, 리뷰2 M-2) |
| `failure_state.backoff_seconds()` | 원인+회차 → 대기 | `escalation_state()` 사용 |
| `app/bridge.py` | 화면용 카드 변환 | `escalation_state()` 사용 + 배지 매핑 추가 |
| ~~`secure/signature.py`~~ | ~~컨테이너 판별~~ | **6b로 이연**(리뷰2 B-2·M-3) |

### 데이터 흐름 — **6a (구현 계약)**
```
COM 실패
  ├─ 워치독 발화?  ─예→ stage로 OPEN_TIMEOUT / READ_TIMEOUT        (기존)
  └─ 아니오
       ├─ OfficeBusyError · busy HRESULT → TEMPORARY_BUSY           (기존)
       ├─ UnreadableFormatError          → NEEDS_USER_ACTION        (기존)
       └─ failed_stage 사용                                          (신규 — 기존엔 무시됨)
            ├─ open/dispatch → OPEN_ERROR
            ├─ sheets/cell_read/read → READ_ERROR
            └─ 단계 불명 → UNKNOWN_TRANSIENT                         (폴백 유지)

기록 시점: note_failure(...)가 (kind, normalized_stage) 기준으로 consecutive_failures 누적
표시·정책 시점: escalation = consecutive_failures >= 임계  (kind와 독립)
```

라우팅 전제(1차 리뷰 M-3 — 코드 확인 완료): `.xlsx`가 zip이 아니면 `secure/__init__.py:77`이
COM으로 폴백한다. 암호 문서는 COM Open이 더미 암호로 즉시 실패하며 그 예외가 `classify()`에
도달한다. 오라벨은 같은 경로로 들어가지만 **성공**하므로 분류 대상이 아니다.

**6a에서 이 파일들은 `OPEN_ERROR`로 기록되고 승격 경로를 탄다** — 원인을 세분하지 못해도
"반복 실패 중이니 확인 필요"는 사용자에게 전달된다. 원인 세분(암호/DRM)은 6b의 몫이다.

### 데이터 흐름 — 6b (후속 개념안, **구현 계약 아님**)

*아래는 6b 설계에서 확정할 방향의 스케치다. 이번 구현 대상이 아니며, 착수 전제조건은
위 §범위 분할을 따른다.*

```
OOXML 확장자 + COM 실패 (6a에서 OPEN_ERROR로 기록된 것 중)
  └─ 컨테이너 판별 (bounded traversal, 시간 예산 포함)
       ├─ CFB에 EncryptedPackage + EncryptionInfo → PASSWORD_PROTECTED  (양성 증거)
       ├─ 위 스트림 없는 OLE2                      → 오라벨 → OPEN_ERROR 유지
       ├─ ZIP도 OLE2도 아님                        → UNRECOGNIZED_CONTAINER
       └─ 예산 초과·판별 불가                       → OPEN_ERROR 유지(6a 결과)
```

### 범위 분할 — 6a(이번) / 6b(후속)  *(2차 리뷰 B-2·M-3 반영)*

2차 리뷰에서 컨테이너 판별의 실제 비용이 초안 추정보다 훨씬 크다는 것이 드러났다. 이에 따라
**컨테이너 판별을 6b로 분리하고, 6a는 판별 없이 완결되도록** 범위를 나눈다.

| | 6a (이번 구현 대상) | 6b (후속, 별도 설계) |
|---|---|---|
| 내용 | `failed_stage` 활용(OPEN_ERROR/READ_ERROR) · 승격 상태기계 · 연속성 키 · config | `PASSWORD_PROTECTED` · `DRM_DENIED` |
| 신규 I/O | **없음** | CFB bounded traversal |
| 신규 의존성 | 없음 | CFB 파서(vendoring 또는 고정 의존성) |
| 행오버 위험 | **없음** | 있음(M-3) |
| 분류 | 위 4종 + 기존 유지 | 위 2종 추가 |

분할 근거:
1. **원래 요청의 핵심은 6a로 완결된다.** "반복 Open 오류가 UNKNOWN_TRANSIENT에 머물지 않게"는
   `failed_stage` 활용 + 승격만으로 달성되며, 둘 다 순수 파이썬에 신규 I/O가 0이다.
2. **B-2**: CFB 디렉터리는 FAT 체인이라 첫 섹터만으로 부족하고, 섹터 크기도 512B 고정이 아니다
   (헤더 오프셋 `0x1E`의 sector shift). DIFAT·순환 체인·절단 입력까지 다루면 "30줄"이 아니다.
   초안의 비용 추정이 틀렸다.
3. **M-3**: 판별 I/O는 COM 워치독이 이미 해제된 뒤에 일어난다. SMB·DRM 드라이브에서 `open`/
   `read`가 블록되면 **수집기 QThread에 새 행오버 경로**가 생긴다. 바이트 수 상한은 시간
   상한을 보장하지 않는다 — 이 프로젝트가 COM 워치독을 따로 둔 이유와 같은 위험이다.
4. 6a를 먼저 배포하면 실측 `last_error_code` 분포가 쌓여, 6b에서 어떤 분류가 실제로 필요한지
   근거를 갖고 정할 수 있다(3차 주석의 "실측 로그가 쌓이면 그걸 보고 분류를 넓히는 게 순서다"와 동일).

6b 착수 전제조건(설계에 명시):
- 검증된 CFB 파서 확보(vendoring 또는 폐쇄망 미러 확인) — 직접 구현 시 sector shift·FAT/DIFAT·
  디렉터리 체인·순환/범위초과 sector ID·크기 검증을 포함한 bounded traversal 명세 필수
- 픽스처: 첫 섹터 밖 스트림 배치, 4096B 섹터, 순환·절단 체인
- 판별 I/O의 시간 예산·행오버 대응 정책(M-3 대안 1~4 중 택일)
- 예산 내 판별 불가 시 `UNRECOGNIZED_CONTAINER`로 떨어뜨리고 AC 보장 범위도 동일하게 한정
- 워치독 발화 시에도 판별을 수행할지(분류 우선순위) 확정 — 2차 리뷰 M-1

### 핵심 결정과 트레이드오프

**1. `OPEN_ERROR_REPEATED`를 `kind` 값으로 만들지 않는다 (가장 중요).**

`note_failure()`는 **분류가 달라지면 `consecutive_failures`를 1로 리셋**한다(4차 결정 —
TEMPORARY_BUSY가 여러 번 쌓인 뒤 우연히 타임아웃 1번 나면 곧장 긴 백오프로 튀는 것을 막기 위해).

여기에 `OPEN_ERROR → OPEN_ERROR_REPEATED` 전이를 넣으면:

| 회차 | kind | consecutive | 결과 |
|---|---|---|---|
| 1 | OPEN_ERROR | 1 | 30분 |
| 2 | OPEN_ERROR | 2 | 6시간 |
| 3 | OPEN_ERROR**_REPEATED** | **1로 리셋** | **30분** ← 승격했는데 백오프가 후퇴 |

승격시키려던 파일이 오히려 더 자주 재시도된다. #68에서 고친 "이력 손실로 백오프가 처음 칸으로
되돌아가는" 버그와 **정확히 같은 형태**다. 회피하려면 `note_failure`에 "이 두 kind는 같은
계열이니 리셋하지 말라"는 예외를 넣어야 하는데, 그건 kind가 사실은 원인축이 아님을 자백하는 것이다.

→ **채택**: `kind`는 원인만 표현. "반복됨"은 `consecutive_failures`에서 파생한다.
`OPEN_ERROR_REPEATED`가 필요한 곳(화면 배지·로그)에서는 `kind=OPEN_ERROR ∧ consecutive>=2`로
동일한 정보를 얻는다. 저장 필드가 늘지 않고, 리셋 규칙에 예외가 생기지 않는다.

트레이드오프: 요청받은 이름이 enum에 그대로 나타나지 않는다. 화면 표시 문자열로는 그대로 쓴다.

**2. `TEMPORARY_BUSY`와 `UNKNOWN_TRANSIENT`를 목록에서 빼지 않는다.**

요청 목록엔 둘 다 없지만 제거하면 회귀다.
- `TEMPORARY_BUSY`: "다른 사람이 열어둠"은 5~10분이면 자체 해소된다. OPEN_ERROR(30분~)로
  합치면 사용자가 파일을 닫아도 최대 30분을 기다린다.
- `UNKNOWN_TRANSIENT`: COM 실패 양상을 전부 열거할 수 없다. 폴백 버킷이 없으면 미매핑 오류가
  갈 곳을 잃는다. 3차 설계 주석의 "확실한 것만 분류하고 나머지는 UNKNOWN으로" 원칙을 유지한다.

**3. 암호는 컨테이너 내부의 양성 증거로만 판별한다. DRM은 음성 증거로 확정하지 않는다.**
*(초안 전면 수정 — 리뷰 B-1 수용)*

초안은 "OOXML 확장자인데 OLE2면 `PASSWORD_PROTECTED`"라는 8바이트 규칙을 제안했다. **이 규칙은
틀렸다.** 이 저장소가 이미 알고 있는 반례가 있다 — `signature.py` 모듈 docstring:

> 확장자가 OOXML(.docx/.xlsx/.pptx)이라도 실제 내용이 구형 OLE2 바이너리인 **오라벨** 파일을
> 가려내기 위함

오라벨 파일은 정확히 "OOXML 확장자 + OLE2 내용"이면서 **COM으로 정상적으로 열리는** 파일이다
(`secure/__init__.py:77`이 이 경우를 COM으로 라우팅하는 이유가 바로 그것이다). 8바이트 규칙은
오라벨 전부를 암호 문서로 오분류한다. 마찬가지로 "ZIP도 OLE2도 아님"은 DRM의 **양성 증거가
아니라 단순 미식별**이며, 손상·전송 중단된 부분 파일도 같은 곳에 떨어진다. 기존
`_is_drm_suspected()`가 이름에 "suspected"를 붙여 둔 것이 정확한 신중함이었는데, 초안은 그걸
단정형 `DRM_DENIED`로 승격시켰다.

수정안:

| 조건 | 판정 | 증거 성격 |
|---|---|---|
| CFB 디렉터리에 `EncryptedPackage` **및** `EncryptionInfo` 스트림 존재 | `PASSWORD_PROTECTED` | **양성** |
| OOXML 확장자 + OLE2인데 위 스트림 없음 | 오라벨 등 → 원인축 그대로(OPEN_ERROR 등) | — |
| ZIP도 OLE2도 아님 | `UNRECOGNIZED_CONTAINER` | 미식별(DRM 확정 아님) |
| 지원 DRM의 고유 매직 확인됨 | `DRM_DENIED` | **양성** (매직 확보 전까지 보류) |

> **구현은 6b로 이연됨** *(2차 리뷰 B-2 수용)*. 초안은 "CFB 헤더의 첫 디렉터리 섹터를 읽어
> 두 스트림 확인, 30줄이면 충분"이라고 적었으나 **이 비용 추정이 틀렸다.** CFB 디렉터리는
> FAT 체인으로 이어져 첫 섹터 밖에 엔트리가 놓일 수 있고, 섹터 크기도 512B 고정이 아니다
> (헤더 `0x1E`의 sector shift — 4096B 변형 존재). 첫 섹터만 보면 **정상적인 암호 문서를
> 놓친다**(AC-4 미충족). 올바른 구현은 DIFAT·순환 체인·절단 입력까지 다뤄야 하며, 그 자체로
> 별도 설계와 픽스처가 필요하다. 위 §범위 분할의 6b 전제조건 참고.

**`DRM_DENIED`는 이번 범위에서 보류한다.** 실제 지원 대상 DRM(나스카 등)의 안정적인 고유
시그니처 샘플을 확보하기 전에는 부여할 근거가 없다. 확보 전까지는
`UNRECOGNIZED_CONTAINER`로 기록하고, 승격 경로를 통해 사용자에게 노출한다.

한계(유지): 구형 `.xls`/`.doc`의 암호는 OLE2 내부 FilePass 레코드라 이 방식으로도 구분되지
않는다. OPEN_ERROR → 승격 경로를 탄다. 무리한 추측보다 정직한 미분류가 낫다.

**4. 실패 연속성 키를 `(kind, normalized_stage)`로 정의한다.** *(리뷰 M-2 수용)*

`OPEN_ERROR`는 여러 HRESULT를 하나로 묶으므로, `consecutive_failures` 누적 기준을 명시하지
않으면 두 방향 모두 오동작한다 — `kind`만 비교하면 서로 다른 Open 오류가 번갈아 나도 같은
원인으로 승격되고, HRESULT까지 엄격히 비교하면 같은 장애의 코드 변동으로 카운터가 계속
초기화된다.

→ 연속성 키 = `(kind, normalized_stage)`. `normalized_stage`는 `dispatch|open` → `open`,
`sheets|cell_read|read` → `read`로 정규화한 값. **HRESULT(`last_error_code`)는 진단 정보로만
보존하고 연속성 판정에 쓰지 않는다.** 특정 HRESULT군을 분리해야 할 근거가 실측으로 생기면
그때 정규화 테이블을 명시적으로 추가한다(회귀 테스트 동반).

**5. 승격 정책표 (확정 — 사용자 결정 B).** *(1차 M-1 / 3차 M-3 종결)*

사용자 결정: **"3회 이상 → 사용자 조치 필요"는 대기 시간까지 의미한다(안 B).** 원 요청 문언
그대로이며, 기존 사다리의 24시간 칸은 **의도적으로 제거**한다.

| 연속 실패 | 대기(백오프) | UI 상태 | 배지 |
|---|---|---|---|
| 1회 | 30분 | 일반 | 원인명 |
| 2회 | 6시간 | **반복 중** | 원인명 + "반복" |
| 3회 이상 | **7일**(안전밸브 상한) | **사용자 조치 필요** | "조치 필요" |

- 3회차에 대기가 24시간 → 7일로 바뀐다. 기존 4차 사다리 대비 **동작 변경**이며 의도된 것이다.
- 7일은 `needs_user_action_max_sec` 재사용 — 영구 무시 없음(FR-4), 경과 후 1회 재시도.
- 파일 변경·수동 재시도는 회차와 무관하게 즉시 반영(기존 동작). 7일을 기다릴 필요 없이
  [확인 필요한 문서] 화면의 「지금 다시 시도」로 즉시 재시도 가능하다(5차 기능).

임계값 2·3은 전부 config화한다(`escalation_repeat_at`, `escalation_action_at`).

**예외: `TEMPORARY_BUSY`는 이 사다리를 타지 않는다.** *(3차 리뷰 M-2 수용)*

3차 리뷰가 §2와 §5의 모순을 짚었다 — §2는 "`TEMPORARY_BUSY`를 OPEN_ERROR에 합치면 사용자가
파일을 닫아도 최대 30분 대기하는 회귀"라고 했는데, §5 표를 원인 구분 없이 적용하면 파일을
3번 연속 열어둔 사용자의 파일이 **7일 대기로 넘어간다**. 내가 회귀라고 지목한 상황보다
336배 나쁘다.

→ **백오프는 원인별 정책을 우선한다:**

| 원인 | 대기 정책 |
|---|---|
| `TEMPORARY_BUSY` | **항상 5~10분**(기존 유지, 회차 무관) — 사용자가 파일을 닫으면 곧 해소 |
| `FILE_CHANGED` | 항상 즉시(기존 유지) |
| `NEEDS_USER_ACTION` | 항상 7일(기존 유지) |
| `OPEN_ERROR`·`READ_ERROR`·`UNKNOWN_TRANSIENT` | 위 §5 사다리 적용 |

→ **UI 상태(`escalation_state`)는 모든 원인에 공통 적용한다.** 파일을 일주일째 열어둔
사용자에게 "이 문서가 계속 사용 중입니다 — 닫아주세요"를 보여주는 것은 유용하다. 즉
`TEMPORARY_BUSY`도 3회차부터 "조치 필요"로 **표시되지만 대기는 5~10분을 유지**한다.

이것이 §1의 "원인축과 누적축 분리"가 실제로 작동하는 방식이다 — 누적축은 **표시**를,
원인축은 **대기**를 지배한다.

**상태 판정은 단일 순수 함수로만 한다** *(2차 리뷰 M-2 수용)*. 초안의 컴포넌트 표는
`FailureRecord.escalated`(bool) 하나를 적었는데, 위 표는 `일반`/`반복 중`/`사용자 조치 필요`
**3상태**라 bool로 표현되지 않는다. UI(`bridge.py`)와 백오프(`backoff_seconds()`)가 각각
`consecutive_failures`와 임계값을 해석하면 같은 레코드를 서로 다르게 판정할 수 있다.

```python
def escalation_state(rec, policy) -> str:   # NORMAL | REPEATED | NEEDS_ACTION
```

`failure_state`에 이 함수 하나를 두고 UI·백오프가 **모두 이것만** 호출한다. config 교차 검증
(`0 < repeat_at < action_at`, 정수, bool 제외)도 이 모듈에서 하며, 위반 시 기본값 폴백
(4차 `BackoffPolicy.from_config` 관례).

**6. `FILE_CHANGED` 이름을 유지한다(확정). 개명하지 않는다.** *(리뷰 m-1 수용 — 권고를 결정으로)*

`load_failures()`는 `kind not in _ALL_KINDS`인 항목을 **조용히 버린다.** `SOURCE_CHANGED`로
개명하면 기존 기록이 전부 폐기되고, 다른 원인의 유효한 누적 이력까지 함께 사라진다. 개명 실익이
없으므로 **`FILE_CHANGED` 유지로 확정**한다. 하위호환을 위해 `load_failures()`에서
`SOURCE_CHANGED` → `FILE_CHANGED` 별칭 매핑만 둔다(외부에서 손으로 편집한 파일 대비).

분류 체계 변경은 `READ_STRATEGY_VERSION`이 아니라 **`SCHEMA_VERSION`**(이미 존재, 현재 1)을
쓴다. 리뷰 지적대로 추출 전략 변경과 분류 스키마 변경은 별개 축이며, 전자에 후자를 얹으면
분류만 바뀌어도 멀쩡한 추출 이력이 폐기된다.

**7. 승격 후에도 안전밸브를 유지한다.**

"3회 이상 → 사용자 조치 필요"를 문자 그대로 "재시도 중단"으로 구현하면 4차 완료 기준(영구
무시 없음)을 깬다. 그 기준은 세이프모드 루프 사건 — 우리가 만든 일시적 상태 때문에 멀쩡한
파일이 영구히 안 읽히던 사고 — 에서 나왔다. 승격 상태의 대기는 `needs_user_action_max_sec`
(기본 7일)을 상한으로 재사용한다.

### 실패 모드
**6a 경로만** — 신규 파일 I/O가 없으므로 I/O 관련 실패 모드는 이 범위에 존재하지 않는다.

| 상황 | 동작 | 근거 |
|---|---|---|
| `failed_stage`가 `None`(단계 미기록) | `UNKNOWN_TRANSIENT` 폴백 유지 | 기존 동작 — 폴백 버킷을 없애지 않는다(§2) |
| `failed_stage`가 미등록 문자열(신규 단계 추가 시) | `UNKNOWN_TRANSIENT` 폴백 | 정규화 테이블에 없는 값을 추측하지 않는다 |
| 승격 임계 config가 비정상값(`repeat_at >= action_at`·0·음수·bool) | 기본값 폴백 + WARNING | 4차 `BackoffPolicy.from_config` 관례(fail-open) — 잘못된 설정의 결과는 "예상보다 자주 재시도"여야지 "영구히 재시도 안 함"이면 안 된다 |
| 구 kind 값이 든 기존 기록 로드 | **해당 항목만** 폐기(기존 동작) | 파일 전체를 버리지 않는다 — 다른 원인의 유효한 누적 이력 보존 |
| `SOURCE_CHANGED` 별칭이 든 기록(수동 편집 등) | `FILE_CHANGED`로 매핑 | §6 — 폐기 대신 정규화 |
| 승격된 파일이 다음 시도에서 성공 | 기록 삭제(`note_success`) | 승격은 재시도 시점 조정이지 차단이 아님 |

*(6b의 실패 모드 — 시그니처 읽기 실패·CFB 절단/순환 체인·판별 I/O 행오버 — 는 6b 설계에서
다룬다. 6a에는 해당 경로가 없다.)*

### 검증 계획
- `classify()` 단위: 워치독 미발화 + `failed_stage="open"` → OPEN_ERROR (AC-1)
- 승격 전이: 동일 `(kind, stage)` 3회 실패 후 `consecutive_failures`가 3 유지 (AC-3 — #68 회귀 방어)
- 연속성 키(M-2): 같은 kind·같은 stage인데 HRESULT만 다른 실패가 **누적**됨 / stage가 다르면 리셋됨
- **상태 판정 단일화(리뷰2 M-2)**: `escalation_state()`가 임계 경계값(1/2/3/4회)에서 정확히
  NORMAL/REPEATED/NEEDS_ACTION을 반환하고, `bridge`와 `backoff_seconds`가 **같은 레코드에
  대해 같은 상태**를 보고하는지 교차 검증
- config 교차 검증: `repeat_at >= action_at`, 0·음수·bool 입력 시 기본값 폴백
- *(암호/DRM 판별 테스트는 6b 범위 — 이번 구현 대상 아님)*
- **통합 경로(M-3)**: `파일 → AutoReader 라우팅 → COM 실패 주입 → classify → note_failure →
  sidecar` 를 끝까지 통과시켜 최종 `FailureRecord.kind` 검증 (AC-7). 라우팅 함수는 실제로
  실행하고 COM 실패만 주입한다 — `classify()` 단위 테스트만으로는 라우팅 회귀를 못 잡는다.
- **UI 변환(1차 m-2)**: `bridge.getFailures()`가 `consecutive < 임계` / `>= 임계`를 각각 다른
  배지·문구로 변환하는지, **6a에서 실제 생성되는 원인**(`OPEN_ERROR`·`READ_ERROR`·
  `TEMPORARY_BUSY`·`UNKNOWN_TRANSIENT`)의 **원인 표시와 "조치 필요" 상태가 동시에 보존**되는지 (AC-2)
- **원인별 백오프 우선(3차 M-2)**: `TEMPORARY_BUSY`가 3회 이상 실패해도 대기는 **5~10분을
  유지**하면서 UI 상태만 NEEDS_ACTION으로 승격되는지 — §5 예외표 회귀 방어
- **B안 정책값(3차 M-3)**: 3회차 대기가 24시간이 아니라 **7일**인지(사용자 결정 B 고정)
- 안전밸브: 승격 상태 + 7일 경과 → `should_defer` False (AC-5)
- 하위호환: 구 kind 값·`SOURCE_CHANGED` 별칭이 든 JSON 로드 시 예외 없음 (AC-6)
- 원칙7 회귀 테스트 유지(예외 메시지 비저장)

### 남은 확인 사항
1. ~~**승격 해석 확정(사용자)**~~ — **종결(2026-07-28)**: 사용자 결정 **안 B** — 3회차부터
   대기 시간 자체를 7일로. §5 정책표 확정 완료.
2. ~~**R-0001 제약과 A-0001 계약의 모순**~~ — **종결(2026-07-28)**: 사용자 승인 후 PR #71로
   제약 문언을 "호출 방식 호환 유지(반환값 추가는 허용)"로 정정, 머지 완료. (원 기록) 2차 리뷰 B-1. R-0001 제약은
   "기존 `stop_worker` 인터페이스 불변"인데 A-0001은 "반환값 계약이 변경됨(리뷰15 B-1)"이라고
   명시한다. 문서로 확인한 결과 **실재하는 모순이 맞다.** 다만 ① 6단계와 무관한 기존 Accepted
   문서이고 ② R-0001은 Approved라 제약 문언 수정에 사람 승인이 필요하므로, 이 PR에서 임의로
   고치지 않는다. 제안: R-0001 제약을 "기존 **호출 방식** 호환 유지(반환값 추가는 허용)"로
   수정 — bool 반환은 리뷰15 B-1로 이미 승인·구현된 변경이고 호출부가 무시해도 동작하므로,
   제약 문언 쪽이 stale하다. 별도 PR로 처리 권고.
3. **DRM 고유 시그니처 샘플 미확보** — 확보 전까지 `DRM_DENIED` 부여를 보류(1차 리뷰 B-1
   대안 2). 6b 착수 전제조건에 포함.
4. **`IFR_PM_0066.xlsx`의 HRESULT·failed_stage** — 전용 분류 추가 판단용 운영 근거로는
   가치가 있으나 6a의 선행 조건은 아니다(양차 리뷰 모두 동일 판단).
5. **`SCHEMA_VERSION` 증가 시 동작 명세**(2차 리뷰 확인 필요) — 항목별 변환이 아니라
   **알려진 kind는 보존, 미등록 kind만 항목 단위 폐기**로 한다(기존 `load_failures` 동작 유지).
   파일 전체 폐기는 하지 않는다 — 다른 원인의 유효한 누적 이력까지 잃기 때문. 6a 구현 시
   테스트로 고정한다(AC-6).
6. **R-0003이 Draft인 채 A-0003을 진행한 것** — 의도적이다. 요구 탐색과 설계를 같은 PR에서
   돌려 리뷰를 한 번에 받되, **구현은 사람 승인 후** 착수한다.

### 리뷰 이력
- 2026-07-28 GPT 독립 리뷰 1차(채널 B, gpt-5.6-sol) — **REQUEST_CHANGES**.
  B-1(Blocker) 전면 수용해 §3 재작성, M-1·M-2·M-3·m-1·m-2 전건 수용.
  처리 기록: `reviews/REVIEW-20260728-architecture-requirements.md`
- 2026-07-28 GPT 독립 리뷰 3차 — **REQUEST_CHANGES**. M-2(TEMPORARY_BUSY ↔ 승격 사다리 모순)를
  수용해 §5에 원인별 백오프 우선 예외표 추가. M-1·m-1 수용해 데이터 흐름·실패 모드·검증
  계획을 6a 전용으로 재작성하고 6b는 "구현 계약 아님" 절로 분리. M-3은 **사용자 결정(안 B)**
  으로 종결 — 3회차부터 대기 7일. B-1(R-0001 모순)은 별도 PR #71로 해소·머지 완료.
  처리 기록: `reviews/REVIEW-20260728-architecture-requirements-3.md`
- 2026-07-28 GPT 독립 리뷰 2차 — **REQUEST_CHANGES**. B-2(CFB 첫 섹터 한계)·M-3(판별 I/O
  행오버)을 수용해 **컨테이너 판별을 6b로 분리**하고 6a를 신규 I/O 0으로 확정. M-2 수용해
  `escalation_state()` 단일 함수 도입. B-1(R-0001↔A-0001 모순)은 실재 확인했으나 별건으로
  분리(위 남은 확인 사항 2번). N-1 수정.
  처리 기록: `reviews/REVIEW-20260728-architecture-requirements-2.md`
