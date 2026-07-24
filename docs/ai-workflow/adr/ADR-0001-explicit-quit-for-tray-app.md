# ADR-0001: 트레이 상주 앱의 종료를 명시적 quit()으로 전환

| 상태 | 날짜 | 결정자 | 리뷰 |
|---|---|---|---|
| Accepted | 2026-07-24 | Claude (Chief Architect) | reviews/REVIEW-20260724-adr-0001-explicit-quit-for-tray-app-adr-0002-purge-projectio-*.md (1~16차, Blocker/Major 전건 처리·구현 반영, 리뷰11 M-1 축소채택·리뷰12 marker 비동기화/단일인스턴스 순서 정정·리뷰13 marker 해제 위치 재이동·리뷰14 unsupported capability_sig 재설계/marker join 상한 추가·리뷰15 terminate() 강제종료 시 hard_exit 강제/capability_sig 확장·리뷰16 stop_worker 계약 문서화/unsupported capability probe 좁힘 근거 명시) |

## 맥락 (Context)
- 베타 실사용에서 트레이 [종료] 후 `AegisDesk.exe`가 항상 잔존한다. 트레이 아이콘은
  `_shutdown()`에서 먼저 숨겨져 겉보기엔 종료로 보인다.
- 코드에는 `QApplication.quit()` 호출이 없고, 이벤트 루프 종료를 Qt 기본값
  `quitOnLastWindowClosed=True`에만 의존한다.
- Qt6의 `lastWindowClosed`는 "마지막으로 **보이는** 창이 닫힐 때" 발생한다. 트레이 상주
  상태(창이 `hide()`됨)에서 `close()`를 호출하면 보이는 창이 닫히는 사건이 없어 신호가
  오지 않고 `app.exec()`가 반환되지 않는다. 트레이에서 종료하는 시점은 대부분 창이 숨겨진
  상태이므로 "항상 재현"과 부합한다.
- 2026-07-23의 종료 확실화(`lifecycle.stop_worker`)는 워커 행오버 시나리오 전용
  안전망(`isRunning()`일 때만 진입)이라 이 문제를 커버하지 않는다 — 별개의 구멍이다.

## 결정 (Decision)
`main()`에서 `app.setQuitOnLastWindowClosed(False)`를 설정하고, 모든 종료 경로가 수렴하는
`MainWindow._shutdown()`의 마지막 단계에서 종료를 **반드시** 완수한다: 앞 단계(스케줄러 정지·
트레이 정리·`stop_worker`)의 예외와 무관하게 마지막 판정에 도달하며, **워커 비실행이 확인되고
`terminate()`가 쓰이지 않았으면** `QApplication.instance().quit()`, **워커가 여전히 실행 중이거나
실행 여부 판정이 불가하거나 `terminate()`로 강제 중단됐으면**(isRunning 조회 예외 포함) 보수적으로
`hard_exit`(os._exit 래퍼)를 호출한다 — quit과 hard_exit는 정확히 하나만 실행된다. `terminate()`
사용 여부를 "정상 종료 확인"에서 제외한 이유는 리뷰15 B-1 — `QThread.terminate()`는 스레드를
임의 지점에서 강제 중단하므로, 그 결과 `isRunning()`이 False가 되어도 로깅 핸들러 락·LanceDB
파일 락 등을 쥔 채 죽었을 가능성을 배제할 수 없어 "정상 종료"로 볼 수 없다. `stop_worker()`가
`terminate()` 사용 여부를 반환하고, `_shutdown()`이 이 값을 `finalize_shutdown(force_hard_exit=)`로
전달한다. 기존 `stop_worker` 에스컬레이션(정상→terminate→`os._exit`) 구조는 유지하며,
`_shutdown()`의 이 최종 분기는 stop_worker가 예외로 이탈한 경우까지 커버하는 마지막 안전망이다.

## 검토한 대안 (Alternatives)
| 대안 | 장점 | 단점 | 기각 사유 |
|---|---|---|---|
| `_quit_app`에서 `close()` 대신 직접 `_shutdown()+quit()` | closeEvent 우회로 단순 | X(close_action=quit) 경로와 종료 로직이 이원화, closeEvent와 이중 실행 가드 필요 | 종료 경로 수렴점(_shutdown)이 흩어짐 |
| [종료] 직전에 창을 `show()` 후 `close()` | 코드 최소 | 종료 순간 창이 깜빡 나타나는 UX 결함, Qt 내부 타이밍 의존 | 우회책이며 근본 원인 미해결 |
| `os._exit`를 모든 종료에 사용 | 확실히 죽음 | state 저장·로그 flush 등 정상 정리 생략, 데이터 무결성 위험 | 최후 안전망(행오버)에만 한정해야 함 |
| `quitOnLastWindowClosed=True` 유지 + quit()만 추가 | 변경 최소 | 창만 닫아도(close_action=tray 인데 트레이 불가 환경 등) 의도치 않은 종료 여지 | 트레이 앱 표준 관용구(False+명시 quit)가 예측 가능성 높음 |

## 결과 (Consequences)
- 좋아지는 것: 창 가시성과 무관하게 [종료]가 항상 프로세스를 끝낸다. 좀비 프로세스로 인한
  메모리 점유·단일 인스턴스 가드 오작동(좀비가 창만 다시 띄움)도 함께 해소된다.
- 감수하는 것: 이후 추가되는 모든 종료 경로는 반드시 `_shutdown()`을 경유해야 한다(미경유 시
  루프가 안 끝남). 암묵 종료가 꺼지므로 "마지막 창 닫힘 = 종료"에 기대는 코드는 금지된다.
- 후속 조치: `_shutdown()`의 `quit()` 호출을 앞 단계 예외와 무관하게 실행되도록 배치(try/finally
  또는 독립 try). 사외 단위 테스트(quit 콜백 주입 스파이) 추가. 실기 3경로(숨김/표시/close_action=quit) 검증.
