# PPT 종료 수집 계획 독립 재검토 — A / round 2

판정: **r2에서 기존 두 설계 지적 해소, 새 지적 없음**.
미해결 Critical 0 / High 0 / Medium 0 / Low 0으로 이 검토자의 계획 재검증 종료 기준을 충족한다.
구현·자동/native 시험·실제 Windows/Office 실행·배포 승인은 아니다.

## 검토 대상과 근거

- 전체 계획: [r2 보존본](plan-r2.txt), **r2 / 2026-10-20**.
- 기준 문서: [PPT_SHUTDOWN_CAPTURE_PLAN.md](../../design/PPT_SHUTDOWN_CAPTURE_PLAN.md).
- 검토한 전체 r2의 SHA-256: `aca9def288c72d6ac2f3304055f140a6c7570d011cfc26753eafc1313405d656`.
- 기준 소스: `brovior/know-Mate` main `b75c98511cea3ddb7d842c7204dd11b4f9767ea3`의 부분 스냅샷.
- 이전 결과와 처리 근거: [A round 1](reviewer-a-round1.md), [r2 처리 기록](dispositions-r2.md).

변경 목록만으로 판정하지 않았다. r2 전체를 다시 읽고 정본 `DIAGNOSTICS.md`의 PPT 관측 절,
`PPT_CHECKPOINT_HANDOFF.md`, `PPT_ADDIN_INDEXING.md`의 자동종료 계약 및 실제 코드의 관련 경계를 다시 대조했다.
기존 지침과 source 제약은 round 1과 같다. r1/r2 차이는 마지막에 별도로 확인했다.

## 항목별 재판정

| 기존 ID·심각도 | 재판정 | r2 전체 계획과 원자료 대조 결과 |
|---|---|---|
| A-R1-01 / Medium | **해소** | §5가 정상 owner-STA Cancel/lifecycle callback 중첩을 entry/exit·depth를 포함한 정상 record로 보존한다. append 함수 자체 재진입과 구분하고 COM·callback·메시지 pump를 append 안에서 금지한다. 누락 counter의 비영·불일치·조회 불가와 하한 의미를 evidence_loss/unknown으로 연결하며 §6·§9에 summary와 fake/native 기준을 반영했다. guard 상태 변경으로 해결하지 않는다. |
| A-R1-02 / Medium | **해소** | §7이 normal return/거절/확정 실패 직후 별도 보유 대기 없이 자기 소유 wrapper·인수·closure/cache·예외/traceback 보유를 해제하도록 정했다. external은 기존 COMPLETE 다음 정리, internal은 COMPLETE 없이 정리하며, 성공한 COM 초기화와 같은 apartment에서 균형 잡힌 CoUninitialize를 시도한다. 정리 완료 시각과 caller OS exit·post_cleanup_observed_ms를 분리하고 미반환/늦은 완료는 실제 확보 범위만 보고한다. §9에 검증 기준도 반영했다. |

A-R1-01의 기준 코드는 `PptExternalGuard.cs`의 Active/entered 상태에서 수행하는 Quit,
`PptEventSink.cs`의 Cancel 설정과 `PptReader.cs`의 중첩 lifecycle 처리다. r2는 그 동작을 보존하면서
계측의 누락 의미를 명확히 한다.
A-R1-02는 checkpoint121 `run.py`의 service/app 30초 보유 및 정리 순서와
`knowmate/secure/ppt_shutdown.py::observe_exit`의 참조 해제 후 실제 종료 관찰 계약을 다시 대조했다.
기존 runner를 새 기본 절차로 복제하지 않는다는 요구가 명시됐으며, wrapper drop을 전체 native 해제 증명으로 승격하지 않는다.

심각도를 낮추거나 범위를 줄여 닫은 항목은 없다. 위 두 항목은 계획의 누락이 해소된 것이며 구현 시험에 통과했다는 뜻은 아니다.

## 전체 계획 재검토

- **요청·권한:** 새 target 하나와 경로 하나, Quit 최대 1회, 거절/unknown 뒤 추가 관측 RPC·재시도 금지,
  기존 complete_host·동일 identity·ownership/cleanup·generation·export gate 및 process-wide fence가 유지된다.
  `ppt_identity_controller.py`, `PptExternalObservationEntry.cs`, `PptExternalConnect.cs`와 대조했다.
- **증거·판독:** 현 `ExternalTrace.ExportTerminal`의 ThreadPool/terminal 잠금 한계를 유지해 해석하고,
  새 외부 보존·단방향 publish·누락 counter·최종 drain·legacy 제한을 구분한다.
  요청 반환, capture 종료, target OS exit를 같은 결과로 합치지 않는다.
- **수명·취소:** 정리 및 CoUninitialize 미반환이 독립 수집의 완료를 막지 않으며 timeout을 프로세스 종료로
  바꾸지 않는다. 기존 guard의 사용자 문서 보호와 소유 참조 정리, 정상 경고, 강제 종료 금지가 유지된다.
- **배포·정보 보호:** 본문·임의 COM 오류 설명·창 제목·dump는 수집하지 않고 allowlist/pin/새 결과 디렉터리를 사용한다.
  새 진단 버전과 기존 protocol/COM ABI를 구분하며 default/released 비활성, checkpoint121 잠금과 별도 live gate를 유지한다.

이 범위에서 추가 Critical/High/Medium/Low 지적은 발견하지 않았다. 한 실행으로 모든 원인을 확정한다는 보장은 없고,
증거가 부족한 범위는 unknown으로 남긴다는 계획의 제한도 적절하다.

## 수행한 검증과 미실행 gate

수행: 전체 r2 정적 재검토, 원자료·정본 재대조, r1/r2 차이 확인, r2 보존본/현재본의 hash 일치 확인,
리뷰 문서의 상대 링크·공백·코드 블록 검사. r1 리뷰의 대상 링크는 내용이 고정된 r1 txt 보존본으로 연결했다.
Git checkout이 없어 `git diff --check`는 수행하지 않았다.

미실행: 제품 구현, pytest/fake 회귀, Windows x86/x64 shared mapping·counter 가시성·ACL·ABI·WCT 시험,
전체 회귀/빌드·새 package/pin, 설치·등록/UAC, 실제 Windows/Office 합성 시험, 사내 Office/DRM·배포 시험.
현재 환경에는 Windows·Office·완전 checkout과 pytest가 없으며, 과거 PASS를 이번 결과로 재사용하지 않았다.
필수 사전 시험 실패/미실행 시 live/배포를 보류하는 §9 gate는 그대로 남는다.

**최종 미해결: Critical 0 / High 0 / Medium 0 / Low 0.**
프로세스 종료·권한에 영향이 있는 후속 설계이므로 `plan-review-loop`의 새 검증자 최종 확인은 별도 기록이어야 한다.
이 검토자는 r1에 참여했으므로 그 새 검증자 수에 포함되지 않는다. 제품 코드와 계획 원문은 수정하지 않았다.
