# 운영 PPT Reader 개발 검증

현재 요구 버전은 **1.0.3.0**이다. 기본 빌드는 자동 설치를 끈 `disabled`이며,
개발 빌드의 `candidate`/`released`에서 앱 시작부터 자동 설치를 실행한다.
시험 DLL의 내용 일치/속도 결과를 운영 DLL·자동 설치의 성공으로 간주하지 않는다.
아래 명령은 Windows 개발 PC에서 실행한다. 문서 원문은 로그/파일로 기록하지 않는다.

## 자동 설치 후보 빌드와 첫 실행

미설치 시험 PC/VM에서 PowerPoint와 기존 AegisDesk를 정상 종료한다.
시험용 사용자 데이터와 소수 시험 문서를 준비하며 운영 인덱스나 등록을 임의 초기화하지 않는다.
일반 권한 PowerShell에서 다음을 실행한다.

```powershell
.\build.bat clean candidate
if ($LASTEXITCODE -ne 0) { throw 'candidate_build_failed' }
& .\dist\candidate\AegisDesk\AegisDesk.exe
```

별도 DLL 등록 명령 없이 앱이 설치 상태를 점검하고 필요할 때 도구만 UAC로 승격한다.
승격 자식의 실제 종료 후 원래 사용자 재점검이 정상이어야 준비 완료다.
UI는 계속 사용할 수 있고 설치 관찰 동안 최초 인덱싱은 대기한다.
거절·보류 후 Machine 안전이 확인되면 auto는 기존 COM을 사용하고, 안전 미확인은 PPT만 보류한다.

일반 빌드는 `build.bat` → `dist\disabled\AegisDesk`, 후보는 `dist\candidate\AegisDesk`,
최종 승인 시험 빌드는 `build.bat clean released` → `dist\released\AegisDesk`다.
선택 모드의 폴더만 정리하므로 후보 빌드가 일반 배포 결과를 덮어쓰지 않는다.
`fast`는 개발 반복용이며 배포하지 않는다. 모드는 빌드 출처·C# 상수·manifest에 함께 내장된다.
설정 파일·실행 환경변수·manifest만 바꿔서 자동 설치를 켤 수 없다.

## 빌드와 읽기 전용 점검(개발 도구)

PowerPoint를 정상 종료하고 저장소 폴더의 PowerShell에서 실행한다.

```powershell
& .\.venv\Scripts\python.exe .\scripts\build_guard.py --output-dir .\.aegisdesk_build\manual --ppt-auto-install-mode disabled
if ($LASTEXITCODE -ne 0) { throw 'build_guard_failed' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\ppt_reader\build.ps1 -BuildInfoDir .\.aegisdesk_build\manual
if ($LASTEXITCODE -ne 0) { throw 'ppt_reader_build_failed' }
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /check
```

`/check`는 등록을 변경하거나 PowerPoint를 실행하지 않는다. `ready`는 요구 버전의
파일·Machine 등록 확인이며 실제 PowerPoint 내부의 로드·읽기 성공을 뜻하지 않는다.
`machine_safe=false`이면 PPT COM 수집을 미룬다. disabled/candidate의 `auto_install_validated`는 false다.
candidate는 자동 실행이 허용되며, true/false 표시 자체는 Windows 시험 완료 증거가 아니다.

## 기존 설치 준비 폴더와 native lease 검증

기존 1.0.0.0 설치를 업데이트할 때 AppData·ProgramData·dist를 지워야 하는 것은 아니다.
`%ProgramData%\AegisDesk`가 상속을 허용하고 준비 자식만 보호된 과거 구조도,
실제 owner/DACL이 안전한 경우 그대로 재사용한다. 부모 false·자식 true만으로 성공을 판정하지 않는다.
기존 owner·DACL·파일·구버전 DLL은 보존한다. 수동 ACL 변경이나 관리자 앱 실행을 선행하지 않는다.

