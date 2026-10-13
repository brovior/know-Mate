# PowerPoint 추출 방식 사전 검증

`scripts/diag_ppt_read.py`로 기존 COM 읽기와 두 후보를 비교한다. 운영 수집기·설정·DB는
변경하지 않는다. Office 2016·인터넷 폐쇄망이 대상이며 Office.js는 사용하지 않는다.

## 검증 범위

| 방법 | 동작 | 실행 조건 |
|---|---|---|
| conversion | 합성 `.ppt`를 `SaveCopyAs`로 `.pptx` 사본 저장 후 기존 Python 파서로 읽기 | 합성 파일만 허용. 실제 문서 변환 저장 옵션 없음 |
| addin | PowerPoint 내부 C# COM 추가 기능이 셀·그룹을 순회하고 슬라이드별 JSON 반환 | 사내 허용 후 시험용 추가 기능 등록·로드 |
| plain | 이미 존재하는 `.pptx`를 Python으로 직접 읽기 | Python이 읽을 수 있는 PPTX. 새 사본 저장 없음 |

실제 문서의 평문 사본 저장은 `CLAUDE.md §2-1`과 충돌한다. 합성 변환 성공은 사내 DRM
변환 허용·읽기 가능성을 증명하지 않는다. DRM 저장 정책을 바꾸거나 매크로 차단을 낮추지 않는다.

본문·셀·JSON 응답은 메모리에서만 비교한다. 출력은 환경·경로·건수·시간·일치 여부·오류 유형만
포함하며 본문·해시·벡터·클립보드를 사용하지 않는다. 변환 출력과 합성 입력은 임시 디렉터리에서
만들고 종료 시 삭제한다. 실문서는 읽기 전용으로 열고 크기·수정 시각의 변화도 확인한다.

## 실행

저장소 루트에서 Windows cmd로 실행한다. 의존성은 기존 `requirements.txt` 환경의
`python-pptx`·`pywin32`이며 실행 중 다운로드하지 않는다.

Office 없이 비교 계약만 확인:

```cmd
.venv\Scripts\python.exe scripts\diag_ppt_read.py --self-test
```

