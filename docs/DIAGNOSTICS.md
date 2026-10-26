# 성능·크래시 진단과 개선 후보

현재 원인 분석과 다음 검증의 정본이다. 작업 상태는 [ROADMAP.md](ROADMAP.md), 구현 계약은
[DESIGN.md](design/DESIGN.md), 완료 이력은 [UPDATE_NOTES.md](UPDATE_NOTES.md)를 따른다.

## 대화 이동 뒤 응답 중단 (2026-10-24)

질문 전송 후 다른 대화를 열면 `prepareNavigation()`이 요청을 무효화하고 `cancelQuery()`를
호출했다. `onResponse()`도 시작 화면 세대와 현재 화면 세대가 다르면 결과를 버렸다. 기존 화면
계약이 탐색을 취소로 취급했기 때문에 원래 대화로 돌아와도 답변을 이어 받을 수 없었다.

화면 탐색은 실행 중 요청을 유지하도록 변경했다. 요청이 원래 대화 객체를 보유하고 그 대화에만
답변을 반영·저장하며, 현재 화면의 모드·대화가 맞을 때만 표시한다. 완료 전에 돌아오면 같은 요청의
로딩을 복원한다. 사용자 메시지 저장 ACK를 기다리는 동안 이동해도 ACK 뒤 원래 대화·모드로 전송한다.
기존 단일 실행 제한·실제 QThread 종료 확인·시간초과·앱 종료 취소는 유지한다.

복원과 답변 저장이 교차하면 저장 전 원래 대화 객체를 사용해 오래된 파일 응답이 답변을 덮어쓰지
못하게 했다. 저장 실패한 답변도 복원 후 재시도할 수 있도록 보존한다. 명시적으로 요청 대화를
삭제할 때만 해당 질문을 취소하고, 이미 전송한 저장이 끝난 뒤 삭제해 대화가 다시 생기지 않게 한다.
백그라운드 답변 저장은 다른 대화의 복원을 무효화하지 않으며 삭제·새 탐색의 역순 응답 차단은 유지한다.

수정 전 실행 가능한 JS 재현은 대화 이동 시 실제 취소 호출 1회로 실패했다. 수정 후 프런트엔드
40개 시나리오와 Qt 질문 처리 포함 pytest 41개가 통과했다. 실제 QWebEngine·QWebChannel·QThread와
임시 대화 파일을 연결한 합성 시험도 `응답 전 복귀`·`이동 중 완료 후 복귀` 모두 원래 대화에만
답변 1개 저장·표시, 다른 대화 0개, 탐색 취소 없음, 워커 join을 확인했다. 사내 API는 호출하지 않았다.
전체 회귀는 2,175 통과·3 실패·8 skip 및 74개 subtest 통과다. 실패는 기존 PPT 수명·빌드·요청
저장 시험의 symlink/reparse fixture 생성 권한 오류(Windows 1314)이며 채팅 검사는 모두 통과했다.
후속 권한 설정 후 일반 사용자로 해당 3건만 다시 실행해 모두 통과했다. 전체 회귀 재실행 결과와는 구분한다.
새 의존성·대화 저장 스키마·인덱스 형식 변경은 없다.

근거는 `.aegisdesk_build/chat-navigation-before.log`, `chat-navigation-regressions.log`,
`chat-navigation-focused.log`, `chat-navigation-full-pytest.log` 및
`chat-navigation-live-final-check/report.json`이다. 실제 위젯 화면과 답변 표시도 확인했다.
사내 LLM/API 응답과 장시간 현장 시험은 별도다.

## 같은 프로세스의 마지막 검사 직후 열림 경합 재현 (2026-10-24)

**현재 Windows x64·Office에서 저장/미저장 문서 1개·2개, 네 사례 모두 실제 닫기 취소와 문서 유지를 확인했다.**
기존 후보 1.0.5.10의 복사본을 진단용 1.0.5.12로 빌드하고, 마지막 빈 컬렉션 검사·취소 상태 검사
직후부터 기존 `FileExit` 호출 전 사이에 인증된 합성 문서를 같은 Application에서 열도록 계측했다.
제품 소스·의존성·기존 frozen EXE는 변경하지 않았다. 실제 GUI 사용자 입력의 도착 시점을 추측하는
시험과 달리 해당 코드 위치의 순서를 고정한 실기 시험이며, 변경하지 않은 frozen 앱 전체 인덱싱 시험과 구분한다.

| 문서 상태 | 문서 수 | 원 PID | 종료 명령 반환 뒤 실제 닫기 취소 |
|---|---|---|---|
| 저장됨 | 1 | 26160 | 9.004ms 뒤 |
| 미저장 변경 | 1 | 32492 | 7.123ms 뒤 |
| 저장됨 | 2 | 10780 | 20.874ms 뒤 |
| 미저장 변경 | 2 | 27272 | 19.047ms 뒤 |

각 시험은 PowerPoint 0개에서 시작했고, 실제 Reader로 합성 파일을 읽고 닫은 뒤 종료 경계를 시험했다.
PID·생성 identity·STA thread 및 단조 시각으로 `빈 검사 완료 → 같은 호스트 문서 Open → 종료 명령
진입·반환 → PresentationBeforeClose의 cancel=false→true` 순서를 확인했다. 이벤트는 이미
`DeferredProtection=True` 상태에서 발생했으며, `QueryQuitOutcome`과 Python 요청 결과는 모두
`protected`였다. 이벤트 콜백에서는 원시 값만 버퍼에 넣고 정상 RPC에서 기록했으며 누락은 0건이다.

문서 창·슬라이드 수·저장 상태는 15초 뒤에도 같았다. 미저장 문서 각각의 추가 슬라이드와
`Saved=False`도 유지됐고, 두 문서 사례에서는 다른 문서가 함께 닫히지 않았다. 저장 확인 창을
수동 취소하지 않았다. 관찰 후 인증한 합성 문서만 저장·수동 닫기·정상 Quit으로 정리해 다음 수동
닫기가 막히지 않는 것도 확인했다. 주입을 끈 같은 진단 Reader의 대조 시험은 PID 4852의 실제 종료를 확인했다.

최초 진단 1.0.5.11은 계측 코드의 Office 저장 상태 값 변환 오류로 종료 명령 전에 중단되어 제외했다.
일반 종료 비교기의 `experiment_complete=false`·`remaining`은 해당 비교기가 프로세스 종료만 성공으로
정의하기 때문이다. 이 보호 시험은 별도 `race-verification.json`의 실제 이벤트·문서 유지·수동 정리
증거로 판정하며 자동종료 성공으로 바꾸어 집계하지 않는다.

근거는 `.aegisdesk_build/ppt-last-check-race/`의 `race-verification.json`,
`diagnostic-provenance.json`, `{saved,unsaved}-{one,two}-v12/internal_quit/`의
`race-host.jsonl`·`protection-live.json`, 각 시험의 `manual-cleanup.json`이다.
진단용 등록은 정식 helper `/remove`로 제거하고 기존 candidate helper `/prepare`로 1.0.5.10을 복원했다.
복원한 DLL·Events 해시는 기존 candidate와 같고 frozen payload 세 파일도 변경되지 않았다. 복원 후 합성 읽기·정상종료
대조 시험도 PID 29260의 원 handle 종료를 확인했다. 최종 PowerPoint·AegisDesk 프로세스는 0개다.
다른 Office 버전·실제 사내 DRM·일반 배포 승인은 이 결과에 포함하지 않는다.

## 같은 STA의 Office 종료 명령과 지연된 닫기 보호 후보 (2026-10-24)

1.0.5.10 후보는 빈 문서 확인과 `FileExit` 명령을 Reader STA 안에서 처리하고, 명령 반환 뒤에도
닫기 이벤트 보호를 유지하도록 변경했다. 외부 Application.Quit 콜백은 차단했다. 문서 열림 감지와
실제 닫기 취소를 구분하며 취소 확인 후에는 종료 권한을 포기한다. 새 패키지·타이머·강제종료는 없다.
최종 candidate `77faffb-dirty.a409a982`의 빌드·frozen 자체 진단을 완료했다. 일반 배포 종료는 비활성이다.

실제 Reader 단독 시험은 PID 33040의 종료를 확인했다. 변경하지 않은 frozen 앱의
`ppt-host-exit-index-001`·`002`는 각각 PowerPoint 0개에서 시작해 합성 `.ppt` 1청크를 fake 임베딩으로 저장하고,
AegisDesk가 생성한 PID 23224·30144의 원 handle 종료와 잔류 0개를 확인했다. 종료 중 RPC 단절은
`pending`으로 처리한 뒤 OS 관측으로 종료를 확정했으며 재요청·외부 Quit·강제종료는 없었다.

`ppt-user-open-{saved,unsaved}-host-exit-001`은 인덱싱 중 같은 호스트에 사용자 합성 문서를 열었다.
원 PID 20736·16788에서 `protected` 반환 후 창·슬라이드·저장 상태가 인덱싱 완료 15초 뒤와
AegisDesk 종료 뒤에도 유지됐다. 미저장 문서는 추가한 슬라이드와 `Saved=False`도 유지됐다.
각각 19·28청크를 저장했고 나머지 시험 파일은 사용자 사용 중으로 보류됐다. 관찰 후에는 인증한
합성 문서만 정상 저장·닫기·Quit으로 정리했다.

종료 요청 로그 직후 문서를 연 `ppt-host-exit-race-saved-001`·`ppt-host-exit-race-file-unsaved-001`도
문서를 유지했다. 전자는 기존 호스트 COM Open이 실패한 뒤 명시적 파일 실행으로 새 PID 26840에서,
후자는 PowerPoint 실행파일에 파일을 전달해 새 PID 27364에서 열렸다. 원 호스트는 먼저 종료됐다.
두 문서 모두 15초 및 AegisDesk 종료 후 유지됐다. 이 파일 실행 시험만으로는 같은 호스트에서 마지막
검사 이후의 경합을 재현하지 못했으며, 해당 구간은 위의 별도 계측 실기 시험에서 네 사례를 검증했다.

전체 회귀는 2,157 통과·11 실패·8 skip이었다. 시험 모형의 기존 Quit 호출·즉시 이벤트 해제 가정을
수정한 뒤 관련 19개, native Count 2개, 실제 이벤트 수신기/소유 참조 2개를 재실행해 실패 8건을 해소했다.
나머지 3건은 심볼릭 링크/reparse 시험의 Windows 권한 오류 1314이며 통과로 처리하지 않는다.
별도 종료 관련 97개 및 수정한 managed harness 재실행 1개도 통과했다. 설치된 1.0.5.10 DLL의
해시는 최종 빌드 payload와 일치한다.

앞선 재확인 후보 1.0.5.6은 기존 Count 뒤 문서 추가 시험에서 저장·미저장 문서를 유지했다.
그러나 재확인 반환 뒤에 문서를 추가한 `race-after-confirm-saved`에서는 `protected` 반환 뒤에도
원 PID 18824가 종료되고 문서 연결이 끊겼다. 재확인만으로 공백이 해소되지 않아 해당 후보는 채택하지 않았다.

별도 내부 호출 비교에서 Application.Quit은 UI 스레드로 옮겨도 PID 31228이 남았다.
Office `FileExit` 명령은 지연 UI 실험의 PID 25500, 기존 Reader RPC 안의 실험 PID 21216 모두
원 handle의 실제 종료를 확인했다. 이 결과로 최종 후보에서는 별도 UI 타이머를 사용하지 않는다.
외부 왕복을 남긴 FileExit 경합 실험은 미저장 문서의 저장 확인 창이 떠서 자동 보호 성공으로 판정하지 않았다.

시험용 PID 27328의 저장 확인 창은 사용자 잠금 해제 후 수동 취소했다. 같은 문서의 창·2슬라이드·
미저장 상태가 15초 유지된 뒤 합성 문서만 저장·닫고 정상 Quit으로 원 handle의 종료를 확인했다.
이 결과는 수동 취소가 필요했으므로 자동 보호 통과가 아니다. 현재 PowerPoint·AegisDesk는 0개다. 근거는 `.aegisdesk_build/quit-invoke-experiment/`의
`race-boundary-fixed-{saved,unsaved}`·`race-after-confirm-saved`·`ui-quit-no-user`·
`ui-quit-command-no-user`·`inline-quit-command-no-user`·`ui-file-exit-after-confirm-unsaved`다.
최종 후보 회귀·빌드 로그는 `.aegisdesk_build/ppt-host-exit-*`와
`.aegisdesk_build/candidate-ppt-host-exit/`에 보존한다.

## Quit 직전 사용자 문서 보호 실패 (2026-10-24)

**Reader 1.0.5.5 후보는 빈 문서 검사 후 Quit 직전에 열린 사용자 문서를 보호하지 못했다.**
저장된 합성 문서와 미저장 변경이 있는 합성 문서 각각 재현됐다. 일반 배포 자동종료는 계속
비활성이며, 무조작 자동종료·인덱싱 중 미리 열린 문서 보호의 이전 통과 결과로 이 경합을 승인하지 않는다.

변경하지 않은 frozen EXE `77faffb-dirty.5a75e8fa`의 `ppt-quit-race-live-001`은 실제 합성 `.ppt`
인덱싱 1청크 저장 후 `caller Quit` 로그를 관측한 즉시 별도 PowerPoint 실행파일로 사용자 파일을
열었다. 원 PID 11236은 종료됐고, 사용자 파일은 새 PID 20356에서 열려 15초 관찰 및 AegisDesk
종료 뒤에도 유지됐다. 새 호스트로 분리된 결과이며 동일 호스트의 종료 취소 이벤트 검증은 아니다.

별도 `compare-race-boundary.py` 진단은 설치된 원본 Reader·Events DLL과 제품의
`request_guarded_quit()`을 그대로 사용하고, 시험용 target-check 콜백에서만 문서를 추가했다.
Reader의 빈 컬렉션 검사 완료 → 호출자 콜백 진입 → 같은 Application에 합성 문서를 쓰기 가능·
창 표시로 열고 경로·창·슬라이드·저장 상태 확인 → 제품의 정상 Quit 순서를 QPC로 기록했다.
제품 코드·설치 파일·기본 설정 변경과 새 의존성은 없다. 이 진단은 frozen 앱 전체 인덱싱 재현과 구분한다.

`race-boundary-saved`의 PID 33508은 1슬라이드·창 1개·`Saved=True`,
`race-boundary-unsaved`의 PID 22264는 빈 슬라이드 추가 후 2슬라이드·`Saved=False`를 확인했다.
두 시험 모두 `protected`가 아닌 `quit_requested`가 반환됐고, **그 뒤 문서 확인**에서
`RPC_E_DISCONNECTED(0x80010108)`가 발생했다. 원 handle 관측은 두 프로세스의 실제 종료를
확인했다. 시험 도구는 추가한 문서에 Close/Save/Saved 변경을 하지 않았고 PowerPoint를 강제종료하지 않았다.
따라서 단순 Open 실패나 프로세스 잔류와 다르게 사용자 문서 보호 실패로 판정한다.

두 host trace는 `dropped=0`이며 Quit 진입·반환 시 Active 비트가 켜져 있고 Cancelled 비트는 꺼져
있었다. 이후 Unadvise가 성공했다. 현행 Count 검사와 실제 Quit 사이의 문서 추가를 이벤트 보호가
막지 못한 것은 확인됐지만, Office 내부 이벤트 전달·실제 문서 닫힘·구독 해제의 세부 순서는 미확정이다.
다음 작업은 이 경합을 막는 종료 설계 보완과 같은 시점의 회귀 검증이다.

근거는 `.aegisdesk_build/ppt-quit-race-final-report.json`, `ppt-quit-race-live-001/`,
`quit-invoke-experiment/race-boundary-{saved,unsaved}/`의 `race-boundary.json`·`caller.jsonl`·
`result.json` 및 `ppt-race-boundary-{saved,unsaved}-trace/`다. 일반 비교기의
`experiment_complete=false`는 Quit 후 보호 확인 실패이며, 콜백 진입·문서 열림·Quit 반환은
별도 기록으로 확인했다. 테스트 끝에 남은 PowerPoint·AegisDesk는 0개다.

## 내부 Quit 잔류와 보호된 호출자 Quit (2026-10-24)