먼저 일반 권한 Windows 개발 PC의 NTFS 임시 루트에서 아래 테스트를 실행한다.
운영 ProgramData를 수정하는 테스트가 아니다. 관리자 실행 또는 Windows 외 환경의 skip은
필수 native 검증 **미실행**이다.

```powershell
& .\.venv\Scripts\python.exe -m pytest .\knowmate\tests\test_ppt_acl_native.py -v
```

stdout 상속·실제 자식 종료·마지막 handle 삭제 시험도 일반 권한으로 실행한다.

```powershell
& .\.venv\Scripts\python.exe -m pytest .\knowmate\tests\test_ppt_installation.py -k windows_helper_stdout_survives -v
```

하네스가 시간초과하면 `PPT ACL timeout: phase=compile|atomic_control|harness last_stage=... stage_started_ms=... limit_sec=30`의
고정 값을 확인한다. `last_stage=unavailable`은 단계 출력 미수집이며 원인 확정이 아니다.
시간초과는 통과로 바꾸지 않고, 마지막 단계로 컴파일·junction 생성·lease·원자 저장·정리를 구분한다.
먼저 lease 없는 `atomic_control`에서 동일한 실제 저장 함수를 실행한 뒤 `harness`에서 lease를 보유한다.
`atomic_file_open/atomic_disk_flush/atomic_acl_apply/atomic_move_call` 등으로 저장 내부 위치를 좁힌다.
`stage_started_ms`는 해당 단계 진입 시점으로, 30초 누적 소요와 특정 단계의 정체를 구분할 근거다.
세부 계측 구현은 native 시험 실행 파일에만 포함하고 일반 setup에서는 partial 호출이 제거된다.
stdout 파일 열기 거절은 Win32 오류뿐 아니라 CRT의 `errno=EACCES/EPERM`도 인정한다.
파일 없음·일반 I/O 오류는 거절 성공으로 인정하지 않으며 ACL·상속·실제 종료·삭제 검사는 계속 수행한다.
관찰 해제는 설치 도구의 실제 종료로 기록하지 않는다. 닫은 handle을 `Popen` 소멸자가 다시 조회하지 않도록
명시 해제 여부를 유지하며 실제 종료 판정은 열려 있는 관찰 handle로만 수행한다.

이 하네스는 실제 `Directory.CreateDirectory`로 상속 부모를 재현하고 보호 자식을 생성한다.
누락 폴더의 원자 생성과 두 lease 동시 열기, anchor/부모/자식 rename·자식 삭제 거절, 예약 파일 원자 생성·교체,
기존 owner/DACL 불변과 기존 DELETE handle 충돌을 검사한다. 같은 프로세스의 두 handle 시험은
실제 helper·승격 자식의 다른 계정 권한 시험을 대신하지 않는다.

그다음 candidate EXE로 기존 1.0.0.0 → 1.0.3.0 업데이트와 새 설치를 각각 확인한다.
필수 기록은 `required_version=1.0.3.0`, 실제 `installed_version`, 일반 helper/승격 자식 종료,
원래 사용자 `/check`의 `ready`, 인덱싱의 `method=addin`이다. 같은 계정 및 다른 관리자 UAC,
앱만 종료했을 때 helper/자식 생존·stdout 쓰기/마지막 handle 정리, 점유/거절/공유 충돌,
중간 helper 종료·자식 생존·`/recover`와 32/64비트 Office는 별도 실제 시험한다.

사용자 간 시험은 A=디렉터리 owner, B=예약 owner, C=비소유 일반 사용자로 구분한다.
C의 B 예약 갱신·삭제는 거절되고 B의 자기 예약과 Admin/System 작업은 가능해야 한다.
A의 암묵적 WRITE_DAC에 의한 DACL 변경은 lease로 막지 못한다. owner까지 적대적으로 격리해야 하면
이 후보를 활성화하지 말고 관리자 소유 저장 구조 등으로 재설계한다.

