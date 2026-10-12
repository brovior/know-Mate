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
