# 운영 PPT Reader 개발 검증

다른 PC의 미완성 진단 소스 검토는 [checkpoint 이전 안내](PPT_CHECKPOINT_HANDOFF.md)를 먼저 따른다.
아래 후보 안내는 현재 PC의 설치 버전을 뜻하지 않으며 설치 실행 승인이 아니다.

현재 소스의 요구 버전은 Reader/Events/setup 모두 **1.0.5.11**이다. 선택적 Events DLL을 포함하므로
[오프라인 통합·복구 조건](../design/PPT_EVENT_ADAPTER.md)을 먼저 확인한다.
실제 설치·Office 검증 승인이 나기 전에는 아래 설치 절차를 실행하지 않는다.
승인 전에는 기존 설치본을 유지한다. 기본 빌드는 자동 설치를 끈 `disabled`이며,
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

## 구버전 부모 폴더 권한 복구 후보

1.0.0.0 Reader에서 `acl_unavailable / parent_policy / policy`로 업데이트가 막혔을 때의 후보다.
다른 설치 버전이나 임의로 수정된 폴더까지 수리하지 않는다. 기존 Reader 설치 기록과
`C:\ProgramData\AegisDesk\PptReaderRequests`가 남아 있고 부모 아래에 다른 항목이 없어야 한다.
구체적인 허용·거절 기준은 [설계 §8-7](../design/PPT_ADDIN_INDEXING.md#8-7-구버전-부모-폴더-권한-복구-후보)을 따른다.

PowerPoint와 AegisDesk를 정상 종료하고 새 candidate를 실행한다. 별도 config 옵션은 없으며,
조건을 통과하면 부모 권한 복구와 Reader 업데이트에 **최대 두 번의 UAC**가 나타날 수 있다.
준비 완료 로그의 설치 버전 `1.0.5.11`과 `status=ready`를 먼저 확인한다. 이후 새 구형 `.ppt`를
fake embedding으로 처리해 같은 PID의 `process observation ... status=exited`까지 확인한다.
이번 변경만으로 실제 사내 업데이트·PPT 종료가 통과했다고 판단하지 않는다.

| 단계 | 로그의 `stage` | 처리 |
|---|---|---|
| 기존 설치·부모·요청 검사 | `parent_repair_check` | 지원 버전/owner/ACL/기타 항목을 확인. 광범위 ACL 초기화 금지 |
| 감사 파일 생성·검사 | `parent_repair_backup` | 보호된 backup 및 부분 파일 상태 확인. 파일 임의 삭제 금지 |
| 부모 DACL 쓰기·재검사 | `parent_repair_write` / `parent_repair_verify` | 미완료 예약 유지. 앱 재실행만으로 재개하지 않음 |
| 관리자 명시 회수 | `parent_repair_recover` | 살아 있는 helper/child, identity 불명·지원하지 않는 상태는 보류 |

중단 후 `reservation_unresolved`라면 관리자 명시 회수가 필요하다. 앱과 해당 설치 helper가
정상 종료했는지 먼저 확인한다. 미완료 UUID는 요청 파일의 `request_id`와 `state`에서 확인하며,
본문·SID·SDDL을 앱 로그에 복사하지 않는다. 검증한 candidate payload의 `PptReaderSetup.exe`에
관리자 권한으로 `/recover-parent UUID`를 전달한다. 이 명령은 부모를 안전한 방향으로 좁히고
예약을 완료할 뿐 Reader를 업데이트하지 않는다. 성공 후 앱을 다시 실행해 일반 업데이트를 진행한다.

backup이 없고 부모가 이미 protected explicit이면 원본과 중간 저장을 구분할 수 없어 회수도
거절한다. 이때는 요청·backup·부모 ACL을 보존해 진단하며, 폴더 삭제나 예약 complete 수동 편집으로
우회하지 않는다. 이 PC에 보관된 과거 시험용 동일 버전 DLL을 새 빌드로 덮어써 설치하지 않는다.

개발 회귀는 `pytest knowmate/tests/test_ppt_parent_repair.py -v`다. 일반 pytest는 실제 UAC를
요청하지 않으며 lifecycle fixture를 컴파일하고 네이티브 비전파·특수 lease를 검사한다. 수동 승격
lifecycle 검증 산출물과 OS fake 범위는 [진단 결과](../DIAGNOSTICS.md#ppt-구버전-부모-폴더-권한-복구-2026-10-25)에 있다.

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

## PPT 요청 저장과 충돌 진단

이번 수정은 **준비 요청 파일만 native 저장으로 바꾼 1.0.4.0 후보**다. 일반 권한 Windows·로컬
NTFS에서 AegisDesk와 PowerPoint를 정상 종료한 뒤 아래 명령 한 번으로 비교 시험, 실제 제품 저장
경로, 필수 lease·stdout 수명 시험을 모은다.
앱 재빌드는 필요 없으며 시험 C# 실행 파일을 자동 컴파일한다. AegisDesk의 기존 AppData·ProgramData와
설치는 변경하지 않고 UAC도 요청하지 않는다. Reader/setup 요구 버전은 계속 1.0.4.0이다.

```powershell
powershell -NoProfile -File .\scripts\diagnose_ppt_rename.ps1
```

기본 `.venv` 대신 다른 Python을 쓸 때는 `-PythonPath 'C:\경로\python.exe'`를 붙인다.
마지막 `Diagnostic log:`의 파일 하나에 상세 결과를 남기고, 파일 앞뒤에는 같은 짧은 요약을 둔다.
파일 전달이 어려우면 **`PPT_SHORT_REPORT`부터 `PPT_SHORT_END`까지**만 먼저 전달한다.
실패해도 나머지 경우를 계속 실행한다. writer의 각 프로세스 제한은 30초이며, 미확인 native 상태와
저장 중단 시험은 시험 전용 프로세스의 제한 종료를 의도하므로 전체 실행에 몇 분 걸릴 수 있다.
stdout 시험은 시작·정상 종료 관찰에 각각 30초, 실패 정리에 추가 최대 5초를 쓴다.
실패 정리에서 종료하는 대상은 이 시험이 직접 시작한 Python 자식뿐이며, 그 정리가 성공해도
본시험 실패는 유지한다. 실제 설치 helper는 종료하지 않는다.

| 요약 | 판독 |
|---|---|
| `PPT_RENAME_SUMMARY` | 기존 managed 저장과 저수준 native 대조군. 기존 managed 실패가 남을 수 있으며 제품 통과로 대체하지 않는다. |
| `PPT_WRITER_SUMMARY` | 실제 `Reservation.Create/Load/Save`와 `RequestWriter`의 생성·교체·충돌·거절·오류·중단 검증. `fail/timeout/skip/unexecuted`를 함께 확인한다. |
| `PPT_ACL_SUMMARY` | `result=PASS`면 기존 필수 폴더·lease 검증이 실제 제품 요청 저장 경로로 통과했다는 뜻이다. |
| `PPT_STDOUT_SUMMARY` | `result=PASS`면 관찰 해제 뒤 실제 자식의 정상 종료·마지막 handle 삭제를 확인했다는 뜻이다. |
| `PPT_CLEANUP_SUMMARY` | 생성 ID가 없거나 다른 폴더·root junction의 정리를 거절하고, 외부 대상에 영향을 주지 않는 정리 안전성 시험이다. |
| `PPT_REQUIRED_EXTERNAL` | 다른 사용자·UAC·Office는 이 명령에서 미실행이며 별도 검증해야 한다. |

링크 fixture를 만들지 못하면 원래 Win32 숫자 코드와 `UNEXECUTED`를 남기며 전체 필수 검증
완료로 인정하지 않는다. fixture 생성 후 제품의 거절·보존·정리 검증이 실패하면 `FAIL`이다. 일반 사용자로 가능한 directory junction 거절도 별도 시험한다. 이 표시는 제품 저장 실패와
구분하며, 정상 앱 실행을 위해 Developer Mode나 관리자 실행을 요구한다는 뜻은 아니다.
기존 managed 실패는 비교 수치로 남기며 필수 제품 검증의 통과·실패와 구분한다. native 대조군의
실패는 해당 pytest를 실패로 남긴다. `product_approval=false`는 이 명령만으로 출시 승인하지
않는다는 고정 표시다.

요약의 `result`와 `required_gate`는 해당 실행의 pytest 최종 결과를 기준으로 한다. 성공 문구를 출력한 뒤 정리에서 오류가 나면
FAIL이며, skip은 UNEXECUTED, 결과 누락·깨짐·모순은 INCOMPLETE다. 원래 실패 단계와 정리 실패는
따로 남긴다. `PPT_WRITER_PROBLEM/PPT_CLEANUP_PROBLEM`은 관련 단계와 오류 값만 짧게 표시한다.
본문 통과 뒤 정리 실패가 나면 최종 `result=FAIL`이며 `body_result=PASS`는 앞선 본문 결과다.
stdout 지연은 `ready/release_seen/stdout_before/stdout_after/done/before_return`의
마지막 단계와 실제 종료 확인 여부·종료 코드로 좁힌다. `done` 파일만으로 종료를 판정하지 않는다.
중간 결과 파일은 자동 처리하므로 전달할 자료는 기존과 같이 상세 txt 하나 또는 짧은 블록이다.

실제 제품 저장만 따로 재실행하려면 다음을 쓴다.

```powershell
& .\.venv\Scripts\python.exe -m pytest .\knowmate\tests\test_ppt_request_writer.py -s -v -o addopts=
```

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
먼저 `atomic_control`에서 lease 없는 managed 대조군을 확인한 뒤 `harness`에서
과거 상속 부모와 두 lease를 보유하고 실제 제품 요청 저장을 실행한다. `source_open/source_acl/source_write/
source_flush/commit_call/commit_return/commit_verify/source_close`로 위치를 좁힌다.
`stage_started_ms`는 해당 단계 진입 시점이며, 계측은 시험 실행 파일에만 들어간다.
일반 setup의 partial 계측·오류 주입 호출은 컴파일 때 제거된다.
stdout 파일 열기 거절은 Win32 오류뿐 아니라 CRT의 `errno=EACCES/EPERM`도 인정한다.
파일 없음·일반 I/O 오류는 거절 성공으로 인정하지 않으며 ACL·상속·실제 종료·삭제 검사는 계속 수행한다.
관찰 해제는 설치 도구의 실제 종료로 기록하지 않는다. 닫은 handle을 `Popen` 소멸자가 다시 조회하지 않도록
명시 해제 여부를 유지하며 실제 종료 판정은 열려 있는 관찰 handle로만 수행한다.

이 하네스는 실제 `Directory.CreateDirectory`로 상속 부모를 재현하고 보호 자식을 생성한다.
누락 폴더의 원자 생성과 두 lease 동시 열기, anchor/부모/자식 rename·자식 삭제 거절, 예약 파일 원자 생성·교체,
기존 owner/DACL 불변과 기존 DELETE handle 충돌을 검사한다. 같은 프로세스의 두 handle 시험은
실제 helper·승격 자식의 다른 계정 권한 시험을 대신하지 않는다.

그다음 candidate EXE로 기존 1.0.0.0 → 1.0.4.0 업데이트와 새 설치를 각각 확인한다.
필수 기록은 `required_version=1.0.4.0`, 실제 `installed_version`, 일반 helper/승격 자식 종료,
원래 사용자 `/check`의 `ready`, 인덱싱의 `method=addin`이다. 같은 계정 및 다른 관리자 UAC,
앱만 종료했을 때 helper/자식 생존·stdout 쓰기/마지막 handle 정리, 점유/거절/공유 충돌,
중간 helper 종료·자식 생존·`/recover`와 32/64비트 Office는 별도 실제 시험한다.

사용자 간 시험은 A=디렉터리 owner, B=예약 owner, C=비소유 일반 사용자로 구분한다.
C의 B 예약 갱신·삭제는 거절되고 B의 자기 예약과 Admin/System 작업은 가능해야 한다.
A의 암묵적 WRITE_DAC에 의한 DACL 변경은 lease로 막지 못한다. owner까지 적대적으로 격리해야 하면
이 후보를 활성화하지 말고 관리자 소유 저장 구조 등으로 재설계한다.

실패 로그는 고정 요약 code와 검증된 `reason/stage/error_type/winerror`를 표시한다.
요청의 native 호출 후 실패에는 `native_stage/ntstatus/io_status`도 함께 남긴다.
`ntstatus/io_status`는 8자리 대문자 16진수이며 미수집 IO 상태는 null이다.
`commit_return`은 호출 실패, `commit_verify`는 저장 후 확인 실패, `commit_close`는 핸들 정리 실패다.
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


### PowerPoint 종료 3방식 비교 진단

`--ppt-quit-compare`는 설치된 candidate Reader를 그대로 사용해 외부 Quit, 내부 Quit,
참조 해제만 수행하는 시험을 각각 새 PowerPoint·호출 프로세스에서 실행한다. 일반 앱의
자동종료 정책을 바꾸거나 DLL을 설치·등록하지 않는다. Python·컴파일러·PIA 추가 설치는 필요 없다.

**업무용 세션에서 실행하지 않는다.** 폐기 가능한 시험 VDI/VM 세션에서 다른 앱과 AegisDesk를
완전히 종료하고 PowerPoint가 없는지 확인한다. 실행 중에는 사용자와 다른 프로그램이 Office를
열지 않아야 한다. `--confirm-isolated-session`은 이 전제의 명시적 확인이다. 문서 수 검사와 PID
일치는 외부 Quit 직전 사용자 문서가 열리는 경쟁 상황의 안전성을 보장하지 않는다.

먼저 `build.bat clean candidate`로 빌드하고 candidate 앱을 한 번 실행해 Reader 자동 설치가
완료됐는지 확인한 뒤 앱을 완전히 종료한다. 빌드 PC에서 만든 `dist\candidate\AegisDesk` 폴더
전체를 시험 VDI로 옮긴다. 이번 기능이 포함된 새 EXE가 필요하며 Reader를 수동 재설치하지 않는다.

PowerShell에서 저장소 루트를 기준으로 실행한다. VDI에서는 `$exe`를 옮긴 EXE 경로로 바꾼다.

```powershell
$exe = (Resolve-Path '.\dist\candidate\AegisDesk\AegisDesk.exe').Path
$out = Join-Path $env:TEMP ('AegisPptQuit-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$p = Start-Process -FilePath $exe -ArgumentList @(
  '--ppt-quit-compare', '--live', '--confirm-isolated-session',
  '--output', ('"{0}"' -f $out), '--timeout', '30'
) -PassThru
$p.WaitForExit()
Get-Content (Join-Path $out 'summary.log')
```

개발 소스에서는 같은 옵션으로 `python -m knowmate.secure.ppt_quit_compare`를 실행한다.
`Start-Process -Wait`는 잔류한 자식 PowerPoint까지 기다릴 수 있으므로 위처럼 부모의
`WaitForExit()`만 사용한다.
32비트 Office는 `--office-bitness 32`를 추가한다(기본 64). 느린 환경에서는 `--timeout 120`처럼
시험별 제한 시간을 5~300초 범위에서 지정할 수 있다.
도구가 생성한 합성 PPT만 열고 Reader 추출 결과를 고정 해시와 비교한다. 본문·창 제목·임의 예외
문자열은 기록하지 않는다. 종료 요청과 반환, 참조 정리, 자기 COM 초기화 해제, 호출 프로세스 종료,
원 PowerPoint 프로세스의 실제 종료를 구분한다. 실제 종료는 부모가 보관한 원 프로세스 핸들로 확인한다.
호출 프로그램의 COM 준비 후 그 안에서 PowerPoint를 실행하고 바로 연결한다. 부모 감시자는
원 프로세스 핸들의 관측 권한만 복제받으며 PID를 다시 열거나 다른 PPT를 종료 대상으로 삼지 않는다.
실행 직후 프로세스가 끝나거나 핸들 인계가 확인되지 않으면 종료 비교를 진행하지 않는다.
창 핸들 속성 미지원 환경에서는 빈 호스트를 확인한 뒤 제품과 같은 임시 빈 문서·캡션 표식으로
프로세스를 검증한다. 확인용 창이 잠깐 보일 수 있으며 확인 문서·원래 캡션·자동화 보안값은
기존 제품 경로로 복구한다. 이 검증이나 복구가 실패하면 합성 파일 읽기·Quit을 진행하지 않는다.
연결 실패는 `failures`의 `bind_step`, `error_type`, `cause_error_type`, 숫자 코드만 전달해도 된다.

어느 시험에서든 PowerPoint·호출 프로세스가 남거나 상태가 불명확하면 다음 시험은
**UNEXECUTED**로 남긴다. 남은 프로세스를 강제 종료하거나 동일 프로세스로 후속 시험하지 않는다.
제한 시간이 지나 부모가 끝나도 호출 프로세스는 COM 호출 중일 수 있다. 요약의 호출 PID까지
완전히 종료됐는지 확인할 때까지 세션을 격리한 채 유지한다. 확인이 어려우면 시험 VDI를 폐기한다.
참조 해제만 하는 시험은 PowerPoint가 남는 것도 유효한 관측이므로 마지막에 실행한다.
남은 시험은 합성 시험 프로세스임을 확인해 수동으로 닫은 뒤 새 출력 폴더와
`--arm internal_quit`, `--arm reference_drop` 또는 `--arm external_quit`로 별도 실행한다.

결과를 공유할 때는 우선 `$out\summary.log` 하나를 전달한다. 실제 종료가 확인돼도 해당 조건의
관측일 뿐, 사용자 문서 보호·일반 배포 승인·모든 Office 환경의 종료 성공 증거가 아니다.
전체 pytest와 실제 Windows x86/x64·Office·frozen EXE·DRM 검증 상태는
[DIAGNOSTICS.md](../DIAGNOSTICS.md)를 따른다.

### 외부 종료 bootstrap 진단 빌드

`build.ps1 -ExternalDiagnostic`는 명시적 시험 전용 옵션이며 기본값은 비활성이다. 이 옵션은
기존 deferred 진단과 외부 bootstrap IDispatch를 함께 컴파일한다. 기본 빌드의 dual IID와
정상 종료 경로는 유지한다. `AEGIS_PPT_EXTERNAL_DIAGNOSTIC=1`만으로 기본 빌드를 활성화할 수 없다.
진단 빌드도 nonce/PID 생성 시각/MAINSTA/창 소유 스레드, native capability 및 COM identity
검증을 거쳐야 한다. 프로세스 환경 fence는 협력적 재시도 차단이며 보안 경계가 아니다.
오류 기록은 최근 16개 작업까지만 보존하고, mutable fault hook은 시험 define에서만 존재한다.
시험은 합성 host와 실제 COM/native handle을 사용하며 Office 설치·DRM·다른 계정·UAC 검증을
대체하지 않는다. cleanup 오류 주입은 실제 OS CloseHandle 실패를 검증한 것이 아니다.