실패 로그는 고정 요약 code와 검증된 `reason/stage/error_type/winerror`를 표시한다.
`parent_open/parent_policy/child_open/child_policy/request_create/request_acl/request_commit`으로
실패 위치를 구분하며 실제 Win32 오류가 없으면 `winerror`는 null이다. 원문 예외·SID·SDDL은 쓰지 않는다.
미수집·잘못된 출력은 `diagnostic_unavailable`이며, 최종 안전 여부는 독립 `post_check`가 결정한다.
예약 전 실패 후 safe 구버전이면 auto는 COM을 유지하고, 미완료 예약/종료 미확인은 PPT 준비 대기다.
명시 UAC 거절 11만 영구 억제하고 일시 오류는 다음 실행에서 다시 점검한다.

Linux에서는 native ACL·파일 공유·UAC·Office를 실행하지 않았다. 하네스 작성이나 모형 회귀 통과는
이 Windows 승인 시험의 성공을 뜻하지 않는다. source/기본 disabled, candidate 시험,
released의 자동종료 OFF 정책을 유지하며 최종 released 파일의 실제 승인 후 동일 파일을 배포한다.

## 명시적인 최초 설치 후보 시험

다음 명령은 정상 앱의 자동 설치와 분리된 **개발 검증용** 경로다.
평소의 일반 권한 PowerShell에서 실행해야 최초 예약 생성과 다른 관리자 UAC를 검증할 수 있다.
추가 관리자 초기화/예약 폴더 수동 생성 없이 시험한다.

```powershell
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /prepare /prototype
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /check
```

예약 ACL을 만들 수 없으면 `acl_unavailable`로 중단하고 승격하지 않는다. 이를 관리자 모드
사전 초기화로 우회한 결과는 최초 자동 설치 성공으로 인정하지 않는다. 권한 요청을 승인한 경우도
실제 승격 자식의 종료와 일반 사용자 재점검을 확인한다. 실행 요청 성공만으로 설치 완료가 아니다.
disabled 도구에서 `/prepare`만 실행하면 `prototype_gate`로 거절한다. 사용자의 config나 manifest로
이 가드를 켤 수 없다. 이 수동 prototype 시험은 앱의 candidate 자동 설치 시험을 대신하지 않는다.

운영 DLL의 ProgID는 `AegisDesk.PptReader`이며 시험용 `AegisDesk.PptReadProbe`와 별개다.
PowerPoint에서 운영 추가 기능을 강제 활성화하거나 Office 충돌 차단 기록을 삭제하지 않는다.
`LoadBehavior`가 비활성인 운영 등록은 자동 설치/복구로 덮어쓰지 않는다.

## 제거와 중단 복구

### 공백이 있는 설치 경로에서 활성화 실패

이전 설치 도구는 `CodeBase`의 공백을 `%20`으로 기록했습니다. DLL 직접 로드는 성공하지만
COM 생성이 `0x80070002`로 실패하는 환경에서, 공백을 그대로 기록하면 생성에 성공하고
기존 값으로 복구하면 다시 실패하는 비교 시험으로 원인을 확인했습니다. 수정된 도구는
RegAsm과 같은 공백 표기를 사용하며 설치 점검도 같은 기준으로 검사합니다.

위 빌드 명령으로 수정된 도구를 만든 뒤 PowerPoint와 AegisDesk를 정상 종료합니다.
일반 권한 PowerShell에서 아래 명령을 실행하고 필요한 UAC를 승인합니다. 자기 소유 운영
등록만 제거하며 시험용 추가 기능과 Office 충돌 차단 기록은 변경하지 않습니다.

```powershell
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /remove
if ($LASTEXITCODE -ne 0) { throw 'ppt_reader_remove_failed' }
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /prepare /prototype
if ($LASTEXITCODE -ne 0) { throw 'ppt_reader_install_failed' }
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /check
```

`ready` 뒤에는 새 PowerPoint에서 운영 추가 기능의 체크 유지와 실제 읽기를 확인합니다.
인덱싱 성공 로그의 `method=addin`이 운영 DLL 사용을 뜻합니다. `ready`만으로 Office 읽기
성공이나 자동 설치 출하 검증이 완료된 것으로 간주하지 않습니다.

