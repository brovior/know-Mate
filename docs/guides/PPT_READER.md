# 운영 PPT Reader 개발 검증

현재 운영 리더·설치 도구의 구현 후보다. **자동 설치 출하 가드는 꺼져 있다.**
시험 DLL의 내용 일치/속도 결과를 운영 DLL·자동 설치의 성공으로 간주하지 않는다.
아래 명령은 Windows 개발 PC에서 실행한다. 문서 원문은 로그/파일로 기록하지 않는다.

## 빌드와 읽기 전용 점검

PowerPoint를 정상 종료하고 저장소 폴더의 PowerShell에서 실행한다.

```powershell
& .\.venv\Scripts\python.exe .\scripts\build_guard.py --output-dir .\.aegisdesk_build\ppt_reader
if ($LASTEXITCODE -ne 0) { throw 'build_guard_failed' }
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\ppt_reader\build.ps1 -BuildInfoDir .\.aegisdesk_build\ppt_reader
if ($LASTEXITCODE -ne 0) { throw 'ppt_reader_build_failed' }
& .\scripts\ppt_reader\bin\PptReaderSetup.exe /check
```

`/check`는 등록을 변경하거나 PowerPoint를 실행하지 않는다. `ready`는 요구 버전의
파일·Machine 등록 확인이며 실제 PowerPoint 내부의 로드·읽기 성공을 뜻하지 않는다.
`machine_safe=false`이면 PPT COM 수집을 미룬다. `auto_install_validated=false`는 현재 정상이다.

## 명시적인 최초 설치 후보 시험

다음 명령은 정상 앱의 자동 설치와 분리된 **개발 검증용** 경로다.
평소의 일반 권한 PowerShell에서 실행해야 최초 예약 생성과 다른 관리자 UAC를 검증할 수 있다.
추가 관리자 초기화/예약 폴더 수동 생성 없이 시험한다.

```powershell
& .\scripts\ppt_reader\bin\PptReaderSetup.exe /prepare /prototype
& .\scripts\ppt_reader\bin\PptReaderSetup.exe /check
```

예약 ACL을 만들 수 없으면 `acl_unavailable`로 중단하고 승격하지 않는다. 이를 관리자 모드
사전 초기화로 우회한 결과는 최초 자동 설치 성공으로 인정하지 않는다. 권한 요청을 승인한 경우도
실제 승격 자식의 종료와 일반 사용자 재점검을 확인한다. 실행 요청 성공만으로 설치 완료가 아니다.
`/prepare`만 실행하면 출하 가드 때문에 `prototype_gate`로 거절한다. 사용자의 config나 manifest로
이 가드를 켤 수 없다.

운영 DLL의 ProgID는 `AegisDesk.PptReader`이며 시험용 `AegisDesk.PptReadProbe`와 별개다.
PowerPoint에서 운영 추가 기능을 강제 활성화하거나 Office 충돌 차단 기록을 삭제하지 않는다.
`LoadBehavior`가 비활성인 운영 등록은 자동 설치/복구로 덮어쓰지 않는다.

## 제거와 중단 복구

PowerPoint와 AegisDesk를 정상 종료한 뒤 일반 권한 PowerShell에서 제거 도구를 실행한다.
필요한 Machine 수정만 UAC로 승격한다. 소유·비트수·고정 경로가 확인된 운영 등록만 해제하며,
비활성 운영 등록도 제거할 수 있다. 이전 버전 DLL 파일은 자동 삭제하지 않는다.

```powershell
& .\scripts\ppt_reader\bin\PptReaderSetup.exe /remove
& .\scripts\ppt_reader\bin\PptReaderSetup.exe /check
```

미완료 예약/저널은 시간 경과·helper 부재만으로 만료하지 않는다. 수동 복구가 필요한 경우
관리자 PowerShell에서 아래 명시 작업을 실행한다. UUID는 `%ProgramData%\AegisDesk\PptReaderRequests`
안의 해당 `.json` 파일명이다. 프로세스 조회 거부·PID 재사용·아직 살아 있는 helper/자식은
복구하지 않는다. 본문이나 문서 경로는 예약에 저장하지 않는다.

```powershell
# helper/자식 종료 identity를 확인하여 해당 예약을 완료 처리하고 자체 저널을 복구
& .\scripts\ppt_reader\bin\PptReaderSetup.exe /recover '해당-요청-UUID'
# 미완료 예약이 없을 때 자체 등록 저널만 복구
& .\scripts\ppt_reader\bin\PptReaderSetup.exe /recover journal
& .\scripts\ppt_reader\bin\PptReaderSetup.exe /check
```

복구 실패·일치하지 않는 설치 기록·HKCU shadow·다른 소유 등록을 정상으로 간주하지 않는다.
다른 추가 기능·시험 등록·Office Resiliency·UAC·Trust 정책은 변경하지 않는다.

## 자동 설치 출하 전 필요한 Windows 검증

- 일반 권한 최초 실행으로 예약 생성/ACL/UAC가 가능하고 다른 관리자 승인 후 원래 사용자가 예약을 갱신할 수 있는지.
- UAC 대기·설치 중 앱 종료/재시작, 다른 사용자/세션에서도 PPT 수집이 보류되는지.
- helper 강제 중단·자식 handle 미확인·PID 재사용·깨진 예약은 보류되며 수동 복구로 해소되는지.
- 다른 사용자 소유 mutex 접근, 포기된 mutex·중간 등록 실패·저널 rollback 실패가 fail-closed인지.
- Office 32/64비트 PE 점검, HKCU shadow·비활성 등록·PowerPoint 실행 경합에서 자동 수정이 없는지.
- 운영 DLL의 기존 문서 텍스트/청크 일치, 긴 임베딩 후 정상 커밋, 연속 인덱싱·앱 종료·Office 잔류.

Linux fake·C# 순수 하네스는 위 Windows/UAC/Office 시험을 대체하지 않는다.
