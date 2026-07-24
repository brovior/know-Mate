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
| `lifecycle.stop_worker()` | 행오버 워커 에스컬레이션(기존 유지, 변경 없음) | `knowmate/app/lifecycle.py` |

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
      unsupported(projection API 비호환, `AttributeError`): meta["blocked_sig"]=op_sig;
                 meta["blocked_reason"]="unsupported"; meta["blocked_capability_sig"]=capability_sig
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
| projection API가 배포 고정 lancedb 버전에 없음 | `table.search().select([...])` 호출이 `AttributeError` | **"unsupported"로 분류**(리뷰11 M-2) — 재시도로 복구되지 않는 영구 장애이므로 일시적 DB I/O 실패("failed", 30분 백오프)와 구분한다. `on_blocked`와 동일한 형태로 장기 억제(반복 재시도 없음) + 1회 UI 알림("앱 업데이트 필요")하되, 억제 해제 판정은 **op_sig가 아니라 `compute_capability_sig()`(lancedb 버전 지문)**로 한다(리뷰14 M-1 — unsupported는 watch_folders 구성과 무관한 환경 문제라, op_sig 기준이면 폴더 구성이 그대로일 때 앱 업데이트 안내가 실제로는 억제를 풀지 못하는 결함이 있었다). 즉 **unsupported는 24시간 강제 reconciliation 예외** — 시간이 아니라 capability_sig 변화(앱/lancedb 업데이트)로만 재검증한다. 호환 전체-로드 모드 없음 — 요구와 모순되는 폴백 자체를 두지 않음(리뷰3 M-2). lancedb 0.34.0에서 실측 검증 완료(위 핵심 결정 참조) |
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
    삭제 후보 분류의 완전성도 함께 확인한다(리뷰13 m-1).
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
    각 단계는 짧은 간격(예: 50ms 이하, 가능하면 10ms)으로 RSS를 샘플링해
    `max(sampled_rss) - baseline_rss`를 기록한다. 샘플링 간격보다 짧게 유지되는 피크를 놓칠
    수 있음을 감안해, 가능한 플랫폼(Windows)에서는 OS 수준 프로세스 피크 카운터(예:
    `PeakWorkingSetSize`)를 함께 기록해 샘플링 오차를 상호 검증한다. **최종 NFR 합격 판정은
    end-to-end(2) 결과로 한다** — 두 단계를 나눠 기록하는 목적은 판정이 아니라, 불합격 시
    projection 자체의 회귀인지 `optimize()`의 별도 비용인지 원인 분석을 가능하게 하는 것.
    RSS 측정은 `psutil.Process().memory_info().rss`(Windows 포함 크로스플랫폼) 사용.
    - 결과 검증: projection 반환 행 수가 실제 테이블 행 수와 일치하고, 앞·중간·끝에 배치한
      제거 대상 경로가 모두 삭제 후보로 분류되는지 확인(리뷰13 m-1 — 불일치 시 그 자체로
      불합격, 메모리 기준과 무관하게 회귀로 취급).
    - 5개 프로세스 측정값의 **중앙값과 최댓값을 함께 기록**하고, **중앙값 ≤ 30MB이며 개별
      측정값 중 어느 것도 명시적 단회 상한(예: 60MB)을 넘지 않아야** 합격(NFR-1 상한 충족).
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