### 일반 제거와 중단 복구

PowerPoint와 AegisDesk를 정상 종료한 뒤 일반 권한 PowerShell에서 제거 도구를 실행한다.
필요한 Machine 수정만 UAC로 승격한다. 소유·비트수·고정 경로가 확인된 운영 등록만 해제하며,
비활성 운영 등록도 제거할 수 있다. 이전 버전 DLL 파일은 자동 삭제하지 않는다.

```powershell
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /remove
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /check
```

미완료 예약/저널은 시간 경과·helper 부재만으로 만료하지 않는다. 수동 복구가 필요한 경우
관리자 PowerShell에서 아래 명시 작업을 실행한다. UUID는 `%ProgramData%\AegisDesk\PptReaderRequests`
안의 해당 `.json` 파일명이다. 프로세스 조회 거부·PID 재사용·아직 살아 있는 helper/자식은
복구하지 않는다. 본문이나 문서 경로는 예약에 저장하지 않는다.

```powershell
# helper/자식 종료 identity를 확인하여 해당 예약을 완료 처리하고 자체 저널을 복구
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /recover '해당-요청-UUID'
# 미완료 예약이 없을 때 자체 등록 저널만 복구
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /recover journal
& .\.aegisdesk_build\manual\ppt_reader\PptReaderSetup.exe /check
```

복구 실패·일치하지 않는 설치 기록·HKCU shadow·다른 소유 등록을 정상으로 간주하지 않는다.
다른 추가 기능·시험 등록·Office Resiliency·UAC·Trust 정책은 변경하지 않는다.

AtomicWrite 도중 남은 `.tmp`도 안전 미확인으로 보류한다. `/recover UUID`는 유효한 요청 파일이
있어야 하므로 임의 임시 파일을 해소하는 명령으로 사용하지 않는다. 관리자는 설치 helper·승격 자식·
PowerPoint의 종료를 확인하고, 확인할 수 없으면 Windows 재시작 후 자체 요청 디렉터리의 해당 요청
임시 파일과 설치 루트의 `journal.json.*.tmp`/`installation.json.*.tmp`를 구분한다. 관리자가
해당 자체 잔류만 격리·정리한 뒤 명시 복구·`/check`를 실행한다. 유효한 요청·저널과 다른 예약을
일괄 삭제하지 않는다.

## 거절 기록과 재시도

설치는 앱 실행당 한 번만 시도한다. Office·mutex 점유 및 11 이외 실패는 영구 거절로 기억하지 않고
다음 실행에서 안전 상태를 다시 확인한다. 예약 생성 뒤 종료·정리가 미확인인 경우에는 관리자 복구가
필요할 수 있다. 오류가 일시적이라는 이유로 예약이나 저널을 자동 완료하지 않는다.

UAC를 명시적으로 거절한 경우에만 해당 reader 버전의 거절을
`%APPDATA%\AegisDesk\ppt_reader_preparation.json`에 기록한다. 이후 같은 버전의 권한 창을 반복하지 않는다.
기존 launch_failed/check_failed/mutation_failed 기록은 다음 준비 과정에서 정상화한다.
사용자가 거절을 취소하고 다시 자동 설치를 요청하려는 경우에만 AegisDesk를 종료한 뒤 이 준비 기록 파일을
명시적으로 지우고 candidate/released 앱을 다시 실행한다. 인덱스·설정·다른 상태 파일은 지우지 않는다.

## 최종 파일 승인과 배포

`build.bat clean released`로 만든 최종 폴더를 고정하고 아래 Windows 승인 시험을 그 실제 파일로
수행한다. exe·DLL·setup·manifest 해시, 커밋/dirty 지문·모드·버전·시험 환경·결과를 기록한 뒤
같은 파일을 배포한다. candidate 시험 뒤 released로 재빌드한 파일은 새 산출물이므로 다시 검증한다.
released라는 표시나 selftest 통과만으로 실제 설치 승인을 대신하지 않는다.

