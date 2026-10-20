# PPT 종료 수집 계획 독립 검토 — A / round 1

판정: **계획 보완 필요**. 미해결 Critical 0 / High 0 / **Medium 2** / Low 0.
Windows·Office가 없는 현재 환경의 미실행 gate는 이 두 설계 지적과 별도이며, 실행·배포 승인을 하지 않는다.

## 검토 대상과 범위

- 전체 계획: [PPT_SHUTDOWN_CAPTURE_PLAN r1 보존본](plan-r1.txt), **r1 / 2026-10-20**.
- 검토 시작·종료 시 SHA-256: `07f5a939848fa90afe89bd113683f6ed1c5579779a4cfed66a902275432dc322`.
- 기준 소스: `brovior/know-Mate` main `b75c98511cea3ddb7d842c7204dd11b4f9767ea3`의 제공된 부분 스냅샷.
- 검토자는 계획 작성에 참여하지 않았다. 전체 r1과 실제 소스의 호출·수명·기록 경계를 독립 대조했다.
- 제품 코드·계획 원문·설치 상태는 수정하지 않았다. 이 리뷰 문서만 작성했다.

시작 지침은 `AGENTS.md`, `CLAUDE.md`, `docs/dev/document_guidelines.md`를 읽었다.
부분 스냅샷에 없는 `ROADMAP.md`, `response_style.md`, `karpathy_guidelines.md`는 연결된 GitHub 읽기 도구로
위 기준 commit에서 보충했다. `plan-review-loop` 본문과 그 심각도·종료 기준을 적용했다.

대조한 정본은 `docs/DIAGNOSTICS.md`의 PPT 종료·identity 관측 절,
`docs/guides/PPT_CHECKPOINT_HANDOFF.md`, `docs/design/PPT_ADDIN_INDEXING.md`의 자동종료 계약이다.
주요 소스는 controller observation/adapter/report, C# host observation·bootstrap·guard·trace·lifecycle·connection lease,
`PptEventSink.cs`, checkpoint121의 잠긴 runner·handshake·OS observer, `knowmate/secure/ppt_shutdown.py`다.

## A-R1-01 — Medium — callback 중첩과 기록기 재진입, 누락 판정의 경계가 불명확함

**발생 조건:** `TryQuitIfIdle`/native Quit이 아직 반환하지 않은 동안 같은 owner STA에서 Cancel 또는
OnBeginShutdown/OnDisconnection callback이 중첩 진입한다. §5의 “off-thread/reentrant 기록”을
service/guard/event depth가 0보다 큰 모든 중첩 callback으로 구현하면 해당 callback의 entry/exit와
cancel_in/cancel_out를 수치 하나로 버리게 된다. 실제 slot 재진입만 뜻하더라도 offthread·중첩 누락 수치가
0이 아닐 때 evidence_loss와 범위 완결 판정을 어떻게 바꾸는지 정해져 있지 않다.

**원자료 근거:**

- 계획 r1 94행은 reentrant 기록을 별도 누락/중첩 수치로 처리한다고 하고, 96행은 overflow에만 명시적으로
  완결 실패를 연결한다. 102행의 evidence_loss 조건과 §9의 fake/native 검증에도 offthread/reentrant 누락 판정이 없다.
- `scripts/ppt_reader/PptExternalGuard.cs` 65~73행은 state.Active와 guard 진입이 유지된 상태에서 Quit을 호출하고,
  반환 후 Cancelled를 결과로 사용한다. 따라서 이 구간의 callback은 본 진단의 핵심 증거다.
- `scripts/ppt_events/PptEventSink.cs` 19~24행은 그 callback 안에서 Cancel을 설정하고 기록한다.
- `scripts/ppt_reader/PptReader.cs`의 OnBeginShutdown/OnDisconnection과 `externalEntryDepth` 처리는
  바깥 서비스 호출이 끝나기 전 lifecycle이 진입하는 상황을 이미 고려한다.

**요구 변경:** 정상적인 COM/Office callback 중첩과 “slot append 함수 자체가 아직 쓰는 중인 재진입”을 구분한다.
owner STA의 중첩 callback은 depth를 담은 정상 record로 보존하고, 동시/재귀 append를 안전하게 기록할 수 없어 버릴 때만
별도 누락 counter를 사용한다. 해당 누락·offthread counter가 0이 아니거나 그 값을 검증할 수 없으면 영향받는 범위를
완전하다고 판정하지 않으며, 누락된 callback을 미실행으로 표시하지 않는 규칙을 schema/coverage에 명시한다.
정확한 수가 아닌 하한 counter를 쓰면 그 의미도 명시한다. guard의 동작은 바꾸지 않는다.

**재검증 기준:** fake에서 Quit 안의 Cancel 및 lifecycle callback을 중첩 실행했을 때 순서·depth·취소 귀속이 남아야 한다.
별도로 append 자체 재진입/offthread/포화 기록을 주입해 누락 수치가 summary·coverage의 unknown/evidence_loss로
전파되고, 미실행 판정이 나오지 않음을 확인한다. x86/x64 native 시험은 publish 도중 재진입 및 counter 가시성을 다룬다.