Office 인증 갱신 후에도 기존 후보의 실제 합성 PPT 인덱싱은 성공하고 내부 Quit 후 PID는 남았다.
`ppt-index-live-007`의 원 PID 32804, 1청크 저장 및 lifecycle trace의 Quit 진입·반환·Unadvise 성공을
확인했다. 인증 갱신만으로 이 현상이 해소되지는 않았다. 동적 호출을 Reflection·명시적 native
IDispatch METHOD/반환 VARIANT 없음으로 바꾼 별도 1.0.5.1·1.0.5.2 시험도 각각 25초·20초 뒤 잔류했다.

1.0.5.4 별도 시험은 같은 호스트 Count·이벤트 보호 안에서 동기 COM 콜백으로 호출자의 원
Application 프록시에 Quit을 요청했다. 합성 읽기·닫기 후 `quit_requested`가 반환됐고, 원 handle의
실제 종료를 7.11초, 호출자 종료를 7.43초에 확인했다(콜백 MTA=1). 이는 시험 시작 기준 시간이며
Quit 반환 뒤 경과시간과 다르다. Python 호출 스레드와 COM 콜백 스레드는 달랐으나 같은 프로세스
MTA였다. 내부 Application의 Quit이 종료로 이어지지 않는 Office 구현상의 상세 조건은 미확정이다.

수정 후보 1.0.5.5는 기존 보호를 유지하고 이 콜백을 한 요청 동안만 허용한다. 새 활성화·비보호
외부 Quit 재시도·강제종료는 없다. 원 handle·identity·종료 세대·단독 프로세스·소유 기록을 콜백 직전
재확인한다. 일반 배포는 계속 비활성이며, 합성 PPT에 대한 실제 앱 검증 결과는 다음과 같다.

새 frozen 빌드 `77faffb-dirty.5a75e8fa`는 selftest를 통과했다. 독립 시험 `ppt-index-live-009`와
`ppt-index-live-010`은 각각 PowerPoint·AegisDesk 0개에서 시작했다. AegisDesk가 직접 띄운
원 PID 5956·22232에 연결해 Reader 1.0.5.5로 합성 바이너리 `.ppt`를 읽었고, fake 임베딩으로
각각 chunks 1행·emails 0행을 저장했다(`count_rows()` 확인). 두 시험 모두 `aegisdesk.log`에
호출자 Quit·`quit_requested`·`actual_exit`가 남았으며, 독립 관측도 보존한 원 프로세스 handle의
종료를 각각 14:07:30.832·14:08:47.971에 확인했다. AegisDesk는 당시 계속 실행 중이었고
시험 중 PowerPoint 수동 실행·수동 종료·강제종료는 없었다. Quit 반환부터 실제 종료까지 약 2초였다.

같은 최종 Reader 1.0.5.5의 내부 Quit 대조 시험도 12초 후 잔류했다. 별도 보호 시험은 이미 열린
합성 문서 1개가 있으면 `protected`로 반환하고 프로세스를 유지했다. 반면 콜백 중 문서 추가를
시도한 시험은 `RPC_E_DISCONNECTED(0x80010108)` 후 PID 26104가 남았다. 단절이 문서 열기와
Quit 이후 조회 중 어디서 발생했는지는 미확정이며 이 시험은 보호 성공 증거가 아니다. 정상 Quit과
창 닫기로 정리되지 않아 사용자가 직접 정리했고, 다음 시험 전 PowerPoint 0개를 확인했다.
Quit 직전·진행 중 문서 추가·DRM·사내 Office 보호 조건은 추가 검증이 필요하다.

인덱싱 도중 사용자 파일을 여는 추가 시험은 같은 frozen 빌드에서 저장된 문서와 미저장 변경이
있는 문서 각각 통과했다. `ppt-user-open-saved-003`·`ppt-user-open-unsaved-003` 모두
PowerPoint·AegisDesk 0개에서 시작하고, AegisDesk의 직접 기동·연결 후 실제 인덱싱 중
PowerPoint 실행파일에 별도 합성 `.pptx` 경로를 전달해 열었다. 사용자 파일은 감시 폴더 밖에 뒀다.
실제 문서 열림은 각각 14:26:05·14:27:02, 인덱싱 정리 완료는 14:26:16·14:27:06에 확인했다.
원 PID 27012·32712는 유지됐고 `service_return status=protected`로 반환해 호출자 Quit은 없었다.

각 시험의 60개 합성 인덱싱 대상 중 29개·9개가 저장됐다(`count_rows()` 확인). 사용자 실행파일이
원 호스트로 파일을 인계하는 동안 생긴 추가 PID는 외부 점유로 감지해 나머지 31개·51개 읽기를
일시 보류했다. 두 번째 시험은 사용자 문서에 빈 슬라이드를 추가해 `Saved=False`로 만든 뒤,
정리 완료 후 15초 이상 및 AegisDesk 시험 프로세스 종료 뒤에도 2슬라이드·미저장 상태가 유지됐다.
첫 시험도 같은 관측 구간에 원 문서·창·저장 상태가 유지됐으며, 관측 후 별도 시험 정리에서만
합성 문서를 닫고 정상 Quit했다. 새 의존성·제품 코드·기본 설정 변경은 없다.

최종 근거는 `.aegisdesk_build/ppt-user-open-final-report.json`과 각 시험 폴더의 `report.json`·
`appdata/AegisDesk/logs/aegisdesk.log`다. 첫 shell 연결 시험은 문서 열림 미확인, 다음 시험은
미지원 DocumentWindow.HWND 때문에 도구가 중단돼 최종 2회 통과 집계에서 제외했다.
이번 결과는 인덱싱 중 열려 종료 판단 전에 존재한 문서의 보호이며, Quit과 동시에 문서가 열리는
경합의 취소 이벤트 검증을 대신하지 않는다.

필수 전체 pytest는 2158 PASS / 8 SKIP / 10 FAIL이다. 실제 Word 프로세스의 간섭을 받던 대역
1건은 격리했고, 동시 실제 앱 시험의 machine lock과 충돌한 종료 테스트 6건도 앱 종료 뒤 재실행해
통과했다(최종 관련 69 PASS, 별도 집중 검증 88 PASS; 중복 포함). 남은 3건은 symlink/reparse
fixture 생성 권한 부족(Windows 1314)이다. 전체 통과로 판정하지 않는다. 동시 실행 영향을 받은
실제 앱 시험 `ppt-index-live-008`은 자동종료 경로를 타지 않아 성공 집계에서 제외했다.
최종 보고는 `.aegisdesk_build/ppt-caller-final-report.json`, 실제 앱 근거는 각 시험 폴더의
`baseline.json`·`process-trace.jsonl`·`observation.json`·`appdata/AegisDesk/logs/aegisdesk.log`다.

근거는 `.aegisdesk_build/quit-invoke-experiment/`의 `internal-result`, `raw-result`,
`callback-result-3` 및 `ppt-index-live-007`에 보존했다. 초기 콜백 2회는 인터페이스 조회 실패로
Quit 비교 전에 중단됐으며 성공 증거에 포함하지 않는다.

## 직접 기동 직후 COM 연결의 추가 프로세스 생성 (2026-10-22)

`77faffb` candidate와 Office `16.0.20430.20182`에서 PowerPoint 0개 상태로 실제 앱 인덱싱을
시작해도 직접 실행한 PID와 COM 창의 PID가 달라 읽기가 연기됐다. 임베딩은 fake,
문서 추출은 auto/addin이며 합성 바이너리 `.ppt`만 사용했다. 인덱싱 성공 건수는 0이다.

원인은 직접 기동 직후 기존 COM 객체를 찾지 못했을 때의 생성 폴백이다. `ppt_shutdown.launch_and_bind()`는
`CreateProcess` 직후 `win32com.client.Dispatch()`를 호출한다. 설치된 pywin32의 Dispatch는
`pythoncom.connect()`가 실패하면 `CoCreateInstance()`로 진행한다. 호출 추적 2회에서
`connect → MK_E_UNAVAILABLE(0x800401E3) → CoCreateInstance → 두 번째 POWERPNT.EXE 생성`을 확인했다.
최초 프로세스는 호출자의 Quit 없이 코드 0으로 종료됐다. 이전 사이클 PID를 재사용한 비교가 아니다.

3초 기동 대기 후에는 기존 객체 연결이 성공하고 생성 폴백을 타지 않았다. 별도 진단에서
새 프로세스를 만들지 않는 `GetActiveObject()`만 제한 시간 동안 재시도한 2회도 원 PID에 연결됐다.
`WaitForInputIdle()` 성공 후에도 추가 프로세스가 잠시 생긴 사례가 있어 이를 COM 등록 완료 증거로 쓰지 않는다.

추가 조회 시험은 PowerPoint의 클래스 식별자로 실행 중 COM 객체 등록 목록(ROT)을 직접 확인했다.
원 프로세스가 살아 있어도 약 97ms 시점에는 등록이 없고 기존 객체 조회가 `MK_E_UNAVAILABLE`로
실패했다. 약 920ms에 등록이 확인됐으며 약 1198ms에 같은 PID 연결이 반환됐다. 호출자와 원
PowerPoint는 모두 Windows 세션 1·비승격 상태였고 설정·권한 변경이나 두 번째 PPT 생성은 없었다.
이 재현의 오류는 프로세스 생성과 COM 객체 등록 사이의 시간차다. Office 내부 등록 시점의
결정 조건이나 사내 환경에서도 같은 지연이 발생하는지는 미확인이다.

직접 기동 분기는 이제 `GetActiveObject()`로 기존 객체만 조회하고 등록 대기 오류에만 재시도한다.
재시도 전후 원 handle·PID·생성 identity·단독 프로세스 집합을 확인한다. 기본 대기는
`collector.ppt_registration_timeout_sec` 10초이며 다른 COM 오류·대상 변경·시간초과는 읽기를 보류한다.
기존 호스트나 CreateProcess 미시작 시의 읽기 경로는 유지한다.

수정 candidate `77faffb-dirty.727ba198`를 빌드하고 frozen selftest를 통과했다. 실제 앱 시험
`ppt-index-live-004`는 PowerPoint·AegisDesk 0개에서 시작했다. 수집기가 띄운 PID 20352 하나에
16회·1296ms 후 연결됐고, Reader 1.0.5.0이 합성 `.ppt`를 읽어 fake 임베딩 1청크를 저장했다.
`aegisdesk.log`의 `documents_complete`에서 `quit_requested`가 반환됐으나 원 handle은 30초 후에도
살아 있었으며 앱 종료 뒤 추가 12초 관측에서도 남았다. **등록 경쟁은 해소됐지만 자동종료는 실패했다.**
사후 정리에서 동일 PID·생성 identity·빈 문서 집합·창 소유를 확인한 외부 정상 Quit은 실제 종료됐다.
이는 시험 정리이며 자동종료 성공이나 제품의 외부 Quit 허용 근거가 아니다.

추가 종료 비교는 frozen caller의 COM 초기화 실패로 중단됐고, 소스 비교는 이전 Dispatch 연결의
대상 변경으로 중단됐다. 등록 대기만 적용한 로컬 비교 adapter도 확인용 창 이후 visible-window 보호
조건에서 중단됐다. 이 비교들은 Quit 비교 완료 증거가 아니며 내부 Quit 잔류 원인은 미확정이다.
관련 회귀는 최종 코드 기준 199 PASS / 4 SKIP이다. 전체 pytest는 2151 PASS / 8 SKIP / 7 FAIL이며,
테스트 대역 인자 4건은 수정 후 관련 재실행에서 통과했다. 나머지 3건은 symlink/reparse fixture 생성의
Windows 권한 부족(1314)으로 미통과했다. 사내 환경·DRM·일반 배포 자동종료 승인은 미검증이다.

## 진단의 호출 준비와 PowerPoint 직접 실행 순서 (2026-10-22)

후속 VDI 시험은 `bind_step=probe_precheck`, `reason=target_changed`, 원 핸들 `os_status=exited`이며
명령 종료 후 PowerPoint 프로세스가 없다는 보고다. `host_bound`·합성 읽기·Quit에는 도달하지 않았다.
이 관측은 인덱싱 후 내부 Quit의 잔류 원인을 설명하거나 외부 Quit의 성공을 입증하지 않는다.

기존 진단은 부모가 PowerPoint를 먼저 실행한 뒤 별도 frozen 호출 프로그램을 시작했다.
제품은 같은 워커에서 실행 직후 COM에 연결한다. 호출 프로그램 시작·COM 준비 지연 동안
빈 COM 서버가 종료됐을 가능성은 있으나 Windows 실측으로 원인을 확정하지는 않았다.
비교 조건을 맞추기 위해 호출 프로그램의 COM·Python 준비를 먼저 끝내고 그 안에서
PowerPoint 실행과 연결을 연속 수행한다. 부모는 원 CreateProcess 핸들의 관측 권한만 복제받아
PID·생성 시각을 검증하고 종료를 독립 관측한다. 다른 PID를 찾아 종료 권한을 얻지 않는다.
인계 누락·불명확·PID 불일치·조기 종료는 계속 보류하고 후속 시험을 막는다.
실행·핸들 인계·Dispatch 진입/반환 시간을 따로 남기며 `target_exited`, `process_set_changed`,
`identity_mismatch`로 거절 원인을 구분한다. 원 프로세스 핸들 인계 회귀를 포함한 오프라인 시험은
74 PASS / Windows native 1 SKIP이다. 실제 Windows x86/x64 핸들 전달·Office·frozen·DRM과
전체 pytest(pytest 미설치)는 **UNEXECUTED**다. 이 변경으로 조기 종료나 인덱싱 후 잔류가
실제로 해결됐다고 판정하지 않는다.

## 종료 비교의 연결 호환성과 오류 분류 보완 (2026-10-22)

첫 비교에서 `host_bound`·Quit 이벤트 없이 `bind/unknown`으로 중단된 보고가 있다.
`os_status=exited`는 실행한 원 프로세스의 종료 관측이며, COM 호스트 연결을 확인하지 못했으므로
`actual_os_exit_verified=false`와 후속 시험 UNEXECUTED 처리는 유지한다. 이 결과는 외부 Quit 시험 결과가 아니다.

진단이 `Application.HWND`만 직접 읽고 제품의 미지원 속성 대체 경로를 누락한 결함을 보완했다.
HWND 미지원은 유력 원인이지만 이전 로그가 AttributeError 등을 unknown으로 축약했으므로 실제 원인은
아직 미확정이다. 빈 호스트에서 제품의 임시 빈 문서·캡션 표식 검증을 재사용하고, PID·생성 identity·
프로세스 집합이 다르면 Reader 선택·합성 파일 읽기·Quit을 하지 않는다. 확인 문서와 설정 복원 실패도 중단한다.
고정 `bind_step`, AttributeError/TypeError 등 오류 종류와 원인 오류의 종류·숫자 HRESULT를 기록하며
임의 메시지는 기록하지 않는다. 오프라인 회귀 64 PASS / Windows native 1 SKIP,
실제 Windows·Office·frozen·DRM 및 전체 pytest(pytest 미설치)는 **UNEXECUTED**다.

## 설치된 Reader의 종료 3방식 비교 (2026-10-21)

단축 경로 수정 후 직접 실행과 `TryQuitIfIdle()`의 `quit_requested` 반환은 확인됐으나,
원 프로세스 관측은 `timeout`이며 앱 종료 후에도 PowerPoint가 남았다는 보고가 있다.
실행 경로 거절과 내부 Quit 후 미종료는 별개 문제이며, 후자의 원인은 미확정이다.