같은 reader 버전이라도 재빌드하면 DLL 해시가 달라질 수 있다. 도구는 같은 버전의 다른 DLL을
덮어쓰지 않고, `/remove`도 버전 디렉터리의 DLL을 남긴다. 후보 시험 후 최초 설치/최종 승인을 시험할 때는
새 시험 VM 또는 설치 전 스냅샷을 사용한다. 운영 DLL을 수동 삭제해 최초 설치를 흉내 내지 않는다.

## 자동 설치 출하 전 필요한 Windows 검증

- 일반 권한 최초 실행으로 예약 생성/ACL/UAC가 가능하고 다른 관리자 승인 후 원래 사용자가 예약을 갱신할 수 있는지.
- UAC 대기·설치 중 앱 종료/재시작, 다른 사용자/세션에서도 PPT 수집이 보류되는지.
- helper 강제 중단·자식 handle 미확인·PID 재사용·깨진 예약은 보류되며 수동 복구로 해소되는지.
- 다른 사용자 소유 mutex 접근, 포기된 mutex·중간 등록 실패·저널 rollback 실패가 fail-closed인지.
- Office 32/64비트 PE 점검, HKCU shadow·비활성 등록·PowerPoint 실행 경합에서 자동 수정이 없는지.
- 운영 DLL의 기존 문서 텍스트/청크 일치, 긴 임베딩 후 정상 커밋, 연속 인덱싱·앱 종료·Office 잔류.

Linux fake·C# 순수 하네스는 위 Windows/UAC/Office 시험을 대체하지 않는다.


## PowerPoint 자동종료 후보 검증

자동종료는 `build.bat clean candidate`로 만든 후보만 시험한다. 현재 기본·소스·released의
자동종료는 꺼져 있다. 자동 설치가 허용됐다는 뜻을 자동종료 승인으로 해석하지 않는다.
사용자 설정·환경변수로 자동종료를 켜지 않는다.

1. 별도 Windows 시험 PC/VM에서 후보를 설치하고 COM으로 읽는 소수 `.ppt`를 수집한다.
   직접 실행한 `/EMBEDDING` 프로세스와 실제 COM 호스트의 PID·생성 identity가 일치하는지 확인한다.
   실행 인계·다른 PID·미확인은 종료하지 않아야 한다.
2. 문서 완료와 기존 주기 재기동에서 같은 호스트에 종료 요청이 중복되지 않고 실제 PID가
   종료하는지 확인한다. 31개 이상 COM 문서와 PPT 없는 사이클도 각각 시험한다.
3. 사용자 사전 실행, 읽는 도중/종료 직전의 새 문서, 저장·미저장 문서, 보호된 보기 및 편집 전환,
   슬라이드 쇼에서 사용자 작업이 보존되는지 확인한다. 종료 중의 두 BeforeClose 이벤트가
   실제 COM 인수의 Cancel에 반영되는지 native ABI 시험을 수행한다.
4. Quit 도중 OnBeginShutdown/OnDisconnection 순서와 반환 후 지연 닫기 유무를 확인한다.
   안전한 보호 수명을 증명하지 못하면 기능을 배포하지 않고 설계를 다시 검토한다.
5. 추가 기능 비활성·호환성 불일치·호출 거절·RPC 단절·종료 미확인·앱 종료 경합·새 워커에서
   중복 실행 및 강제종료가 없는지 확인한다. 최초 경고 표시 설정에 따른 암호/복구/링크/매크로
   프롬프트와 모달 대기도 시험한다. 알림 억제나 자동 응답으로 통과시키지 않는다.

기존 추가 기능을 제거하거나 강제로 활성화해 이 조건을 우회하지 않는다. 시험용 문서 본문은
기록하지 않으며 빌드 해시·Office 비트수/버전·PID·종료 상태·시험 결과만 남긴다.
전체 fake 테스트 통과는 위 Windows·Office 시험 완료를 뜻하지 않는다. 정상종료의 배포 활성화는
이 시험 결과를 확인한 별도 변경이며 현재 released 빌드는 자동종료를 제공하지 않는다.