## A-R1-02 — Medium — controller 참조를 놓는 시점과 실제 관측 구간이 정해지지 않음

**발생 조건:** Quit은 정상 반환했지만 controller가 app/service/중간 wrapper나 예외 traceback을 보유한 채
180초 OS 관측을 기다리거나, 이전 runner의 30초 service 보유 절차를 재사용한다.
이 경우 같은 “Quit 반환 후 PPT 생존” 결과가 서로 다른 참조 보유 조건에서 만들어진다.
정리 기록을 남긴다는 요구만으로는 기본 실행의 정리 시점이 고정되지 않으며, 마지막에 해제하면
참조를 놓은 뒤의 충분한 관측 시간이 없을 수 있다.

**원자료 근거:**

- 계획 r1 §3은 요청 시작부터 OS를 감시하고 §6~7은 참조 정리 begin/return을 기록하지만,
  정상 요청 반환과 COM wrapper 해제/CoUninitialize 사이의 순서·대기 정책을 명시하지 않는다.
- `scripts/diagnostics/ppt_checkpoint121/run.py` 186~205행은 정상 완료 뒤 service/app을 유지한 30초 관측,
  service drop, app drop, caller 종료 순서다. 이 runner는 잠겨 있고 새 runner는 별도 작성하므로 이 순서를
  기본 계약으로 묵시 상속해서는 안 된다.
- `knowmate/secure/ppt_shutdown.py` 292~293행의 `observe_exit`는 worker 참조를 해제한 뒤 실제 종료를 확인하는 계약이다.
- `docs/DIAGNOSTICS.md`의 113 외부 시험은 참조를 보유해도 종료된 과거 사례를 제공하지만,
  모든 새 실행에서 참조 보유가 무관하다고 증명하지 않는다.

**요구 변경:** 새 기본 실행에서는 normal return/거절/확정 실패 뒤 추가 COM 관측 없이 해당 apartment의
자기 소유 COM wrapper·중간 보유·실패 예외 보유를 정리하고 CoUninitialize를 시도하는 순서를 명시한다.
의도적인 service/app 30초 보유와 강제 GC를 기본 경로에 들여오지 않는다. 외부 감시는 요청 시작부터 계속하되,
`request_return`, `controller_refs_drop_begin/end`, `CoUninitialize_begin/end`, caller exit와
실제로 확보한 “정리 완료 후 OS 관측 시간”을 별도 기록한다. COM 호출 자체가 미반환이거나 정리가 멈추면
외부 수집만 마무리하며 post-cleanup 관측을 확보한 것으로 세지 않는다. observer 180초 상한을 자동 연장하지 않는다.

**재검증 기준:** fake에서 internal normal return과 external COMPLETE 후 정리 순서·보유 대기 없음·후속 COM 0을 확인한다.
참조 정리/CoUninitialize가 반환하지 않는 경우와 deadline 직전 정리된 경우에도 외부 수집이 끝나고,
post-cleanup 관측 시간 및 미확보 사유가 정확히 남아야 한다. 별도 Windows/Office 시험에서 실제 caller 종료와
retained target handle의 종료를 독립 관측한다. wrapper drop을 서버 전체 native refcount 해제 증명으로 보고하지 않는다.

## 확인된 보존 조건

- 선택 COMAddIn.Application을 생략해도 첫 unknown/fatal 이후 COM 중단·기존 complete_host·identity/ownership/
  cleanup/generation·export gate를 유지한다. 현행 결함을 단순 무시하는 계획은 아니다.
- internal과 external을 별도 새 실행으로 나누고, 기존 host guard·process-wide one-shot fence를 유지한다.
- ThreadPool export의 한계와 legacy frame의 request 범위 한계를 인정하며, 외부 retained mapping/handle로
  target 종료와 증거 보존을 분리한다.
- 권한·pin·ABI·native writer·Office·사내 DRM의 미실행 gate를 계획 승인으로 대체하지 않는다.

## 검증한 항목과 미실행 항목

수행: 전체 r1 정적 검토, 실제 코드/정본 대조, 검토 대상 SHA-256 일치 확인, 기준 commit의 누락 지침 read-only 조회,
이 문서의 파일·링크·공백 검사. Git checkout이 없어 `git diff --check`는 실행하지 않았다.

미실행: 계획의 구현, pytest/fake 회귀 실행, x86/x64 Windows native transport/ACL/ABI/WCT,
빌드·패키지·pin 생성, 설치·등록/UAC, 실제 Windows·Office 합성 실행, 사내 Office/DRM·배포 시험.
과거 보고된 PASS 수치를 새 검증 결과로 재사용하지 않았다.

**종료 기준 미충족:** r1 미해결 Critical 0 / High 0 / Medium 2 / Low 0.
작성자가 판단·보완한 다음 revision의 전체 계획을 다시 읽고 두 지적과 새 문제를 재검증해야 한다.