회귀 검증도 Office 없이 실행한다. C# 순회 검증은 Windows의 .NET Framework 컴파일러가 필요하며
추가 기능을 등록하지 않는다. 완료한 검사와 사내 미검증 범위는
[진단 문서](../DIAGNOSTICS.md#사전-검증-도구-확인-결과-2026-10-11)에 기록한다.

```cmd
.venv\Scripts\python.exe -m pytest knowmate\tests\test_ppt_probe.py -q
```

PowerPoint를 사용자가 모두 정상 종료한 뒤 합성 변환 비교:

```cmd
.venv\Scripts\python.exe scripts\diag_ppt_read.py --synthetic --method conversion --repeat 3
```

합성 파일은 기본 3슬라이드·표당 100행×10열과 별도 빈 슬라이드로 만든다. 병합 셀·빈 행·셀 내
줄바꿈·중첩 그룹을 포함한다. `--rows`·`--columns`·`--slides`로 크기를 바꿀 수 있다.
합성 PPTX를 구형 PPT로 만드는 준비 시간은 비교에서 제외하고, 후보의 PPTX 변환 저장 시간은 포함한다.

이미 존재하는 일반 PPTX의 COM/직접 읽기 비교:

```cmd
.venv\Scripts\python.exe scripts\diag_ppt_read.py --file "C:\검증\문서.pptx" --method plain
```

등록된 추가 기능의 합성 또는 실제 문서 비교:

```cmd
.venv\Scripts\python.exe scripts\diag_ppt_read.py --synthetic --method addin
.venv\Scripts\python.exe scripts\diag_ppt_read.py --file "C:\검증\문서.pptx" --method addin
```

`--synthetic --method all`은 두 후보를 비교한다. `--file --method all`에서는 변환을 차단하고
내부 읽기만 시도하며 종료 코드 2로 미검증 범위를 표시한다.

## 시험용 추가 기능 준비

`scripts/ppt_addin/PptReadProbe.cs`는 VSTO 런타임을 추가하지 않는 최소 C# COM 시제품이다.
Office 주 STA에서 순회하도록 `StandardOleMarshalObject`를 사용하고 호출 스레드도 확인한다.
PowerPoint가 로드한 앱과 입력 프레젠테이션의 COM identity가 같아야 읽는다. 파일 저장·열기·Quit을
추가 기능에서 수행하지 않는다. Python은 `COMAddIns.Item(...).Object`로 이미 로드된 객체만 호출한다.

빌드는 설치와 분리돼 있다. .NET Framework 4.x와 C# 컴파일러를 사용하며 Office PIA·NuGet·인터넷이
필요하지 않다. 대상 PC에는 .NET Framework 4.x가 있어야 한다. 사내 규정상 등록이 허용되지 않으면
addin 결과는 미검증으로 남긴다. 시제품은 서명되지 않았으며 배포용 설치 프로그램이 아니다.

PowerShell 스크립트 실행이 허용된 환경에서 컴파일만 실행:

```cmd
powershell -NoProfile -File scripts\ppt_addin\manage.ps1 -Action Build
```

실행 정책 때문에 스크립트를 실행할 수 없으면 정책을 변경하지 않고 컴파일러로 빌드할 수 있다:

```cmd
mkdir scripts\ppt_addin\bin
"%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe" /nologo /target:library /platform:anycpu /optimize+ /out:scripts\ppt_addin\bin\PptReadProbe.dll /reference:Microsoft.CSharp.dll /reference:System.Core.dll /reference:System.Web.Extensions.dll scripts\ppt_addin\PptReadProbe.cs
```

사내 허용·스크립트 실행 조건이 충족된 뒤 사용자가 등록한다. 아래는 **Office 32비트** 예시이며
Office 64비트이면 `-OfficeBitness 64`로 바꾼다. Python 비트수와 구분한다.

```cmd
powershell -NoProfile -File scripts\ppt_addin\manage.ps1 -Action Install -OfficeBitness 32
```

기본 `-Scope User`는 현재 사용자의 시험용 CLSID·ProgID·PowerPoint Addins 키만 등록한다. 매크로·서명·신뢰 정책을
변경하지 않는다. Office에서 등록을 거부하거나 로드하지 못하면 도구는 unavailable로 보고하며
Connect를 강제로 켜지 않는다. 등록된 DLL 경로를 옮기지 않는다.

시험 후 PowerPoint를 정상 종료하고 같은 비트수로 등록 해제:

```cmd
powershell -NoProfile -File scripts\ppt_addin\manage.ps1 -Action Remove -OfficeBitness 32
```

### 컴퓨터 단위 등록 시험

관리자 권한으로 실행하면서 UAC가 비활성인 환경에서는 COM이 사용자 등록을 무시하고
컴퓨터 등록만 사용할 수 있다([Microsoft 설명](https://learn.microsoft.com/en-us/windows/win32/sysinfo/merged-view-of-hkey-classes-root)).
`-Scope Machine`은 관리자 권한으로 지정한 Office 비트수의 HKLM 영역에 이 시험용 추가 기능만
등록한다. 기본 User와 Machine은 서로 독립적이며, Remove도 지정한 범위만 해제한다.
UAC·서명·신뢰 정책을 변경하거나 관리자 권한을 자동 요청하지 않는다.

PowerPoint를 모두 정상 종료하고 저장소 루트의 **관리자 권한 CMD**에서 실행한다.
아래는 Office 64비트이며, 32비트 Office에서는 모든 명령의 OfficeBitness를 32로 바꾼다.

```cmd
rem 두 범위의 같은 ProgID가 시험 결과에 영향을 주지 않도록 기존 사용자 등록을 먼저 해제
powershell -NoProfile -File scripts\ppt_addin\manage.ps1 -Action Remove -OfficeBitness 64 -Scope User
rem 등록할 범위·키를 미리 확인(레지스트리 변경 없음)
powershell -NoProfile -File scripts\ppt_addin\manage.ps1 -Action Install -OfficeBitness 64 -Scope Machine -WhatIf
powershell -NoProfile -File scripts\ppt_addin\manage.ps1 -Action Install -OfficeBitness 64 -Scope Machine
```

DLL은 기존 Build 결과를 사용하며 실행 중 다운로드하지 않는다. Install은 기본 CLR 정보와
실제 어셈블리 버전별 하위키를 함께 등록한다. 버전 하위키의 존재만으로 로드 성공을 판정하지 않는다.

등록 후 새 64비트 Windows PowerShell에서 COM 생성만 먼저 시험한다.

```powershell
try {
    $probeType = [Type]::GetTypeFromProgID('AegisDesk.PptReadProbe', $true)
    $null = [Activator]::CreateInstance($probeType)
    'COM 객체 생성 성공'
} catch {
    $_.Exception.GetBaseException().Message
}
```

COM 생성에 성공하면 PowerPoint의 COM 추가 기능에서 `Aegis Desk PPT read probe`의 연결을
확인한다. 체크가 유지되지 않으면 표시된 로드 오류를 확인하며 재시험 성공으로 취급하지 않는다.
PowerPoint를 정상 종료한 뒤 기존 Python 환경에서 최소 합성 시험을 실행한다.

```cmd
.venv\Scripts\python.exe scripts\diag_ppt_read.py --synthetic --method addin --repeat 1
```

시험 후 PowerPoint를 정상 종료하고 **설치 때와 같은 Machine 범위·Office 비트수**로 해제한다.
DLL 파일을 옮기거나 삭제하기 전에 등록을 해제한다.

```cmd
powershell -NoProfile -File scripts\ppt_addin\manage.ps1 -Action Remove -OfficeBitness 64 -Scope Machine
```

이 절차는 시험용이다. 다른 PC의 권한·보안정책·Office/DRM 호환성이나 일괄 배포 가능성을
증명하지 않는다. 실제 문서 본문이나 추출 결과를 파일로 저장하지 않는다.

### 로드 중 충돌한 뒤 수정본 재시험

COM 객체 생성과 Office의 연결 콜백은 별도 단계다. 객체 생성이 성공해도 추가 기능을
연결할 때 PowerPoint가 종료되면 로드 성공으로 취급하지 않는다. PowerPoint를 정상 종료하고
설치 때와 같은 Scope·OfficeBitness로 Remove한다. 보안정책이나 Office의 다른 추가 기능은 변경하지 않는다.

연결 규약 수정본은 DLL 재빌드가 필요하다. COM 객체 생성에 사용했던 PowerShell도 종료해
이전 DLL 참조를 해제한다. 아래는 저장소 루트의 관리자 CMD·Office 64비트 예시다.
먼저 수정된 C# 소스와 테스트를 내려받고 기존 시험용 등록을 해제한 상태에서 진행한다.

```cmd
powershell -NoProfile -File scripts\ppt_addin\manage.ps1 -Action Build
powershell -NoProfile -Command "[Reflection.AssemblyName]::GetAssemblyName((Resolve-Path 'scripts\ppt_addin\bin\PptReadProbe.dll').Path).Version.ToString()"
.venv\Scripts\python.exe -m pytest knowmate\tests\test_ppt_probe.py -v
```

DLL 버전은 `0.1.2.0`이어야 한다. 다른 버전이면 재등록하지 않고 빌드 결과를 확인한다.
Windows .NET Framework 컴파일러가 있으면 두 C# 하네스도 실행된다. 연결 하네스는 실제
COM callable wrapper의 Dual vtable을 통해 연결·해제·시작·종료·실패 콜백을 호출한다.
Office를 실행하거나 등록하지 않으며 내부 순회 하네스와 별개다. Linux에서 이 항목이
skip된 결과는 네이티브 COM 검증 통과를 의미하지 않는다. 테스트가 실패하면 재등록하지 않는다.

테스트를 통과한 뒤 컴퓨터 영역에 다시 등록한다.

연결 하네스의 `probe_missing_host`는 호스트가 없는 입력을 거부하는 의도적 실패 시험에 사용된다.
정상 콜백의 배열 왕복은 각 콜백 직후 확인하며 실패 시험에서는 정상 출력 배열을 요구하지 않는다.
배열 검사 실패는 `after_connect_array_null`, `after_shutdown_get_hr_0x…`처럼 단계와 코드로 구분한다.
검사 실패를 Office 로드 성공으로 해석하지 않는다. 검증 하네스만 수정된 경우 DLL 재빌드는
필요 없지만 갱신된 테스트로 다시 통과해야 재등록한다.

```cmd
powershell -NoProfile -File scripts\ppt_addin\manage.ps1 -Action Install -OfficeBitness 64 -Scope Machine
```

PowerPoint가 앞선 충돌로 시험용 추가 기능을 비활성화했을 수 있다. 사용자 등록·컴퓨터 등록을
반복해서 바꾸지 말고, PowerPoint의 추가 기능 화면에서 이 시험용 항목만 확인한다. Office가
비활성 항목의 복구를 제공하면 수정 DLL의 테스트 통과 후 해당 항목만 복구한다. 체크를 연결할 때
다시 종료되면 반복 활성화를 중단하고 같은 범위로 Remove한다. Office의 충돌 차단 기록을
레지스트리에서 일괄 삭제하거나 Trust 정책을 우회하지 않는다.

PowerPoint의 정상 실행·연결·종료를 먼저 확인하고 위 합성 시험을 repeat 1로 실행한다.
성공한 뒤 repeat 3으로 늘린다. 수정은 COM 연결 규약 불일치를 해소한 것이며 실제 Office/DRM
환경의 모든 충돌이 해결됐다는 뜻은 아니다. 시험 후 같은 범위로 Remove한다.

### 내부 읽기 오류 판독

진단 DLL `0.1.2.0`은 재빌드·재등록해야 한다. PowerPoint와 DLL을 생성한 COM 시험용
PowerShell을 모두 종료한 뒤 빌드한다. `CS0016` 파일 사용 중 오류가 나면 잔류 프로세스가
DLL을 잡고 있는지 확인하며 빌드 실패 상태에서는 등록하지 않는다.

실패 결과의 `probe_stage`, `probe_error_kind`, `probe_inner_hresult`로 호출 위치·오류 종류·코드를
확인한다. `application_get`은 문서의 Application 접근, `host_identity`·`document_identity`는
각 객체의 COM identity 취득이다. identity가 다르면 `probe_wrong_application`을 그대로
표시하며 확인을 우회하지 않는다. 연결 해제·잘못된 스레드·STA 불일치도 별도 표식으로 표시한다.

그 뒤 슬라이드·도형·그룹·표·셀·텍스트·직렬화 단계를 구분한다. 계속 처리한 읽기 오류는
`[ppt-probe] 내부 읽기 오류`에 슬라이드·그룹 내 도형 위치·행·열과 코드만 기록한다.
상세는 슬라이드당 최대 16건이며 나머지는 미표시 건수로 남긴다. 오류가 있는 비교는 성공으로
판정하지 않는다. 임의 COM 오류 문구·문서 본문은 출력하지 않는다.

이 정보는 반환된 예외의 진단이며 COM 호출이 멈췄거나 프로세스가 종료돼 예외가 반환되지 않으면
실패 지점을 확정할 수 없다. 외부 진행 로그와 Office 충돌 이벤트를 함께 확인해야 한다.

도구와 등록 스크립트는 PowerPoint를 Quit·강제 종료하지 않는다. 사용자 PowerPoint가 실행 중이면
기존 소유권 가드로 읽기를 연기한다. COM 호출 자체가 멈추면 즉시 취소가 보장되지 않는다.
숨김 문서·DRM 추가 기능 공존·실제 Office 스레드 전달·잔류 프로세스는 사내 확인 대상이다.
RPC 연결 단절 또는 문서 닫기 미확인은 후속 실험을 중단한다. 그 외 후보 실패는 본문 없는 오류를
남기고 다음 후보를 비교한다.

## 판독

- `equal=true`: 슬라이드별 문자열 목록의 순서와 도형·그룹·표·셀 건수가 일치하고 양쪽 읽기 오류가 0.
  현재 리더의 표 공백·줄바꿈 정렬을 적용한 결과를 비교한다. `mismatch_slides`는 불일치 위치만 표시한다.
- `seconds.total`: 열기·읽기·닫기를 포함한다. conversion은 `save`와 `parse`도 포함한다.
  addin의 `internal_read`는 내부 순회 시간이며 별도 진단값이므로 total에 다시 더하지 않는다.
- `speedup`: 같은 반복의 기존 COM 전체 시간 / 후보 전체 시간. 반복마다 실행 순서를 바꾼다.
  첫 반복·캐시 준비도 중앙값에 포함하며 Office 시작(dispatch)은 별도로 표시한다.
- 종료 코드 `0`: 요청한 비교가 완료되고 일치. `1`: 오류·내용/건수 불일치·원본 변화.
  `2`: 추가 기능 미로드 또는 실제 문서 변환 차단으로 요청한 비교가 미완료.

자체 검사와 합성 결과는 사내 성능 검증이 아니다. 사내 실측에서는 Office 제품·정확한 빌드·비트수,
DRM 환경과 메모리 추적 설정을 함께 기록한다. `office_version=16.0`만으로 Office 2016 제품을
확정하지 않는다. 속도·내용 보존·종료 안전성을 확인한 뒤 운영 적용 여부를 결정한다.