격리된 시험 세션 전용 `--ppt-quit-compare`로 외부 Quit·내부 Quit·참조 해제를 새 프로세스에서
비교한다. 부모는 원 프로세스 핸들과 호출 프로세스 종료를 독립 관측하고, 잔류·불명확·증거 누락 시
후속 시험을 실행하지 않는다. Reader 설치·등록과 일반 자동종료 정책은 바꾸지 않는다.
실행 방법과 격리 조건은 [비교 진단 안내](guides/PPT_READER.md#powerpoint-종료-3방식-비교-진단)를 따른다.

이 진단은 명시적으로 격리를 확인한 사용자가 통제된 환경에서 실행하는 별도 도구다.
기존 capture/lifetime runner의 실행 잠금을 해제하지 않으며, 외부 Quit의 공유 인스턴스 경쟁 안전성이나
일반 배포 승인 증거를 제공하지 않는다. 실제 Windows x86/x64·Office·frozen·DRM 검증은 **UNEXECUTED**다.

개발 agent 구현 후 주 agent가 리뷰하고, 진단·진입점·기존 단축 경로·수명 unittest를 함께 실행해
59 PASS / Windows native 1 SKIP을 확인했다. 전체 pytest는 pytest 미설치로 **UNEXECUTED**다.

## PowerPoint 단축 경로로 직접 실행이 생략되는 결함 (2026-10-21)

설치·helper·stamp 검사가 모두 통과해도 App Paths가 `PROGRA~1` 같은 Windows 8.3 단축명을
반환하면 문자열 기반 Program Files 포함 검사가 실패했다. 이때 `untrusted_path`로 일반 COM
연결로 전환되고 직접 실행 ticket이 없어 문서 완료 시 `origin_mismatch`로 Quit을 요청하지 않는다.
29건 처리 뒤 `documents_complete` 정리가 실행된 사례이므로 30건 주기 미도달이 원인은 아니다.

단축명을 Windows native API로 긴 경로로 확장한 뒤 허용 루트와 비교하도록 수정했다. 원래 경로와
확장 경로의 링크·reparse 거절 및 실행 파일·PE 비트수 검사는 유지했다. 경로 거절에는 고정된
세부 사유·오류 종류·숫자 코드를 남겨 `untrusted_path` 한 가지로 뭉뚱그리지 않는다.
이 수정은 직접 실행 진입 조건을 바로잡으며, 실제 Quit 반환·원 PID 종료·사용자 문서 보호의
Windows/Office 검증을 대체하지 않는다.

오프라인 회귀는 `python -m unittest knowmate.tests.test_ppt_short_path knowmate.tests.test_ppt_lifetime -v`로
37 PASS / Windows native 1 SKIP을 확인했다. 전체 pytest는 pytest 미설치로 **UNEXECUTED**이며,
Windows 실제 단축명 API·Office 실행·Quit·원 PID 종료·사용자 문서 보호는 **UNEXECUTED**다.

## PPT 참조 수명·원 프로세스 관측 후보 (2026-10-20)

워커의 첫 실제 COM 사용 전에 native MTA를 확보하고 자기 성공분만 해제하는 후보와,
직접 실행한 PowerPoint의 원 handle을 보존하는 관측을 추가했다. 문서·예외 프레임이 끝난 뒤
정리하며, 빈 정리 결과를 실제 프로세스 종료나 재기동 성공으로 집계하지 않는다.
자기 초기화 해제·apartment 전체 종료·OS 프로세스 종료는 서로 다른 결과다.

별도 `ppt_lifetime` 진단은 Reader 로드 상태의 새 프로세스에서 참조 해제와 외부 Quit을 비교한다.
현재는 mock/replay만 사용하며 live/private 진입은 잠겨 있다. 기존 capture·checkpoint121·제품 설치
계약은 유지한다. 새 1.0.7 진단 DLL은 build-only 소스이며 실제 빌드·설치·등록은 하지 않았다.
명령과 결과 해석은 [진단 안내](guides/PPT_CAPTURE.md#별도-참조-수명-비교-후보-1070)를 따른다.

새 진단은 직접 생성한 합성 파일의 모든 OPC member 이름·내용을 비교한다. 기존 capture의
슬라이드·관계 검사만으로는 notes/customXml 등 전체 내용의 합성 출처를 보장하지 못하므로
기존 검증기를 새 경로의 합성 인증으로 사용하지 않는다.

### 구현 리뷰·실행 결과 (2026-10-21)

개발 Agent의 구현 뒤 root 소스 리뷰에서 다음을 보완했다.

- native apartment 조회 실패는 파일 손상으로 집계하지 않고 COM 환경 대기로 처리한다.
- 진단 로그 쓰기·drop 실패에도 자기 참조를 정리한 뒤 native pair를 끝낸다.
- OS 종료 사실과 실험 완료를 분리한다. 읽기·Close·참조 정리·호출자 완료가 빠지면 CLI 성공이 아니다.
- 외부에 보존된 예외도 끝난 COM 프레임을 붙잡지 않도록 숫자 코드 추출 후 traceback 연결을 정리한다.
- 초기화 직후 로그가 중단돼도 자기 성공분을 해제하고, apartment 검증 전 재진입은 차단한다.

`python -m unittest knowmate.tests.test_ppt_lifetime -v`: **23 PASS**.
별도 표준라이브러리 기반 native/handle 모형 검사 **12 PASS**, 변경 Python 34개 구문 검사와
`git diff --check` 통과. 모형 검사는 실제 Windows native 시험이 아니다.
필수 `python -m pytest knowmate/tests -v`는 `No module named pytest`로 실행하지 못했다.
pytest 미설치로 전체 회귀는 **UNEXECUTED**다.
전체 회귀 통과나 배포 가능 판정으로 해석하지 않는다.

Windows x86/x64 native·첫 import·frozen·Office/DRM·Reader 속도/정확도·실제 원 PID 종료·설치/복구는
**UNEXECUTED**다. 공유 host의 사용자 보호·실제 종료·배포·COM 수명·관측·성능 기준에 관한 기존
미해결 Critical 1 / High 4 / Medium 1은 유지하며, 후보 구현이나 모형 시험으로 닫지 않는다.

## PPT 한 번 수집 진단 구현 후보 (2026-10-20)

**핵심 identity 우선 관측·단회 internal/external adapter·공유 buffer/외부 collector·독립 OS 감시·schema3 판독을
구현하고 소스 리뷰·fake 회귀를 확인했다. 실제 Windows 빌드/native/Office 검증은 미실행이다.** 새 live 진입은 잠겨 있고 일반 배포
자동종료는 계속 비활성이다. 요청 반환·관측 종료·원 handle의 OS 종료·증거 유실을 서로 구분한다.
이 변경은 기존 in-host Quit 미종료 원인의 해결 증거가 아니다.

새 코드·분리 진단 패키지·offline 판독 및 필수 gate는 [실행·판독 안내](guides/PPT_CAPTURE.md)를 따른다.
작성자와 분리한 지적·수정·116 PASS/6 SKIP 및 기존 환경 오류는 [구현 리뷰](archive/ppt-shutdown-capture-review/implementation-review.md)에 남겼다.
Bind 이전 lifecycle은 unknown으로 남기고 이후 slot도 누락 counter/최종 drain을 확인한다.
아래 10월18일 기록은 당시 checkpoint121 분석이며 현재 설치 상태를 새로 확인한 기록이 아니다.

## PPT 종료·identity 관측 검토 (2026-10-18)

**설치와 합성 PPT 읽기는 성공했으나, 원래 in-host Quit 미종료 원인은 미확정이다.**
121에서는 선택 getter를 필수 host 관측 앞에 둔 진단 순서 결함을 확인했다.
순서 개선은 **미구현·미검증 제안**이며 이번 작업은 분석·offline 검증에서 종료한다.
현재 설치는 candidate121(`1.0.21.0`), recovery122(`1.0.22.0`)는 빌드만 했고 설치하지 않았다.
추가 설치·실제 Office 시험·live 재실행은 하지 않는다.

### 소스·환경과 실제 증거

분석 기준은 브랜치 `fix/ppt-deferred-diagnostic`, HEAD
`67c23cd6a9e851a0aeffd2ac7394579c03165353`와 미커밋 변경을 포함한 동결 소스
fingerprint `4d71bac5`이다. 당시 HEAD 및 문서 전용 커밋 `348c23d`만으로는 candidate121을 재현할 수 없다.
제품 코드·테스트·다른 문서 변경을 포함하지 않았다는 설명은 과거 문서 전용 커밋 `348c23d`의 범위다.
후속 보존 체크포인트에는 미완성 제품·테스트·진단 소스를 포함한다. 다만 candidate121 패키지의
byte-identical 복원이나 live 준비 완료를 뜻하지 않는다. 기본 비활성 빌드와 잠긴 runner, 새 PC 검증 조건은
[checkpoint 이전 안내](guides/PPT_CHECKPOINT_HANDOFF.md)를 따른다.
Windows 집 PC, 64-bit PowerPoint, 일반 사용자 controller에서 실행했으며 설치 UAC는 사용자가 승인했다.

| 실제 시험 | 관측 결과 | 해석의 한계 |
|---|---|---|
| 113 지연 관측 | 합성 읽기 65자·해시 일치, in-host 요청 `quit_requested`; native Quit 반환·Unadvise 성공 기록 뒤 600초에도 원 PID 생존 | Quit 반환은 OS 종료가 아니다. 후속 callback 전체를 포착한 trace가 아니다 |
| 113 순차 비교 | guard 이후 60초 생존; 외부 Quit 후 약 2.30초에 OS 종료 | 동일 세션의 순차 비교라 원인 분리는 안 됨 |
| 113 외부 Quit | Application·reader service 참조를 유지한 채 약 2.38초 후 OS 종료 | Python service 참조 보유만으로 미종료를 설명할 수 없음; host 호출 문맥·sink 영향은 남음 |
| 115 deferred | owner 측 deferred 경로에서 Quit 반환, 약 174.59초 후에도 생존 | 단순히 Python 호출 반환을 기다리는 문제라는 설명은 부족함 |
| 115 외부 flags=3 | DISPID 2021, flags 3 외부 호출 후 약 2.45초에 OS 종료 | flags=3만으로 내부 경로 실패를 설명할 수 없음 |
| 117 | 설치·loader·읽기 성공; Prepare `external_refused`, identity 획득/해제 2/2, START/ACK/Quit 0 | 당시 비교 결과와 guard 사유 미계측; 2/2는 동일 identity 증명이 아님 |
| 119 | Prepare `Identity.Different` / `IdentityMismatch`; guard `NotReached`, trace 33개·drop 0, START/ACK/Quit 0 | controller에서 재구성한 ROT 비교와 host가 받은 원 인자의 동일성을 혼동하면 안 됨 |
| 121 | 설치 후 `check=ready`, 보호 loader·DLL 두 개 해시 일치, 읽기 65자·해시 일치; Bind `bound`; controller unknown | host 관측 4개와 Prepare·START·ACK·Quit·Completion 모두 미실행. 종료 경로 성공 증거 없음 |

121 core SHA-256은 `b3ecd7de549510ecfc56b441730ae0d599232abd7521770b4bdad2c3f5302af8`,
events는 `1203cdfc432833112fbfe06036733e125003c40085940ad9150e9301892d142c`이다.
설치 `prepare`는 1회 실행했고 후속 `check`도 `ready`였다.
합성 출력 SHA-256은 `1d766c6794d7e50eba5a37b5850debc6f5309db02458793a941e3e614f9dd10e`이며 원문은 기록하지 않았다.

### 121 진단 순서 결함과 확보된 범위

실제 순서는 Bind → controller `collect` → 조건부 host `ObserveExternalIdentity` →
primitive JSON export → 조건부 Prepare다. controller는 다음 순서로 수집한다.
JSON 행 배열은 ROT가 먼저지만 실행은 candidate부터다.

1. 정확히 제출할 candidate의 로컬 VARIANT unwrap·canonical IUnknown·형식 정보.
2. ROT app, raw `_oleobj_`, candidate 및 ROT의 QI `_Application` self-control.
3. `candidate.Application` → `COMAddIns` → `COMAddIns.Item` → `COMAddIns.Item.Application`.

마지막 네 항목은 비교 보조용 선택 경로다. 그러나 `collect.usable`은 어느 행에서든
unknown/fatal이 생기면 false가 되고, `observe_export_prepare`는 9개 행을 포함한
controller usable을 host 호출의 선행 조건으로 사용한다. 따라서 알려진 선택 경로 오류가
무관한 핵심 host 증거까지 가렸다. 별도 선택 항목 완결성과 핵심 증거 완결성 구분이 없다.
안전 중단 규칙 자체를 완화할 사유는 아니다.

121은 앞의 5개 controller identity 행에서 group 0과 self-control을 확보했다.
`candidate.Application`은 `0x80020006`(명시적 이름 부재)로 unavailable,
COMAddIns와 Item은 각각 group 1·2로 관측됐다. 마지막 getter는 outer
`0x80020009`(DISP_E_EXCEPTION), EXCEPINFO scode `0x80004005`(E_FAIL)로 unknown이다.
121은 scode만 보존하므로 EXCEPINFO 나머지 필드를 추측하지 않는다.
119의 같은 선택 경로 시도에도 outer/inner가 같았고 EXCEPINFO는
`(0, None, None, None, 0, -2147467259)`였다. underlying Office getter 실패 원인은 미확정이다.
Python backend는 GetIDsOfNames와 Invoke를 하나의 `Application` 단계로 기록하므로
121 기록만으로 두 세부 경계를 독립 판정할 수 없다.

121 controller는 7 observed / 1 unavailable / 1 unknown, 소유 wrapper hold는
9 acquired / 9 drop attempts / 9 dropped, cleanup 오류 0이다.
이는 Python wrapper 정리 장부이며 서버의 모든 native 참조 해제 증명은 아니다.
이전 관측값은 유효한 과거 증거지만 오류 이후 서버 건전성·host captured identity·
COM 전달 후 candidate identity·Prepare 허용 여부·Quit 동작은 알 수 없다.
`host-report.json`은 terminal frame 미복구로 불완전하다. native trace에서 행이 안 보인다는
이유로 미도달을 단정하지 않는다. Prepare 0회는 controller 제어 흐름·저장 결과로 확인했다.

host 구현은 `Describe(host)` → `Describe(candidate)` → `host.Application` →
`candidate.Application` 순서다. COMAddIn.Application 경로는 없다.
선택 Application getter 이전에 핵심 두 행을 얻지만, 각 Describe의 형식 메타데이터 조회는
그 행의 QI와 결합돼 있어 host 메타데이터 unknown이 candidate 행까지 막을 수 있다.
host.Application unknown은 이미 확보한 핵심 행을 남기고 다음 선택 행을 중단한다.
이 경우 전체 report는 unusable이며 process fence UNKNOWN·Prepare 차단은 유지된다.
121 실제 실행에서는 host probe가 호출되지 않았으므로 이 내부 순서의 결과는 미측정이다.

### Offline 결과와 다음 조사 순서

동결 candidate121 전체 fake 회귀는 **1973 PASS / 1 FAIL / 5 SKIP**다.
실패는 production request writer 필수 gate 미완료이며 내부 결과는 **60 PASS / 0 FAIL /
2 UNEXECUTED**, reparse leases=1·2의 WinError 1314다. 권한 부족 미실행을 제품 승인으로 바꾸지 않는다.
이전 PR의 15 skipped는 총계만 알려져 있어 원 실행 목록을 추측하지 않는다.
별도 controller 14 PASS와 synthetic guard/CP/sink 시험은 실제 Office 종료 증명이 아니다.
회사 DRM·다른 계정·전체 UAC 행렬·실제 Office 배포는 이 집 PC 결과로 대체할 수 없다.

이번 offline 검증은 기존 controller의 정확한 마지막 getter 오류 재현과 필수 canonical
identity 실패 시 host 차단을 포함한다. 순서 개선 구현의 통과 결과가 아니다.
최종 **35 PASS**(정확한 마지막 getter 오류 재현·필수 canonical 실패 차단 2건 포함)를 확인했다.
첫 실행은 기존 pytest 임시 폴더 접근 거부로 25 PASS / 10 setup errors였고, 기존 폴더를
변경하지 않은 새 고유 임시 경로에서 같은 35개를 재실행해 통과했다. 실행 로그는 아래 경로에 보존한다.

1. controller-only 변경안: 필수 identity/wire 정보 → 기존 host observation 1회 → 즉시 primitive
   결과 저장. 알려진 COMAddIn.Application은 이번 실행 미실행으로 표시하고 119·121 과거 오류를
   별도 첨부한다. 모든 선택 항목 수집 여부와 핵심 증거 완결성을 분리한다.
2. 기존 `complete_host`, ownership/cleanup·generation·same identity·export 성공·Prepare 안전
   검사를 유지한다. 선택 getter를 별도 조사하더라도 첫 unknown/fatal 이후 추가 host 호출과
   Prepare를 금지한다. 필수 단계 실패는 host probe 자체를 막아야 한다.
3. 구현 후 offline에서 host 증거가 선택 오류 이전 저장됨, 오류 이후 host/Prepare 0회,
   export·cleanup·필수 실패 차단을 검증하고 별도 검토를 거친다. controller 순서만 바꾸는 데는
   설치121 재빌드·재설치가 필요 없다. host 내부 메타데이터 순서를 바꾸려면 별도 DLL 변경이 필요하다.
   새 소유 세션의 live 시험은 이후 별도 승인 범위이며 기존 PID 재시도는 금지한다.

### 시험 프로세스 정리와 증거 경로

121 시험은 직접 생성한 PowerPoint를 생성시각·상속 capability·marker 창·독립 OS handle로
연결했다. 합성 문서를 닫은 뒤 Presentations/ProtectedViewWindows/SlideShowWindows가 모두 0임을
확인했다. 이는 당시 증거이며 나중의 문서 상태를 보증하지 않는다.
180초 독립 관측 종료 시 원 프로세스는 생존했다.

이후 승인된 정상 정리에서 EXE 경로·PID·생성시각·정확한 HWND·스레드·단일 활성 주창과
다른 visible 창/대화상자 부재를 OS API로 재확인했다. COM 호출 없이 WM_CLOSE **1회**,
강제 종료·대화상자 자동 응답 없이 원 handle의 종료와 창 소멸을 확인했다.
시험 프로세스는 종료됐으며 이 정리는 진단 Prepare/Quit 성공에 포함하지 않는다.
최초 정리 스크립트 실행은 문법 오류로 동작 전 중단됐고, 수정 후 실행에서만 WM_CLOSE를 보냈다.

아래는 모두 저장소 루트의 `.aegisdesk_build/` 아래 상대 경로다.
원 로그는 Git 추적 대상이 아니며 과거 문서 커밋과 후속 소스 보존 체크포인트 모두에 포함하지 않는다. 재현에 필요한 동결 소스·패키지·
원 로그는 이 PC에 보존해야 한다.

| 내용 | 정확한 상대 경로 |
|---|---|
| 113 내부·외부 비교 | `delayed-guard-quit113/RESULT.json`, `compare-guard-external113/RESULT.json`, `external-service-held113/RESULT.json` |
| 115 deferred·외부 flags | `live-deferred115/RESULT.json`, `external-flags3-115/RESULT.json`, `quit-dispatch115/python-spy.json` |
| 117 거절 분석 | `live-external117/RESULT.json`, `live-external117/refusal-analysis.json` |
| 119 identity·기존 getter 오류 | `live-external119/live-result.json`, `live-external119/identity-and-close-v2.json`, `live-external119/identity-final-and-close.json` |
| 121 설치 | `ppt-pair-121-122-ready/installation-trial-121/prepare.stdout.log`, `ppt-pair-121-122-ready/installation-trial-121/validation-result.json` |
| 121 관측·OS·정리 | `live-external121/identity-controller-result.json`, `live-external121/identity-observation.json`, `live-external121/control.jsonl`, `live-external121/process-events.jsonl`, `live-external121/host-report.json`, `live-external121/analysis-evidence.json`, `live-external121/normal-close-os-only.json` |
| 소스·설치 pin | `live-external121/package-pins.json`, `live-external121/controller-manifest.json`, `ppt-pair-121-122-ready/source-candidate121/` |
| 전체 fake·writer·controller | `trace-release121/FINAL_REPORT.json`, `trace-release121/full.log`, `trace-release121/full.xml`, `trace-release121/writer-summary.json`, `trace-release121/controller-final.xml` |
| 이번 offline·변경 전 기록 | `identity-order-review121/offline-final.log`, `identity-order-review121/offline-final.xml`, `identity-order-review121/test_order_reproduction.py`, `identity-order-review121/baseline-status.txt`, `identity-order-review121/baseline-diff.patch`, `identity-order-review121/baseline-hashes.json` |

실제 실행 명령은 후보 패키지 `PptReaderSetup.exe prepare`(1회), `PptReaderSetup.exe check`,
`.venv/Scripts/python.exe -X utf8 .aegisdesk_build/validate_install121.py`,
`.venv/Scripts/python.exe -X utf8 .aegisdesk_build/live-external121/run.py --live`였다.
이 목록은 과거 실행 기록이며 재실행 지시가 아니다.

## 현재 판단 (2026-10-13)

PowerPoint 내부 추가 기능은 VDI와 사내 PC의 소수 문서에서 내용 일치·속도 개선이 보고됐다.
[운영 적용·설치 설계](design/PPT_ADDIN_INDEXING.md)는 완료했으며 운영 구현과 연속 인덱싱 검증은 남아 있다.
사내 크래시 기록도 확보하며 SQLite 전환이나 추적 수명 변경은 아직 구현하기로 결정하지 않았다.

| 항목 | 확인된 사실 | 미확정 사항·다음 검증 |
|---|---|---|
| 앱 비정상 종료 | 사용자가 사내 발생을 보고함. 진단 활성 시 사이클마다 추적 시작·종료 | 오류 모듈·예외 코드·종료 단계 확보 |
| 메일 처리 중 종료 후 WINWORD 잔류 | 트레이 종료 후 워커 강제 종료·하드 종료 로그 확보, 문서 완료 시 정리·임베딩 취소 구현 | §4의 사내 종료·PID 검증 대기 |
| 문서 상태 저장 | JSON 전체 읽기·재작성, 경로 조회는 메모리 딕셔너리 | 사내 JSON 읽기·쓰기와 파일 접근 시간을 분리 측정 |
| 유휴 폴더 확인 | 폴더 순회 후 삭제 확인에서 파일 존재를 다시 조회 | 로컬·SMB별 순회·존재 확인 비용 측정 |
| 검색 규모 | 검색용 벡터 인덱스 생성 코드 없음, 질의 처리는 동기 실행 | 청크 수와 검색·LLM 시간을 측정한 뒤 개선 선택 |
| 임베딩·Office 지연 | 32청크 호출에서 미계측 4.82~5.52초 보고, 응답 처리·CPU 계측 보강 | 새 구간 로그와 메모리 진단 설정 확보, Office open 편차 실측 |
| PowerPoint COM 읽기 | 내부 추가 기능의 합성 반복·실문서 비교에서 사용자 보고로 내용 일치와 속도 개선 확인 | 아래 §사내 검증 보고. 운영 설치·연속 인덱싱·메모리·종료 검증 대기 |

2026-10-09에는 Word·Excel `Quit()` 미반환 또는 후속 종료 미확인으로 소유 registry가 지워져 다음
사이클이 같은 Office를 재사용하거나 사용자 점유로 오판할 수 있는 경로를 보강했다. 보고된 장시간
사례의 원본 로그는 없어 실제 원인이 Quit hang, 강제종료 실패, identity 조회 실패 중 무엇인지는
확정하지 않았다. 새 로그는 PID·EXE·시간·정리 결과만 남기며 문서 내용은 기록하지 않는다.

### PPT 설치 준비 폴더 호환성 (2026-10-16)

이전 설치가 기본 상속으로 만든 `AegisDesk` 부모를 strict 검사로 거절하는 호환성 결함을
확인했다. 부모의 상속 차단 false·준비 자식 true인 조합에서 `/prepare`는 UAC 전에
`acl_unavailable`/12로 중단될 수 있었다. 부모 플래그만으로 새 정책 전체 통과를 단정할 수
없으며 실제 owner/DACL·고정 경로·파일시스템 검사를 함께 통과해야 한다.
AppData 삭제로 ProgramData/HKLM 설치 상태는 초기화되지 않는다.

1.0.3.0 후보는 부모의 실제 권한을 보수적으로 판정하고 자식의 정확한 보호 ACL은 유지한다.
폴더 검사에서 기존 owner/DACL·파일을 보존하며 directory lease와 민감 작업 전 재확인을 추가한다.
owner/Admin/System을 신뢰하는 기존 경계는 유지한다. lease는 owner의 DACL 변경 권한을 동결하지 않는다.
앱 로그의 `required_version/installed_version`과 고정 `stage/error_type/winerror`로 실패 위치를 판독한다.

별도 임시 루트의 Windows native 하네스를 추가했다. Linux에서 실제 Windows ACL·공유 handle·UAC·
Office는 **미실행**이며, 하네스 소스나 모형 통과를 설치 성공으로 보고하지 않는다.
[기존 폴더 검증 절차](guides/PPT_READER.md#기존-설치-준비-폴더와-native-lease-검증)를 통과한 뒤
같은/다른 관리자 UAC, A(owner)/B(예약 owner)/C(비소유 사용자), 실제 ProgramData 업데이트를 확인한다.

Windows 시험에서 CRT 파일 열기는 native `winerror` 없이 `errno=EACCES`만 반환할 수 있다.
거절 판정은 이 표현을 수용하고 무관한 오류는 거절한다. 실제 helper 종료와 삭제 확인은 별도 유지한다.
native 하네스의 30초 시간초과는 컴파일/실행 phase와 flush된 마지막 고정 stage를 표시한다.
멈춘 단계가 확인되기 전에는 원인을 특정하거나 설치 성공으로 간주하지 않는다.
저장 진입 이후 위치는 lease 없는 대조군과 lease 보유 시험에서 실제 `AtomicWrite`의 열기·기록·flush·close·
ACL·rename 단계를 나눠 확인한다. 계측은 native 시험 빌드에서만 포함한다.
명시적으로 닫힌 관찰 handle을 `Popen` 소멸자가 다시 조회할 수 있는 수명 경로는 별도로 차단한다.
관찰 해제를 가짜 종료 코드로 대체하거나 helper를 종료하지 않는다.

사내 비교에서는 managed 신규 저장이 lease 보유 시 Win32 32, 교체는 1175로 실패했고,
저수준 native 신규·교체는 lease 0/1/2에서 통과했다. native 이름 충돌의 NTSTATUS는
`C0000035`였으나 기존 시험이 자기 DELETE handle을 열어둔 채 소스를 다시 읽어 Win32 32를
덧붙였다. 이제 소스를 닫은 뒤 충돌 보존 상태를 확인한다. managed 교체 대조군의
`target_security_changed`는 아직 분류하지 않았으며 owner/DACL/보호·정규화/control flags의
차이를 숫자·불리언으로 나눠 기록한다. 실제 권한 약화라면 별도 출시 차단 사유다.

1.0.4.0 후보는 **요청 파일만** 실제 `RequestWriter`의 같은 폴더 native 이름 변경으로 저장한다.
WRITE 공유를 추가하거나 lease를 풀지 않는다. ProgramFiles 기록·저널의 managed 저장은 유지한다.
[한 번의 진단 명령과 짧은 결과 판독](guides/PPT_READER.md#ppt-요청-저장과-충돌-진단)은
기존 대조군과 실제 제품 저장, 필수 ACL·stdout 수명 시험을 구분해서 보고한다.
미확인 native 반환은 성공 처리하지 않고 helper·버퍼·lease·mutex를 유지하며 보류한다.
사내에서는 writer 56 PASS / 4 FAIL / 2 UNEXECUTED, 필수 ACL 시험 PASS가 보고됐다.
실패 4건은 hardlink fixture 생성 2건과 junction 정리 단계 2건이며, 제품 저장 결함으로 확정하지
않았다. stdout 시험은 release 후 5초 안에 실제 종료가 확인되지 않았다. 원본 전체 로그는 미제공이다.

후속 시험은 링크를 lease 전 준비하고 실제 오류 코드를 기록한다. 정리는 시험 소유 ID를 확인한
임시 폴더만 대상으로 하며 reparse를 따라가지 않고 최초 오류와 정리 오류를 함께 보존한다.
stdout은 쓰기 전후·종료 직전 단계를 기록하고 retained process handle로 실제 종료와 마지막-close
삭제를 확인한다. 시험 자식의 실패 정리가 성공해도 본시험 실패를 통과로 바꾸지 않는다.
pytest가 줄 앞에 시험 이름을 붙여 ACL PASS 요약을 누락하는 문제는 재현했으며, 최종 결과를
사용해 정리 단계 실패까지 포함해 판정한다. 명령 한 번과 상세 로그 하나·앞뒤 짧은 요약은 유지한다.

Linux 회귀·Mono 컴파일은 Windows 저장 성공의 근거가 아니다. 후속 Windows 시험·PowerShell·
회사 DRM과 다른 사용자·같은/다른 관리자 UAC·32/64비트 Office 검증은 **미실행**이다.
기존 orphan temp만 남은 상태의 명시 복구는 별도 과제이며 자동 삭제·재권한 부여로 우회하지 않는다.

### COM poison 복구 일관성 (2026-10-15)

실제 종료·소유 기록 제거 후 2초 목록이 남는 문제, 진행 중인 열거/guard가 옛 결과를 사용하는
경합, Word·Excel 설정 완료/재사용과 정리가 겹치는 경합을 제품 코드에서 보완했다.
최초 오류 뒤 Close RPC 오류가 생겼을 때 단계와 code를 섞던 경로도 분리했다.
공통 판독기는 내부 구조화 HRESULT와 bounded cause를 처리한다. 사용자 Office와
미검증 PID를 추정 종료하지 않으며 PPT Quit·kill 금지를 유지한다.

실제 제품 helper·getter·parse를 사용하는 fake 회귀는 event로 열거/identity 시점을 고정하고
실제 registry mutator로 cleanup ABA를 만든다. 정확한 generation·앱/proof pair,
잠금 밖 참조 해제, 최초 분류·이중 오류 증거·민감 문구 불로그 및 PPT fresh preflight를 확인한다.
설계 모델 통과와 구분하여 제품 회귀 결과는 PR의 검증 기록을 따른다.

사내에서는 동일 EXE의 RPC 단절 → actual exit → 다음 파일의 attempt/guard epoch·PID를
연결해 확인한다. 사용자 Word·Excel 동시 실행, 조회 권한 실패, PID 재사용, HWND 대체 창,
PPT 재접속·중단 Close도 확인해야 한다. 실제 Windows·Office·DRM·COM Release는 미실행이다.
잠금 검사 이후 새로운 OS 종료·사용자 실행을 원자화하지 않으며 즉시 GC를 보장하지 않는다.

## 대량 메일 완료 뒤 메모리 잔류 (2026-10-24)

**합성 `.mysingle` 2,000건에서 완료 후 잔류를 재현했고, 수집기 캐시 공유·상한 후보로 유휴
Private Bytes를 약 53% 줄였다.** 진단 OFF/basic/detailed의 CPU 비용 비교와 별도 시험이다.
실제 메일 파서·AES-GCM/DPAPI·LanceDB·수집기 QThread를 사용하고 임베딩만 제품 fake 모드로
실행했다. 별도 AppData·인덱스에서 500건씩 4사이클을 처리해 양쪽 모두 10,000청크를 저장했다.

| 관측 시점 | 기존 코드 Private Bytes | 캐시 한도 후보 |
|---|---:|---:|
| DB 준비 후 | 331.9MiB | 332.1MiB |
| 2,000건 처리·워커 join 후 | 1,095.5MiB | 518.5MiB |
| 30초 유휴 후 | 1,092.0MiB | 515.0MiB |
| 시험용 명시적 GC 후 | 1,094.8MiB | 516.0MiB |
| 시험용 DB·수집기 참조 해제 후 | 420.9MiB | 392.9MiB |

기존 코드는 fragment를 1,042개에서 1개로 병합한 뒤에도 사용량이 줄지 않았다. GC 후 살아 있는
`MailIndexJob`은 0개였고 Python Arrow 풀의 현재 사용량도 거의 0이었다. 시험용 Arrow
`release_unused()`는 기존 코드에서 약 4.6MiB만 줄였지만 DB·수집기 참조 해제는 약 669MiB를
줄였다. 이 재현의 주요 보유 영역은 장수명 DB 연결·테이블의 네이티브 자원과 캐시로 판단한다.
Python Arrow 풀 수치만으로 Rust 쪽 Arrow 할당 전체를 측정했다고 해석하지 않는다.

설치된 LanceDB 0.34.0의 `Session.default()` 설명은 인덱스 6GB·메타데이터 1GB의 캐시 한도다.
이는 실제 선할당량이 아니다. 후보는 문서·메일 수집 연결에 같은 세션을 주입하고 각각 64MiB로
제한한다. 실제 세션 표본은 약 66MiB였으며 프로세스 전체 메모리 한도나 모든 메모리의 OS 반환을
보장하지 않는다. 운영 GC·Arrow trim·작업 집합 축소는 추가하지 않았다. 기존 설정에 키가 없어도
번들 기본값을 적용하고 저장 스키마·인덱스 버전·의존성은 유지한다.

저장 UID 2,000개·각 5청크와 전체 행 수를 projection 조회로 확인했다. 별도 프로세스로 재연결해
표본 20개 메일의 복호화 본문·벡터·출처 필드를 메모리에서 해시 비교했고 기존/후보 결과가 같았다.
평문·벡터는 보고서에 남기지 않았다. 유휴 다음 사이클·워커 idle·진단 참조 해제·시험 프로세스 종료도
확인했다. 캐시 축출 뒤 독립 QueryReader 조회를 포함한 관련 테스트 33개가 통과했다.
전체 회귀는 2,182 통과·1 실패·6 skip 및 74개 subtest 통과였다. 실패는
`test_office_app_exit.py`의 resiliency 정리 시험에서 30ms 검사 시점에 helper 진입 신호가
없었던 것으로, 같은 시험 단독 1개와 해당 모듈 전체 22개는 재실행에서 모두 통과했다.
전체 실행을 실패 0으로 재표기하지 않는다. PPT native subprocess 출력의 UTF-8 디코딩 thread
경고도 남았으며 이번 캐시 수정에서 해당 시험의 출력 처리나 종료 정책은 변경하지 않았다.

두 대량 시험과 회귀는 별도 프로세스에서 일부 동시에 실행했으므로 경과시간으로 속도 향상을
주장하지 않는다. 보조 `latest_version_hint.json` 갱신의 접근 거절 경고가 기본 1회·후보 3회 있었고,
재연결과 저장 결과 검증은 통과했다. 경고 자체가 해소됐다고 주장하지 않는다.
처음 3,000건 시도는 시험 스크립트의 청크 수 계산 오류로 중단했고, 캐시 getter 조회 실패로
작업 전에 끝난 두 시도도 집계에서 제외했다. 최종 2,000건 시험은 실제 준비 결과로 청크 수를 산정했다.

근거는 `.aegisdesk_build/bulk-mail-memory/comparison.json`,
`{baseline-2000-v3,bounded-2000}/{events.jsonl,report.json,loaded-source-manifest.json}` 및
각 폴더의 `loaded-source/`다. 기존 코드는 `d8cfeae`, 후보는 기록된 hash와 일치하는 소스를
보존했다. 후보 측정 시작 후 타입 표기만 보완했으며 전체 회귀는 최종 소스로 실행한다.
`run.py`·`compare.py`·`provenance.py`, `full-pytest.log`, `cache-contract-after.log`,
`office-cleanup-recheck.log`·`office-cleanup-module-recheck.log`도 같은 상위 폴더에 보존한다.
사내 원본·API·DRM·frozen 전체 UI·장기간의 누수 여부와 직렬 성능 비교는 미검증이다.

## 1. 앱 크래시와 메모리 진단

### 현재 사용법과 검증 대기

기본값 `collector.memory_diagnostics_enabled: false`는 유지한다. 일반 사용량 로그가 필요하면
`true`와 `memory_diagnostics_mode: basic`을 사용한다. 기존 true 설정도 mode를 추가하지 않으면
basic으로 동작한다. 설정을 바꾼 뒤 앱을 재시작한다. 계약은
[설계 문서의 메모리 진단](design/DESIGN.md)을 따른다.

| 모드 | 기록·동작 | 부하 해석 |
|---|---|---|
| OFF | 조회·진단 로그·추적·진단 GC 없음 | 평소 기본값 |
| basic | Windows Private Bytes·delta와 이미 로드된 Arrow 풀 수치 | tracemalloc API·스냅샷·강제 GC 없음. 외부 tracing 자체의 부하는 남음 |
| detailed | 기존 프로세스 전체 depth 10 추적·스냅샷 비교·종료 GC | 명시 선택한 짧은 재현에만 사용. 외부 추적의 depth·peak·수명 보존 |

basic은 Python 값을 `n/a`, `python_scope=not_measured`로 기록한다. 미로드 PyArrow를 수치 조회
때문에 import하지 않으며 Windows가 아니거나 조회 실패하면 해당 수치는 `n/a`다. Private 조회가
실패한 표본과 다음 유효 표본의 delta도 `n/a`다. Arrow peak는 사이클 peak가 아닌 풀 수명 고수위다.
Private에서 Python·Arrow를 빼서 네이티브 사용량을 확정하지 않는다.

기존 단계·메일 첫/약 50건/마지막 묶음·optimize 전후 표본 빈도는 유지한다. COM 정리 뒤 basic은
`cycle_resources_released`를 남기며 GC·추적 시작/종료·스냅샷 로그는 생성하지 않는다.
이 표본은 작업 참조 정리 뒤 사용량이지 전체 메모리 반환 보장이 아니다. detailed는 실제 GC 전후와
자체 trace 시작/종료·스냅샷 경계를 유지한다. 자체 추적 current는 추적 이후 살아 있는 할당만 포함하며
`python_scope`·`trace_started_at_ns`·`tracemalloc_mib`로 범위와 추적기 사용량을 표시한다.

진단의 Python 예외는 종류만 보고하고 수집 결과를 바꾸지 않는다. 오류 보고 자체가 실패해도 COM
정리·진단 참조 해제·idle 전환을 계속하며 optimize 전후 콜백 실패로 성공/쿨다운을 바꾸지 않는다.
수집 완료 시그널은 `_run_cycle()` 끝에서 전송되어 COM·진단 종료보다 먼저다. 다음 자동 수집은
`isRunning()`으로 중복 실행을 막는다. 강제 QThread 중단·네이티브 크래시는 finally를 보장하지 않는다.

**운영 권장 전 남은 검증**: 아래 Windows 소스 시험은 같은 입력·임시 인덱스 조건의 OFF/basic
각각 3회 비교에서 basic median 경과시간 증가 5% 이내를 확인했다. 실제 Windows EXE에서
30분 이상 반복, 취소·다음 사이클·앱 종료와 저장 결과 동일성도 확인한다. 합성 입력의 fake 성능
비교만으로 Office·사내 API 성능을 확정하지 않는다. 현장 시험 전 기본 OFF를 유지한다.

### Windows 소스 비교 (2026-10-24)

**basic의 경과시간 기준은 충족했고 detailed의 큰 CPU 비용은 재현됐다.** Windows·Python 3.11.0,
PyQt6 6.11.0·LanceDB 0.34.0·PyArrow 24.0.0에서 실제 수집기 QThread·PlainReader·LanceDB와
제품의 fake 임베딩을 사용했다. 합성 TXT/EML만 사용하며 사용자 설정·인덱스·의존성은 변경하지 않았다.
각 실행은 새 프로세스와 새 인덱스로 시작하고, DB 생성은 측정 전에 마쳤다. 모드 순서를 바꿔 3회씩 실행했다.

| 같은 입력 | OFF 중앙값 | basic 중앙값 | detailed 중앙값 |
|---|---:|---:|---:|
| 문서 8개·메일 4개, 24청크 | 1.435초 | 1.496초 | 43.520초 |
| 문서 32개·메일 8개, 1,016청크 | 4.245초 | 4.120초 | 미실행 |

basic은 짧은 작업에서 +4.22%, 큰 작업에서 -2.96%였다. 작은 차이를 성능 향상으로 해석하지 않는다.
큰 작업의 실제 수집기 PID 외부 표본은 CPU 시간 중앙값 OFF 5.125초/basic 5.453초,
Private Bytes 최고치 중앙값 360.6/360.6MiB였다. CPU 시간은 전체 스레드 합계이며 경과시간과 다르다.
저장 행의 경로/메일 UID·청크 순서·본문·벡터는 메모리에서 해시로 비교했고 모든 모드의 결과가 같았다.
기록에는 본문과 벡터를 남기지 않았다.

detailed는 짧은 작업에서도 약 30.3배 걸렸다. 로그 경계상 문서 fake 임베딩 합계 약 11.6초,
메일 임베딩 약 8.7초, 메일 처리 후 스냅샷 생성·비교 약 11.3초, GC 약 1.5초였다.
추적 시작·종료는 수 밀리초였다. 이 부하는 추적 활성 중 할당 작업과 스냅샷 비교에 크게 나타났으며,
반복 stop이 크래시 원인이라는 증거는 아니다. fake 임베딩의 Python 난수 벡터 생성 비용이 포함되므로
사내 API에서도 같은 배율이라고 일반화하지 않는다.

20ms Qt 타이머의 최대 간격 중앙값은 OFF/basic 약 31/32ms, detailed 약 206ms였다.
실제 PID로 외부 수치를 다시 확인한 별도 1회 detailed에서는 최대 간격 1.054초와 CPU 시간 43.672초를
관찰했다. 이는 Qt 이벤트 루프 지연이며 실제 전체 QWebEngine 화면의 조작 시험과 구분한다.

세 모드에서 합성 임베딩 대기 중 취소→join→idle→다음 사이클 저장 결과 동일성을 확인했다.
취소 전달 후 join은 OFF/basic 약 0.014/0.015초, detailed 0.375초였다. OFF/basic은 각각 최초 인덱싱,
변경 없는 20사이클, 문서 1개 재인덱싱 3사이클을 실행했다. idle 20회 동안 Private Bytes 증가는
약 0.4/0.6MiB였고 모든 워커 join·진단 참조 및 자체 추적 해제·시험 Qt 프로세스 종료를 확인했다.
관련 진단·설정 pytest 53개도 통과했다. 짧은 반복 시험이며 메모리 누수 부재·크래시 해결을 확정하지 않는다.

처음 시도한 큰 detailed 작업은 과도한 시간이 걸려 시험용 Python만 중단했고 집계에서 제외했다.
가상환경 launcher PID를 측정했던 초기 외부 CPU/메모리 표본도 제외하고 실제 PID를 확인해 재측정했다.
초기 작업 내부 시간과 Qt 간격은 유효하며 최종 분석은 이를 외부 수치와 구분한다.
근거는 `.aegisdesk_build/memory-load-check/bounded/analysis.json`, `report.json`, `large-report.json`,
`verified-short-report.json`, 각 실행의 `app.log`·`result.json`과 상위 `focused-tests.log`다.
재현 스크립트와 소스 hash도 해당 폴더에 보존했다. frozen EXE의 30분 이상 시험·전체 앱 종료 절차,
Office·DRM·사내 API는 미검증이며 기본 OFF와 detailed의 짧은 명시적 사용 정책은 유지한다.

### 로컬 합성 비교 (2026-10-15)

Linux에서 합성 32×1024 임베딩 응답을 실제 JSON 해석·벡터 검증 함수로 처리했다.
워밍업 뒤 각 모드를 별도 프로세스에서 3회씩 순서를 바꿔 실행한 경과시간 중앙값이다.
일반 로그는 이미 로드된 Arrow 풀과 단계·50회 단위 표본을 실제 파일에 기록했다.

| 같은 작업의 반복 수 | OFF | basic | detailed |
|---|---:|---:|---:|
| 1,800회 | 12.955초 | 12.865초 | 미실행 |
| 30회 | 0.210초 | 0.227초 | 5.603초 |

긴 OFF/basic 비교는 목표인 중앙값 증가 5% 이내를 충족했다. 작은 시간 차이를 성능 향상으로
해석하지 않으며 짧은 사이클의 표본·파일 로그 비용은 남는다. 각 비교 안에서 입력 hash·처리
벡터 수·checksum은 같았다. 초기 1,500회 detailed 측정은 시간이 과도해 중단하고 별도 짧은 동일
작업 비교로 나눴다. 긴 상세 진단이나 실제 Windows 부하가 검증됐다는 의미는 아니다.
Private Bytes·Office·사내 API·실제 메모리 누수와 크래시 감소 효과는 미측정이다.

### 가설과 한계

사내 LLM은 반복 추적 시작·종료를 유력 원인으로 제시했다. 네이티브 스레드의
`PyTraceMalloc_Track()`와 `tracemalloc.stop()` 경합으로 크래시가 발생한
[CPython 보고](https://github.com/python/cpython/issues/128679)가 있다. 보고의 재현 환경은
Python 3.12/3.13·Linux다. 이 프로젝트의 Windows·Python 3.11에 동일 결함이 적용되거나,
사용 중인 라이브러리가 해당 경로를 실행한다는 증거는 아직 없다.

진단을 끄면 추적뿐 아니라 스냅샷·강제 GC도 함께 꺼진다. 크래시가 사라져도
반복 `stop()`만 원인이라고 확정하지 않는다. 앱의 사용자 종료 처리에서 실행하는
`QThread.terminate()`·`os._exit()`와 Office 워치독의 프로세스 종료도 구분한다.

### 증거 확보 순서

1. 사내 앱 버전·빌드 커밋·실제 Python/의존성 버전, 진단 설정, 발생 시각·작업·종료 요청
   여부, 직전 로그와 Windows 응용 프로그램 오류의 오류 모듈·예외 코드를 확보한다.
2. 먼저 OFF/basic의 같은 작업 로그를 PID·사이클 ID별로 비교한다. 상세 자료가 꼭 필요할 때만
   detailed의 COM 정리·GC·스냅샷 비교·추적 종료 경계를 짧게 확인한다. 추가 자료가 필요하면 `faulthandler` 또는 Windows 크래시 덤프도 검토한다.
3. 동일 조건에서 진단 활성/비활성을 비교하고, 관련성이 확인되면 추적을 한 번 시작해 유지하는
   방식, 스냅샷, 강제 GC를 분리해서 비교한다. 간헐적 증상이므로 한 번 성공으로 판정하지 않는다.

detailed 로그는 `after_gc_collect` 뒤의 스냅샷 비교와 추적 종료를 구분한다. 전후 표본은 Python
예외로 작업이 종료된 경우에도 남으므로, 완료 표본만으로 작업 성공을 판정하지 않는다.
`sys.excepthook`은 있으며 `faulthandler`는 없다. 테스트는 진단 비활성의 무계측, 외부 추적 보존,
표본 범위·배치 결과·정리 순서 등을 확인하며 Windows 네이티브 경합 재현 검증은 아니다.
추가 진단은 문서·메일 본문과 복호화 평문을 저장하지 않는다. 덤프는 프로세스 메모리를
포함할 수 있으므로 사내 취급 절차를 확인하고 외부로 반출하지 않는다.

## 2. 상태 JSON과 유휴 스캔

### 현재 구조

문서 상태는 `index_state.json`이다. `collector/state.py`가 사이클 시작에 전체 JSON을
딕셔너리로 읽고, 수집기는 `state.get(path)`로 수정 시각·크기·포맷 정보를 비교한다.
파일마다 JSON 기록을 순차 검색하지 않는다. 변경 없는 문서는 추출·임베딩하지 않는다.

저장은 전체 JSON을 임시 파일에 쓴 뒤 교체한다. 현재는 상태가 바뀐 경우에만 기본
50건 또는 30초 확인 시점과 종료에 묶어 저장한다. 변경 없는 사이클은 문서 상태를 재저장하지
않는다. 기록에는 파일마다 `chunk_ids`도 포함되므로 비용은 파일 수뿐 아니라 청크 수에 좌우된다.

폴더 순회와 별도로 `CleanupManager._find_orphans()`가 기록된 파일마다 `Path.exists()`를
호출한다. 감시 폴더별로 상태 전체를 훑고 `_count_indexed()`도 다시 순회한다.
문서·메일 폴더 열거와 실제 파일 존재 확인 비용은 상태 조회 색인으로 없어지지 않는다.

### 합성 측정 (2026-10-06)

Windows·Python 3.11.0에서 합성 경로·현재 문서 상태 필드·UUID 청크 ID로 측정했다.
`json.loads`, 기존 `classify_changes`, `json.dumps(ensure_ascii=False, indent=2)`를 각각
5회 실행한 중앙값이다. 실제 파일의 내용은 사용하지 않았다.

| 파일 기록 | 파일당 청크 | JSON 크기 | JSON 해석 | 변경 비교 | 저장 문자열 생성 |
|---:|---:|---:|---:|---:|---:|
| 300 | 10 | 0.26 MiB | 1.0 ms | 0.1 ms | 6.1 ms |
| 3,000 | 10 | 2.60 MiB | 14.1 ms | 1.0 ms | 70.1 ms |
| 10,000 | 10 | 8.68 MiB | 63.1 ms | 4.8 ms | 266.1 ms |
| 3,000 | 100 | 14.45 MiB | 69.0 ms | 1.3 ms | 283.0 ms |
| 3,000 | 500 | 67.09 MiB | 260.3 ms | 1.0 ms | 1,104.9 ms |

디스크 I/O, 추적 활성 시 비용, SMB, Office, 실제 앱 전체는 포함하지 않는다.
비교는 `classify_changes` 측정이며 수집기 전체 시간이 아니다.
같은 로컬 파일을 3,000번 `exists()`로 확인한 참고 측정은 204 ms였지만,
서로 다른 3,000개 파일이나 SMB의 대용은 아니다.
사용자가 기억하는 3,000개 파일 환경의 심한 부하는 재현하지 못했다.

### 개선 시작 조건

먼저 상태 읽기·저장, 폴더 열거, 삭제 확인을 별도로 실측한다. 상태 전체 처리가
주요 비용이면 SQLite를 후보로 삼는다. 파일 경로를 키로 변경 행만 저장하며 JSON 이전과
LanceDB 저장 후 상태 복구를 설계한다. 상태 저장 개선이며 검색 벡터DB 교체가 아니다.
파일 접근이 주요 비용이면 열거 결과의 재사용을 검토하되, 접근 불가·열거 중단을
삭제로 오인하지 않는 안전 조건을 먼저 정한다.

## 3. 검색·인덱싱 개선 후보

| 후보 | 시작 조건·판단 자료 |
|---|---|
| 검색용 벡터·스칼라 인덱스 | 실제 행 수·검색 시간을 측정. 근사 검색은 재현율과 갱신 운용 검증 |
| 필요한 컬럼만 조회 | 현재 호출 위치 재확인. 과거 위반 목록을 그대로 적용하지 않음 |
| 질문 처리 비동기화 | [상세 설계](design/ISSUE_B_query_async.md)에 따라 UI 응답성과 스레드·종료 안전성 검증 |
| LLM 스트리밍 | 사내 API 지원 확인과 UI 비동기화 선행 |
| 추출·임베딩 병행 처리, 배치 조정 | 단계별 실측 후 단일 쓰기·평문 메모리 상한·취소·파일별 실패 격리 설계 |

검색은 문서 벡터 검색, 조건에 따른 경로 키워드 보강, 로컬 범위의 메일 검색을 수행한다.
항상 3회 검색하지는 않는다. 저장 규모는 원본의 ‘파일 수×용량’뿐 아니라
총 청크 수, 행 크기, fragment(저장 데이터의 작은 조각) 수로 평가한다.

기존 `[query]`·검색 결과 로그로 검색과 LLM 시간을 구분한다. 파일당 임베딩 약 20초,
DRM PowerPoint의 open 편차, COM 종료 유예 도입 후 12.2% 저하는 과거 보고이며
현재 환경의 측정값으로 취급하지 않는다. EDR·부하는 원인 후보이며 확정하지 않았다.

### 임베딩 미계측 지연 (2026-10-10)

사내 xlsx 인덱싱에서 32청크·시도 0 호출 네 건이 전체 5.81~6.44초, connect 0.00초,
ttfb 0.88~0.96초, read 0.03~0.33초, 미계측 4.82~5.52초로 보고됐다. 이전 미계측에는
응답 JSON 해석과 벡터 검증도 포함돼 이 로그만으로 원인을 확정할 수 없다. xlsx 추출과
DB 저장은 이 호출의 계측 범위 밖이다.

느린 호출 로그는 기존 connect·ttfb·read에 setup(연결 준비·취소 소켓 등록), json(응답 해석),
validate(벡터 검증)를 추가한다. 미계측은 이 여섯 구간을 전체 경과시간에서 뺀 나머지다.
ttfb에는 요청 전송과 응답 헤더 대기가 모두 포함된다. cpu는 같은 호출에서 해당 스레드가
사용한 CPU 시간이므로 구간 합계에 더하지 않는다. 경과시간에 비해 cpu가 작아도 특정
대기 원인을 단정하지 않는다([Python 문서](https://docs.python.org/3.11/library/time.html#time.thread_time)).
tracing·trace_depth는 느린 호출 기록 시점의 메모리 추적 상태이며 진단 기능을 켜거나 끄지 않는다.

로컬 합성 응답 32청크의 해석·검증은 메모리 추적 없이 약 0.03초, tracemalloc 깊이 10에서
약 0.27초로 사내 지연을 재현하지 못했다. 새 구간 로그와 실제
`collector.memory_diagnostics_enabled`와 `memory_diagnostics_mode` 설정으로 다음 확인 대상을 정한다. 본문·벡터·키는 기록하지 않는다.

### PPT 추가 기능 사내 검증 보고 (2026-10-13)

다음은 사용자가 시험 도구 결과를 전달한 보고다. 원본 로그 파일과 문서 본문을 확인하거나
보관한 결과는 아니다. 추가 기능은 호스트의 열린 문서에서 경로로 대상을 찾는 시험 DLL 0.1.3.0이다.

| 대상 | 보고된 기존/추가 기능 시간 | 결과 |
|---|---|---|
| 합성 문서 3회 | speedup 7.3, 301.18, 7.76 | 모두 pass·equal=true |
| VDI 실제 사내 문서 | 521.6초 / 5.08초, 로그 speedup 약 102 | pass·equal=true·source_unchanged=true |
| 원래 사내 PC의 문제 PPT | 약 46초 / 약 3.6초, 로그 speedup 12.86 | pass·equal=true·source_unchanged=true |
| 사내 PC의 추가 확인 | 총 3개 파일에서 유의미한 개선 보고 | 개별 추가 시간·원본 로그는 미제공 |

합성 2회차는 기존 약 1372초, 추가 기능 약 4.5초가 보고돼 기존 방식의 큰 지연이 포함됐다.
그 원인은 미확정이며 301배를 대표 개선값으로 쓰지 않는다. 표의 반올림 시간으로 로그 speedup을
다시 계산해 덮어쓰지 않는다. equal은 슬라이드 문자열·순서·건수와 오류 없는 결과를 기존 추출과
비교한 것이며 원본 화면 전체와의 대조나 두 방식의 공통 누락을 검증한 것은 아니다.

시험 중 COM 로드 충돌과 자동 로드 설정, 잘못된 Application identity 거부가 수정·재시험됐다.
최종 경로 기반 시험의 성공은 모든 PC의 설치나 대량 운영의 안정성을 증명하지 않는다.
[운영 설계](design/PPT_ADDIN_INDEXING.md)에 따라 별도 설치 도구·운영 리더·복구·로그를 구현하고
배포본에서 일반 권한 읽기, 여러 문서/사이클, 메모리·취소·Office 종료를 확인해야 한다.

### PowerPoint COM 슬라이드 읽기 (2026-10-11)

사내 로그 분석 보고에서 PPTX 추출은 전체 8201.9초 중 5578.9초(68.0%)이며, COM 단계의
read가 5507초로 보고됐다. 사용자가 같은 느린 파일을 VDI에서 측정한 결과는 전체 122.6초,
read 117.82초, 27장, 최장 슬라이드 17.79초다. 사내 PC의 같은 파일 read는 424초다.
후속 상세 계측에서 셀 읽기 구간이 주 병목으로 좁혀졌다. 실행 환경별 차이와 각 조회 안의
DRM 처리·COM 왕복·Python 래퍼 비용은 아직 분리하지 않았다.

`[ppt] 슬라이드 읽기`는 1부터 시작하는 순회 번호와 실제 방문한 도형·그룹·표·셀 개수,
최대 그룹 깊이, 오류 건수 및 구간 합계를 INFO에 기록한다. `operations`는
`구간:실행횟수/합계초`이며 구간 안의 모든 COM 속성 호출 수를 뜻하지 않는다.
`slides_next`·`shapes_next`·`group_next`는 순회 대기, `cell_text`는 표 셀 읽기,
`text`는 일반 텍스트 읽기다. 순회 횟수에는 마지막 StopIteration 확인도 포함된다.
`slide=0`은 슬라이드 목록·순회 준비, `shape=0`은 아직 특정 도형에 들어가기 전이다.

`[ppt] 도형 읽기`는 1초 이상이면 INFO, 그 외에는 DEBUG다. `shape=2.3`은 두 번째
도형의 세 번째 그룹 자식 위치이고 이름·ID·본문은 조회하거나 기록하지 않는다.
그룹 도형의 시간은 자식 시간을 포함하므로 부모·자식 시간을 더하지 않는다.
표의 행·열 개수는 기존 조회 결과를 재사용하고 셀 개수는 읽기를 시도한 셀 수다.
슬라이드별 진단 시간은 해당 슬라이드의 순회 대기부터 포함한다. 기존 최장슬라이드의
측정 경계는 유지한다. `[ppt] 읽기 구간 합계`는 슬라이드 목록 조회와 마지막 순회 확인을
포함한 파일 전체의 구간 합계를 기록한다.

`[ppt] 읽기 진행`은 한 구간이 응답하지 않으면 10초 간격으로 마지막 위치·구간·표 셀
좌표와 대기시간을 남긴다. 별도 스레드는 COM을 호출하지 않으며, 파일 읽기 종료 시
중지한다. 기존 read 단계·워치독 시작시각, RPC 단절 처리와 Office 종료 정책은 유지한다.

VDI의 기존 측정 스크립트에서 수정된 `PowerPointComReader.parse()`를 호출해 같은 파일을
비교한다. 완료된 슬라이드 요약과 느린 도형·진행 로그만 공유하고 추출 본문은 남기지 않는다.
추출 내용·표 셀·그룹 순서가 같음을 확인한 뒤 지연 구간을 기준으로 후속 최적화를 선택한다.

#### VDI 후속 실측과 일괄 읽기 조사

사용자 보고: 전체 추출 123.26초, read 118.44초, 표 셀 8,299개 읽기 91.45초
(read의 77.2%), 일반 텍스트 도형 1,682개 읽기 12.21초, 오류 0건이다.
10번 슬라이드는 표 1,550셀·17.83초, 17번은 그룹 포함 도형 913개·12.55초다.
직전 전체 122.6초와 비슷하며 계측 보강만으로 속도 향상을 확인한 것은 아니다.
오류 0건은 기존 추출 내용과 동일하다는 검증을 대신하지 않는다.

현재 셀 읽기는 `Cell(row, col).Shape.TextFrame.TextRange.Text` 전체를 한 구간으로 잰다.
8,299회는 셀 읽기 구간 횟수이며 정확한 COM 호출 수가 아니다. 셀당 평균 약 11ms에는
객체 접근·문자열 반환·Python 처리가 함께 포함되므로 DRM 비용이 없다고 결론내리지 않는다.

2026-10-11에 공식 API와 공개 구현을 확인한 결과:

| 경로 | 일괄 읽기의 의미 | 적용 조건 |
|---|---|---|
| 기존 PowerPoint COM | 문서화된 Table API는 Cell 접근을 제공하며 전체 값 배열 API는 찾지 못함 | 속성 조회 준비 비용 감소만으로 셀별 왕복을 없앨 수 없음 |
| Office.js Table.values | 표 전체 값을 string[][]로 로드. 표 목록 확보 후 여러 표의 load를 큐에 넣고 한 번 sync할 수 있음 | PowerPointApi 1.8. 확인된 사내 Office 2016에서는 미지원으로 현재 후보 제외 |
| PowerPoint 내부 추출 함수 | 외부의 한 번 요청에 내부 함수가 셀·그룹을 순회하고 문자열/배열을 한꺼번에 반환하는 설계 후보 | 승인된 추가 기능 설치·로드·스레드 처리 검증. 내부 셀 순회와 DRM 처리 비용 자체는 남음 |
| 파일/메모리 스트림 직접 파싱 | PPTX를 읽어 로컬 메모리에서 셀·그룹을 순회하며 COM을 사용하지 않음 | DRM 문서의 읽기 가능한 바이트를 승인된 경로로 확보할 수 있어야 함 |

Office.js의 [Table.values](https://learn.microsoft.com/en-us/javascript/api/powerpoint/powerpoint.table?view=powerpoint-js-1.8)는 공식 일괄 값 조회 API다.
[지원표](https://learn.microsoft.com/en-us/javascript/api/requirement-sets/powerpoint/powerpoint-api-requirement-sets)의 Windows 최소 버전은
2504·빌드 18730.20030이며 볼륨 라이선스 영구/LTSC 제품에는 1.8이 제공되지 않는다.
사내 제품은 Office 2016으로 확인됐고 VDI 제품·정확한 빌드는 미확인이다. SDK 내부의 실제 조회 비용과 DRM 호환성은 미검증이다.
사내 추가 기능은 [Office.js 로컬 참조](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/referencing-the-javascript-api-for-office-library-from-its-cdn)를 사용할 수 있지만
관련 파일과 실행 환경을 포함한 폐쇄망·숨김 문서 연동 검증이 필요하다.

내부 함수 후보는 [추가 기능의 COM 객체 노출](https://learn.microsoft.com/en-us/visualstudio/vsto/calling-code-in-vsto-add-ins-from-other-office-solutions?view=visualstudio)을 통해
외부에서 호출할 수 있다. 외부 호출을 Office 주 스레드로 전달해야 하며,
[VSTO 구조 문서](https://learn.microsoft.com/en-us/visualstudio/vsto/architecture-of-vsto-add-ins?view=visualstudio)는
자동화로 시작한 Office에서 정상 작동을 보장하지 않는다. 실제 로드·원문 미변경·사용자 문서
보호를 먼저 검증한다. 전체 문서 반환은 취소 간격·메모리 비용도 비교해 반환 단위를 정한다.
[Application.Run](https://learn.microsoft.com/en-us/office/vba/api/powerpoint.application.run)으로 별도 VBA 함수를
호출하는 시제품도 가능하지만 현재는 AutomationSecurity=3으로 매크로를 차단한다.
성능 실험을 위해 운영 보안 설정을 낮추거나 대상 문서에 코드를 주입하지 않는다.

유사 도구의 공개 구현:

- [Microsoft MarkItDown](https://github.com/microsoft/markitdown/blob/main/packages/markitdown/src/markitdown/converters/_pptx_converter.py): python-pptx로 파일 스트림을 열고 table.rows와 row.cells를 순회한다. COM 일괄 조회가 아니다.
- [Unstructured](https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/partition/pptx.py): python-pptx로 파일/스트림을 열고 셀 텍스트 행렬을 만든다. COM을 사용하지 않는다.
- [Apache POI](https://github.com/apache/poi/blob/trunk/poi/src/main/java/org/apache/poi/sl/extractor/SlideShowExtractor.java): 로드한 슬라이드 객체의 표를 행·열별로 순회하고 그룹을 재귀 처리한다.
- [Aspose.Slides](https://docs.aspose.com/slides/net/extracting-text-on-cloud-platforms-using-aspose-slides/): 파일/스트림에서 GetPresentationText를 호출해 슬라이드별 텍스트를 얻는 Office 독립 라이브러리다. 사내 DRM을 읽거나 현재 표 구조·순서를 동일하게 보존한다는 증거는 없다.
- [공개 VSTO 텍스트 추출 사례](https://note.com/mikan05/n/n9b644efd1e10): 추가 기능 안에서 표 셀을 순회해 하나의 문자열로 조합한다. 속도 수치는 없으며 예시의 클립보드 반환과 위치 정렬은 그대로 적용하지 않는다.

셀 순회 자체보다 PowerPoint 밖에서 매 셀마다 프로세스 간 요청을 반복하는지를 구분한다.
Office 2016에서는 승인된 내부 추가 기능 배포 가능 여부를 확인한다. 가능한 경로의
VDI 시제품을 기존 리더와 같은 파일로 비교해 전체·표 읽기 시간, 행·열·셀 수와 실제 문자열
동일성을 메모리에서 검증한다. 병합 셀·빈 셀·셀 내 줄바꿈·중첩 그룹도 비교한다.
내부 함수가 외부 왕복 비용을 줄일 것이라는 판단은 구조상 가설이며 아직 실측하지 않았다.
2026-10-11 당시 사내 PowerPoint 시제품·DRM 비교는 미실행이었다. 최신 사용자 보고는 위 §사내 검증 보고를 따른다.

대안 중 [getFileAsync](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/get-the-whole-document-from-an-add-in-for-powerpoint-or-word?tabs=powerpoint)는
전체 파일을 얻지만 공식 문서상 로컬 임시 복사본을 만든다. 평문 파일 비저장 계약을 충족한다고
가정하지 않는다. [형식 지원표](https://learn.microsoft.com/en-us/javascript/api/office/office.document?view=common-js)에서
PowerPoint는 Text 형식을 지원하지 않는다. 클립보드 Copy는 공유 클립보드를 변경하며
표 전체의 문자열/HTML 제공 보장과 DRM 허용 여부를 확인하지 못해 우선순위에서 제외한다.

#### 사전 검증 도구 확인 결과 (2026-10-11)

Office 없이 비교 계약·실문서 변환 차단·본문 미출력·오류 시 중단을 검증했다.
전체 회귀는 1,095건 통과·3건 건너뜀이며, 이후 추가·최종화한 PPT 회귀 24건도 통과했다.
C# 시제품은 .NET Framework 컴파일과 가짜 개체를 이용한 실제 순회 코드 검증을 통과했다.
PowerShell 등록 스크립트는 구문만 확인했다. 실행 정책으로 Build 실행이 제한돼 정책 변경 없이
컴파일러를 직접 사용했으며, 추가 기능 설치·등록 해제는 수행하지 않았다.

자체 검사 결과의 `performance_verified=false`처럼 위 결과는 성능 향상의 증거가 아니다.
Office 2016의 실제 로드·스레드 전달, 합성 PPT 변환, 사내 DRM 문서의 내용 보존·시간·종료 안전성은
미검증이다. 두 후보는 사전 비교 대상이며 운영 적용은 사내 실측 후 결정한다.

#### 다음 검증

사용자가 사내 Office 2016·인터넷 폐쇄망 조건을 확인했다. `Table.values`의 PowerPointApi 1.8은
Office 2016에서 사용할 수 없어 현재 시제품 후보에서 제외한다.

`scripts/diag_ppt_read.py`와 시험용 C# COM 추가 기능으로 기존 읽기·PPTX 변환·내부 순회를
비교한다. 실행·등록·판독은 [PPT 검증 안내](guides/PPT_PROBE.md)를 따른다. 운영 코드는 변경하지
않았다. 변환은 합성 파일만 허용하고 실제 문서는 저장 없이 기존 COM/내부 읽기와 비교한다.
추가 기능 등록은 사내 허용 후 사용자가 수행하며 자동 설치·정책 변경은 없다.
사내 Office·DRM 실측은 미실행이다. 같은 파일의 내용·표/그룹 순서·건수와 전체 시간 비교가 완료 기준이다.

추가 제안인 COM→PPTX 사본 저장→XML 읽기는 외부 셀 호출을 줄일 수 있으나
현재 대상은 이미 DRM PPTX다. 사본이 DRM으로 보호되면 직접 파싱 가능 여부를 확인해야
하며, 복호화 사본을 저장하면 CLAUDE.md의 평문 파일 비저장 계약과 충돌한다.
운영 경로에 임시 평문 저장을 도입하지 않는다. 이번에는 변환 실험을 실행하지 않았다.

## 4. Office 종료 정리 확인

`collector.com_quit_call_timeout_sec`는 Word·Excel의 `Quit()` 호출 제한(기본 10초)이며,
`com_quit_grace_sec`는 반환 후 자연 종료를 기다리는 유예(기본 5초, 0이면 생략)다.
종료 타이머는 COM 객체를 사용하지 않고 검증된 PID·생성 identity 사본만 정리한다.

- `Office Quit 반환/예외`와 `Office Quit 시간 초과 발화`로 호출 자체의 상태를 구분한다.
- `Office 정리 결과`의 `actual_exit`는 옛 대상의 종료 또는 PID 재사용 확인이다.
  `remaining`, `identity_unknown`, `failed`가 있으면 소유 기록을 보존한다.
- 핸들 열기·생성 identity 조회·종료 요청·종료 대기의 실패는 PID·EXE와 오류 코드로 기록한다.
  종료 요청 성공만으로 실제 종료를 판정하지 않는다.
- `Office 종료 미확인으로 … COM 연기`는 문서 자체 실패가 아니다. 같은 EXE는 30초 간격으로
  재정리를 시도하고, 그동안 실패 이력을 추가하지 않는다. 일반 OOXML 파싱·다른 Office는 계속된다.

타이머 정리가 진행 중이면 `callback_completed=False`로 남기며 성공 완료로 집계하지 않는다.
PowerPoint는 기존처럼 Quit·강제종료하지 않고, 자연 종료나 PID 재사용이 확인된 옛 기록만 제거한다.
WINWORD 잔존만으로 PPT까지 건너뛰었다고 단정하지 않는다. 실제 Windows·Office·DRM 환경에서
Quit 멈춤과 강제종료 실패 로그를 확인해야 하며, 강제종료가 실패하면 COM 호출 복귀는 보장되지 않는다.

### 메일 처리 중 강제 종료 후 Word 잔류 (2026-10-10)

문서 완료 시 Office 정리와 임베딩 취소를 구현하고 fake·localhost HTTP·HTTPS 회귀 검증을
완료했다. 실제 Office·DRM 검증은 사내에서 수행한다.
사용자는 PR #97 병합 버전에서 문서 처리가 끝나 메일 인덱싱 중인 상태로 트레이의 ‘종료’를
눌렀고, 앱 종료 후 작업관리자에 WINWORD가 남았다고 보고했다. 로그는 스케줄러 중지 → 수집기
취소 요청 → `[embed] 느린 호출` → 워커 종료 시간 초과 → `terminate()` → 하드 종료 순서다.

보고 당시에는 문서·메일 처리 뒤 `finally`에서 COM을 정리해 메일 처리 중에도 Word가 남을 수
있었다. 일반 종료 대기는 8초인데 임베딩 HTTP 연결·응답은 30초 대기 후 한 번 재시도할 수
있었다. 스레드 강제 중단·하드 종료는 COM 정리 완료를 보장하지 않는다. 당시
`COM 블로킹 추정`은 워커 종료 시간 초과의 공통 문구로 실제 COM 지연을 확정하지 않는다.
임베딩 대기가 취소 처리를 늦췄다는 것은 가설이며, 요청 소요시간과 잔류 PID의 소유·정리
로그를 추가 확인해야 한다.

현재 동작과 검증 기준:

1. 문서 처리가 끝나면 생성 워커에서 Word·Excel을 정리한다. 사이클 종료의 최종 정리도 유지하고
   정리 미확인 상태를 보존한다. 메일 자체는 Office 정리 실패와 무관하게 진행한다.
2. 수집 취소는 해당 워커의 활성 HTTP 소켓을 중단하며 임베딩 재시도·후속 배치를 차단한다.
   스레드별 연결로 검색 요청을 보호하고, 취소된 메일은 기존 데이터·실패 이력을 유지한다.
   임베딩·Office·DB 정리 중 종료에는 최대 60초 유예를 주며 실제 작업 단계를 기록한다.
3. fake·localhost 회귀로 문서→메일 정리 순서와 느린 임베딩 취소를 검증하고, 사내에서
   검증된 소유 Word·Excel의 실제 종료와 사용자 Office 보호를 확인한다.

이미 연결된 응답 대기는 소켓을 중단하지만 DNS·연결 수립 자체의 즉시 취소는 보장하지 않는다.
OS·HTTP 연결 제한과 종료 유예로 기다리며, 워커가 끝내 복귀하지 않으면 기존 강제 종료를 유지한다.

PID·생성 identity로 검증된 소유 프로세스만 정리하고 PowerPoint Quit·강제 종료 금지를
유지한다. 하드 종료 경로의 무조건 종료를 유지하며 그 경로에 블로킹 정리를 추가하지 않는다.

## 설치 부모 TrustedInstaller 신뢰 누락 (2026-10-26)

사내 업데이트의 `code=13`, `install_root_policy / program_files_parent / root_write_untrusted`에서
거절 ACE가 Windows TrustedInstaller의 FullControl·현재 폴더 전용·상속 항목 하나라는 보고를 받았다.
해당 부모는 `ProgramFiles/AegisDesk`이며 소유자는 Administrators, Reader 루트와 다른 읽기 ACE는 정상이다.
기존 `Protect`가 Admin/System 이외의 쓰기를 모두 거절해 Windows servicing SID를 누락한 것이
이 설치 실패의 직접 원인이다. 요청 부모의 과거 `legacy_write_missing`과 구분한다.

1.0.5.15 후보는 고정 설치 부모·정확한 TrustedInstaller SID·None inheritance/propagation·
FullControl 상한에만 쓰기 예외를 적용한다. 이름이나 service SID 접두사로 허용하지 않는다.
Reader 루트·버전 폴더·파일, 다른 주체의 쓰기, 다른 상속 scope에는 적용하지 않는다.
소유자 조건은 계속 Admin/System이며, 기존 부모 ACL은 수정하지 않는다.
Reader/Events/setup·요구 버전과 시험 fixture는 1.0.5.15로 맞춘다. 일반 배포 자동종료 정책과
기존 요청/복구/롤백 계약은 유지한다. 실제 사내 업데이트와 PowerPoint 종료 성공은 별도 검증한다.

Linux 전체 `pytest knowmate/tests -v`는 **2,319 PASS·121 SKIP·74 subtests PASS**, exit0,
62.05초다. 실제 제품 C# 판정 소스를 .NET 8 시험 프로젝트로 실행한 27개 경계 시험도 포함한다.
이는 Windows ACL API 시험이 아니다. 기존 main에서도 실패하던 정상 default interface 기대값을
현재 `IPptReaderClientShutdown`에 맞춰 갱신했으며 제품 인터페이스는 바꾸지 않았다.
Windows x86/x64 ACL·실제 UAC 업데이트·제품 DLL/EXE 빌드·Office/DRM은 **UNEXECUTED**다.

## PPT 관리자 helper 진단 전달 (2026-10-26)

사내 1.0.0.0→1.0.5.13 업데이트에서 UAC 이후 `code=13 / mutation_failed`가 남고 journal·
새 버전 복사·등록 전 실패했다는 사용자 진단 보고를 받았다. 배포 버전/해시와 기존 설치 기록은
정상이라는 보고이며, 실제 사내 API/OS 오류는 아직 확정하지 않았다. `ProgramData/AegisDesk`의
요청 생성 성공과 `ProgramFiles/AegisDesk/PptReader`의 설치 ACL 검증은 별개다.

코드에서 승격 자식의 상세 오류를 일반 helper가 exitcode로만 수신하고 최종 `mutation_failed`로
축약하는 경로를 확인했다. 기존 사용자 소유 임시 폴더의 실제 Protect 메서드 사본은 ACL을
바꾸지 않은 채 `acl_unavailable`/code13으로 실패했고 journal은 생성되지 않았다. 이는 가능한
실패 경로의 컴포넌트 재현이며 사내 원인 확정이 아니다.

1.0.5.14 후보는 일회성 local pipe로 phase별 마지막 단계와 최초/후속 오류의 고정 scalar를
전달한다. 원래 오류가 wrapper/상태로 바뀌기 전에 포착하며 예외 메시지·stack·경로·SID·본문은
제외한다. 진단 수집 실패는 기존 설치 결과·독립 안전 점검·예약/저널/ACL 정책을 바꾸지 않는다.
설계와 판독은 [§8-8](design/PPT_ADDIN_INDEXING.md#8-8-관리자-설치-진단-전달-후보) 및
[안내](guides/PPT_READER.md#관리자-설치-실패-진단)를 따른다. 의존성 추가는 없다.

최종 소스에서 주 에이전트가 직접 실행한 전체 `pytest knowmate/tests -v -W error`는
**2,407 PASS·6 SKIP·74 subtests PASS**, 경고 보고 없음, exit0, 1228.13초(20:28)다.
로그와 종료 코드는 `.aegisdesk_build/ppt-helper-diagnostics/full-pytest.log` 및
`full-pytest-exit.txt`에 있다. Python 계약/로그 관련 358개(34.75초), 통신·컴포넌트 54개와 기존
부모 회귀 8개를 합친 62개(178.60초)도 통과했다. 부분 시험은 전체와 중복되므로 합산하지 않는다.
손상된 선택 진단·직접 주입·다른 작업의 stale phase를 폐기하고 독립 사후 점검을 유지했다.

실제 일반 helper→동일 계정 runas 관리자 child의 두 컴포넌트 사례는 실제 token·pipe·Anonymous
SQOS와 원래 Protect의 `root_write_untrusted`를 확인했다. 관리자 token impersonation의 token
open이 1347로 거절되고, child13의 관측이 일반 helper로 전달됐다. 이 결과는 제품 설치 전체
시험과 구분한다. 최종 근거는 `.aegisdesk_build/hdc/ecb36361/parent-verification.json`이다.

별도의 실제 UAC 관리자 controller와 그 token을 상속한 실제 child 흐름은 private 고정 경로와
HKCU 시험 경계에서 Main→Mutation/Check/native writer/pipe를 실행했다. 19개 on/off 쌍,
38개 관측 모두 통과했고, 주 에이전트가 실제 Python parser로 38개를 재검증했다. 19쌍의 상태·
종료코드·저널·예약은 같았으며 rollback 두 사례의 `secondary.target=record`도 확인했다.
ShellExecuteEx·Office·고정 설치 경로·HKCU는 시험 경계이므로 위 ordinary→runas 시험과
혼동하지 않는다. 최종 근거는 `.aegisdesk_build/hdf/55a02871/parent-verification.json`이다.
두 최종 fixture의 제품 C# 25개 SHA는 현재 소스와 모두 일치한다.

최초 flow에서는 실제 rollback File.Replace의 HRESULT 80070497(1175) 때문에 on/off 상태가
달라져 실패했다. 원래 primary와 실제 secondary가 분리된 증거는 보존하지만 전체 통과로 세지
않으며, 일반 record/journal 재시도는 추가하지 않았다. 다른 긴 경로 fixture의 요청 저장 실패는
원래 JSON이 없어 MAX_PATH·WinError를 확정할 수 없다. 위 최종 결과는 짧은 새 fixture에서
검증했고, 이전 checkpoint·848c 실행이나 rollback target 수정 전 61%에서 중단한 전체 실행을
최종 소스 성공 증거로 사용하지 않는다. 상세 검토는
`.aegisdesk_build/plan-review-loop/ppt-helper-diagnostics/code-review.md`에 보존했다.

candidate payload 빌드·무결성 검증은 `.aegisdesk_build/ppt-helper-diagnostics/payload-build-*`의
별도 기록을 따른다. 전체 앱 EXE·운영 설치·실제 사내 실패·Office/PPT 자동종료·원격 UNC·다른
관리자 계정은 미검증이다. 앱이 설치 결과 관찰을 끝내거나 종료한 뒤의 영구 로그 보관/재수집은
추가하지 않았다. 이전 .13 결과를 .14 사내 설치 성공으로 바꾸어 해석하지 않는다.

## PPT ACL 표현 호환 (2026-10-25)

후속 경고 정리: 아래 최초 전체 회귀의 경고 128개는 LanceDB `table_names()` 폐기 예정 API
경고 124회와 PPT native 시험의 한글 컴파일 출력 UTF-8 해독 예외 4회다. 메일·검색·시험의 목록
조회는 `list_tables().tables`로 교체했고, 현재 고정 버전의 로컬 DB에서 14개 테이블 이름을
모두 조회하며 기존 메일 데이터를 유지하는 회귀를 추가했다. 컴파일러 `/utf8output`과 Python의
명시적 UTF-8 수집으로 한글 진단을 보존한다. 경고 무시 설정이나 의존성 변경은 없다.
후속 전체 `pytest knowmate/tests -v -W error`는 **2,257 PASS·6 SKIP·74 subtests PASS**,
경고 보고 없음, exit0, 1366.64초다. 관련 34개도 `-W error`에서 통과했다. 근거는
`.aegisdesk_build/warning-cleanup/full-pytest.log`와 `report.json`이다. 이 후속 소스의 앱 EXE
재빌드·실제 Office/사내 검증은 실행하지 않았으며, 아래 최초 후보 패키지의 소스 지문과 구분한다.

1.0.5.13 후보는 부모 ACL을 exact mask 대신 역할별 unsigned generic mapping·권한 상한으로
분석한다. Users/CI 쓰기 0x116/0x100116·generic·부분/분리 권한을 지원하고 잔여 읽기·실행·sync를
보존한다. 외곽 ProgramFiles/AegisDesk의 TI/CO만 같은 의미의 허용 scope/권한을 수용한다.
strict child/request/backup과 일반 parent 정책은 유지한다. 원시 ACL/identity 재검사와 exact native
readback을 완화하지 않는다. 상세 정책은 [설계 §8-7](design/PPT_ADDIN_INDEXING.md#8-7-구버전-부모-폴더-권한-복구-후보)을 따른다.

백업 없는 명시 회수는 현재 Repairable/normal 거절/legacy 1.0.0.0/프로세스 종료를 같은 mutex에서
검증하고 예약 state만 complete로 바꾼다. protected 원본과 중간 상태를 구분하지 않으며 현재
recovery lease부터 완료까지 원시 ACL·identity를 보존한다. 결과는 outdated/unsafe 및
`reservation_released_retry_required`이고, 새 prepare에서 새 UUID·정식 백업으로 재시도한다.

Windows가 generic 상속 쓰기를 현재 객체/디렉터리 후손의 두 ACE로 나누는 실제 저장 표현은
정확한 쓰기 기여 쌍만 허용한다. 외곽 TI의 CI(non-IO)도 None+CI|IO와 현재/후손 coverage가
동일한 표현으로만 허용한다(`ac92fcb1` 격리 실측). OI·NP·다른 SID/불일치 쓰기는 거절한다.

새 matrix에서 기존 managed File.Replace의 1175를 반복 관측했다. 실패 순간 source/target의
DELETE/WRITE/WRITE_DAC 실제 open은 성공했고 exact backup ACL·원래 두 이름과 hash가 보존됐다.
지속 권한 부족이나 특정 외부 locker가 원인이라고 확정하지 않는다. 백업 전용 저장은 1175에만
ID/hash/raw 보안/루트/다른 partial을 재검사한 뒤 최대 한 번(100ms) 재시도하도록 제한했다.
일반 메타데이터 저장이나 다른 HRESULT에는 적용하지 않는다.

초기 standalone AtomicProbe는 같은 FileSecurity 객체를 재사용해 두 번째 temp ACL의 변경 표시가
소모된 시험 준비 오류가 있었다. 해당 128쌍의 AdminOnly/동일 ACL 결과는 증거에서 제외한다.
최종 fixture는 파일마다 새 ACL을 적용하고 실제 readback과 strict ACL을 첫 시도 전에 검사한다.
입력 상속 설정의 재상속 원복·AI controlbit 정규화·Access-only 쓰기 문제도 준비 오류로 구분했다.

확정한 .13 회귀는 다음과 같다. 아래 .11 수치를 새 후보의 검증으로 사용하지 않는다.

- 관련 pytest **8 PASS**, 40.09초. native x86/x64 각각 직접 CI 입력 32변형·실제 kernel generic 쌍 6변형의 두 native 저장, 최종 모든 Users 후손 W=0/현재 P상한, scope별 읽기/실행/sync 잔여·하위 ACL/내용 보존을 확인했다. unknown/불일치 쌍·scope·위험 권한 거절과 실제 OS 접근 거부도 구분했다.
- 격리 lifecycle **133사례 PASS**: 실제 저장 입력/상속을 재조회한 부모6×보호2×분리2×외곽4=96 인계, 백업 commit fault12, raw/group 경합2, 실제 Main 오류6, backup 없는 ACL 무변경 회수·새 UUID 재시도3, 기존14회귀. helper의 논리 승격/liveness와 HKCU 격리 등록·Office closed/64는 모형 경계다. 합성 legacy DLL의 native 파일/해시/등록 증명·backup·namespace lease·native DACL·예약 writer·핵심 Run/Elevated/Recover는 실행했다.
- fault gate는 매 파일의 실제 strict ACL을 먼저 검증하고 clean/permanent1175는 정확히 2attempt, 다른 HRESULT·file/root identity/hash/ACL/group·partial 변조는 첫 시도 후 거절했다. 무주입 core의 첫 Replace207회 중 실제1175가2번 발생해 각1회 엄격 재검사 후 완료했다.
- 실제 Main의 retry JSON3개와 실패 JSON6개를 Python parser의 candidate 정책에 연결해 검사했다. encoded 역사 URL은 일반 checker에서 repairable일 수 있으므로, 예약-only 결과는 같은 mutex에서 전용 legacy 증명으로 관측한 outdated/unsafe snapshot이다. 일반 checker 정책은 바꾸지 않았다.
- 실제 ordinary helper35312→같은 계정 UAC admin child33036에서 private parent의 0x116 실제 입력, native2저장, child exit0와 정상 lease→예약 완료를 확인했다. 전체 제품 설치·다른 관리자·lifecycle의 모든 다중 프로세스 경합을 통과한 것으로 해석하지 않는다.

최종 lifecycle 증거는 `.aegisdesk_build/plp/b4cfbf41`, 실제 UAC는
`.aegisdesk_build/pu13/case-0533613b4a434a2fb2746daf1d83c18b`다. 각 증거의 C#20개 원본 hash를
현재 소스와 대조했고 mismatch0, lifecycle ParentRepair 핵심은 byte 동일이다. helper 종료를
확인한 뒤 실행한14개 `.plp` GUID의 HKCU 시험 키만 정리했고 운영 레지스트리는 변경하지 않았다.
결과 요약은 `.aegisdesk_build/ppt-acl-portability`에 보존했다. 첫 UAC fixture의 audit 변경 표시·
경로/ACL 준비 실패는 제품 성공/실패 수치와 분리했다.

필수 `.venv/Scripts/python.exe -X utf8 -m pytest knowmate/tests -v`는 **2,256 PASS·6 SKIP·74 subtests PASS**,
exit0, 1025.57초(17:05)다. 경고128개에는 기존 capture-buffer native 시험4개의 subprocess UTF-8
컴파일 출력 해독 경고가 포함되며 새 ACL 복구 시험에서는 발생하지 않았다. 로그는
`.aegisdesk_build/ppt-acl-portability/full-pytest.log`, 종료코드는 `full-pytest-exit.txt`에 보존했다.
candidate payload 빌드·검증 결과는 `.aegisdesk_build/ppt-acl-portability/payload-build-*`의 별도
기록을 따른다. 전체 EXE 빌드·실제 Reader 설치/Office·사내/도메인·다른 관리자 검증은 미완료다.
실제 ProgramData/HKLM/Office 설치는 변경하지 않았다.

## PPT 구버전 부모 폴더 권한 복구 (2026-10-25)

1.0.0.0 설치가 남긴 ProgramData 부모의 상속 Users Write 때문에 `acl_unavailable /
parent_policy / policy`가 발생할 수 있다. 일반 ACL 검사를 완화하지 않고 정확한 legacy 설치와
ACL을 확인한 뒤 부모 DACL만 좁히는 1.0.5.11 후보를 구현했다. 구현 계약은
[설계 §8-7](design/PPT_ADDIN_INDEXING.md#8-7-구버전-부모-폴더-권한-복구-후보), 판독·운영 절차는
[Reader 안내](guides/PPT_READER.md#구버전-부모-폴더-권한-복구-후보)를 따른다.

### 확인한 결과

Windows 11 Pro 10.0.26200·로컬 NTFS·.NET Framework compiler에서 아래를 실행했다.
이번 작업은 실제 ProgramData 권한·Reader 설치·HKLM 등록·Office를 변경하지 않았다.

| 검증 | 결과와 범위 |
|---|---|
| native 일반/특수 lease | 일반 lease는 역사적 ACL을 거절하고 특수 lease만 기존 schema 1 reserved 생성·읽기·complete 저장 성공. 일반 토큰의 elevated 전이는 거절 |
| native parent-only 저장 | `NtSetSecurityObject(info=4)` 두 번으로 부모 protected 및 최종 ACL 확인. protected/unprotected·다단계 하위 owner/group/DACL·합성 내용 hash 유지. 첫 저장 전/후 sibling 파일·디렉터리 생성 보존 |
| native handle 공유 | namespace READ pin과 보안 WRITE_DAC handle 동시 취득. 새 write/delete 및 선점 write handle 충돌 확인. read-only handle의 DACL 쓰기 거절 |
| 입력·복구 거절 | 낯선 SID/deny/mask·flag 변형, 다른 owner·sibling, NULL/generic/불량 감사 ACL 거절. backup 없는 protected explicit 원본은 명시 회수도 거절 |
| 역사적 checker | 실제 `70fe63f`와 `c5c638a`의 1.0.0.0 소스를 컴파일해 새 요구 버전 schema 1 reserved/elevated를 모두 pending 처리. 경로 binding만 fixture로 치환 |
| 현재 Python checker | 실제 native writer로 만든 reserved 파일을 `_machine_records_safe`가 보류 |
| 실제 UAC 2-process | ordinary helper→동일 계정 관리자 UAC child의 실제 token/image/payload 검사, reserved→elevated, native DACL 두 단계, 자식 종료 관측 후 ordinary 정상 lease→complete 성공 |
| 생산 lifecycle 핵심 | 원본 Run/Elevated/Recover와 실제 backup·writer·lease·legacy 파일/등록 검사를 아래 OS 경계만 격리해 **14사례 PASS** |

lifecycle 14사례는 정상 복구→설치 reserved 인계, 같은 1.0.0.0의 encoded/unescaped CodeBase,
UAC 취소, unknown code·child 보류와 늦은 child 거절, backup 생성 전/후·각 native 저장 직후 중단,
backup 없는 원본 예약 회수 및 backup 있는 안전한 전진, backup 누락/temporary/내용 손상/불량
ACL 거절, sibling 보존·pending이다. 각 예약 commit에서 별도 스레드가 동일 mutex를 얻지 못해
복구 complete→설치 reserved 인계가 같은 mutex 구간에 있음을 확인했다.

이 lifecycle controller는 실제 관리자 토큰에서 실행했지만 **논리 Admin·process-gone·동기
Elevation은 시험 binding**이다. known-folder 경로는 고정 workspace fixture, HKLM/HKCU는
`HKCU\Software\AegisDeskRepairFixture\<시험 GUID>` 아래, Office는 closed/64로 격리했다.
protected 1.0.0.0 fixture assembly·record·SHA·두 역사적 URL의 등록 비교, backup 보안·저장,
Nt DACL·일반/특수 lease·writer는 실제 코드를 실행했다. 사본의 `ParentRepair` 핵심은 원본 byte와
동일하며 `provenance.json`에 원본·binding SHA 및 경계를 기록했다. 실제 다른 사용자/다른 관리자
UAC·다른 프로세스 간 모든 중단·동시 실행·Reader 업데이트를 통합 통과했다고 해석하지 않는다.

### 회귀와 보완

- 새 pytest **6 PASS**, 기존 request writer 행렬 포함 **14 PASS**, 계약·버전 관련 **287 PASS·3 SKIP**.
- 필수 `.venv/Scripts/python.exe -X utf8 -m pytest knowmate/tests -v`: **2,216 PASS·6 SKIP·74 subtests PASS**, exit 0, 1046.89초. 경고 128개에는 기존 capture-buffer native 시험 4개의 subprocess UTF-8 출력 해독 경고가 포함된다. 새 repair 시험에서는 이 경고가 발생하지 않았다.
- 첫 전체 실행은 후속 리뷰에서 구버전 ProgramFiles 외곽 container의 상속 호환 문제가 발견되어
  56% 진행 상태에서 중단했다. 미완료 결과를 최종 통과 수치로 사용하지 않는다. 역사적 Protect가
  Reader 루트만 보호해 부모는 상속 상태였음을 fixture에 반영했고, 실제 ProgramFiles DACL을
  private anchor에만 복사해 검증했다. 외곽에만 정확한 TrustedInstaller/CREATOR OWNER 상속
  예외를 허용한 뒤 최종 lifecycle 14사례와 native 거절 행렬을 다시 통과했다.
- 첫 통합 시험에서 Windows wildcard `id.json.*`가 base `id.json`까지 반환해 정상 backup을
  temporary로 오인한 결함을 발견했다. 전체 파일명에 `id.json.` exact prefix를 적용하고 base-only/
  실제 temporary 회귀 및 최종 14사례로 재검증했다.
- 설치 경로 pin에 FILE_LIST_DIRECTORY/FILE_READ_DATA를 포함해 native 공유 제약을 유지했다.
  native 저장은 AUTO_INHERITED 제어 비트를 정규화하므로 예상 DACL도 그 비트를 제거해 비교한다.
- 처음 실패한 lifecycle 시도는 fixture의 lease 경로 binding 누락으로 실제 ProgramData의 읽기
  검사에서 거절됐다. 예약·권한 변경 전 종료했다. 이후 모든 known-folder 참조를 치환했다.
  긴 시험 경로의 MAX_PATH 실패와 사본의 LF/CRLF hash 차이도 시험 도구 문제로 분리해 수정했다.

격리 증거는 `.aegisdesk_build/pl/1ff4d5f4`(최종 lifecycle),
`.aegisdesk_build/ppt-parent-uac-fixture/case-778808b2e07e4b71a2776fdb881ac8bd`(실제 UAC),
`.aegisdesk_build/ppt-parent-repair/full-pytest.log`에 보존한다. 실패 시도는 성공 건수에 넣지 않는다.
수동 lifecycle의 재현 빌더는 `knowmate/tests/ppt_parent_lifecycle_build.py`이며 무인 pytest는
UAC를 실행하지 않는다. 시험 helper/controller 종료를 확인한 뒤 컴파일 GUID에 해당하는 HKCU 시험 키만 정리했으며, 운영 레지스트리는 변경하지 않았다.

### 남은 승인 범위

현재 설치·과거 시험 DLL은 유지한다. 새 candidate payload의 컴파일·manifest 점검과 실제 기존
1.0.0.0→1.0.5.11 설치, 원래 사용자 ready, 구형 `.ppt`/fake embedding 인덱싱 후 동일 PID 종료,
저장/미저장 사용자 문서 보호는 구분해 검증해야 한다. 동일 버전의 과거 시험 DLL을 새 payload로
덮어써 설치하지 않는다. 다른 Office/OS·사내 DRM·A/B/C 계정·장시간은 미검증이다.

## 5. 완료된 개선과 과거 분석

현재 완료 상황은 [수정노트](UPDATE_NOTES.md)에서 확인한다. 주요 개선은
재인덱싱 시 기존 데이터 보존, 메일 후보 누락 방지, 성공 캐시, 상태 저장 집약,
fragment 수 기반 DB 정리, Office RPC 단절 복구다.

- [PERF_ANALYSIS.md](archive/PERF_ANALYSIS.md): 2026-09-06 분석 초안. 일부 근거가 반박됐으며 현행 지시가 아님.
- [PERF_ANALYSIS_REVIEW.md](archive/PERF_ANALYSIS_REVIEW.md): 교차검증·결함 발견 이력. 발견 후 수정된 항목 포함.
- [ISSUE_B_query_async.md](design/ISSUE_B_query_async.md): 질문 비동기 실행·읽기/대화/종료 보호 계약. 실제 사내 Windows·Office 장시간 검증은 남아 있음.

과거 3,000개 파일 부하와 ‘상태에 색인 추가’의 정확한 기록은 이번 문서·Git 이력 검색에서
찾지 못했다. SQLite 보류 기록만으로 같은 논의였다고 단정하지 않는다.
