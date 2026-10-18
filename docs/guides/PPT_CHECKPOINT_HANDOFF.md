# PPT 진단 checkpoint를 다른 PC에서 이어가기

이 체크포인트는 미완성 제품·실험 코드를 보존한다. 자동 종료 문제의 해결본이나 배포 승인본이 아니다.
[분석 정본](../DIAGNOSTICS.md#ppt-종료identity-관측-검토-2026-10-18)에 기록한
controller 선택 getter 선행 결함은 **수정하지 않았다**. 새 설치·실제 Office 시험은 이 이전 작업에 포함하지 않는다.

## 보존 범위와 소스 차이

- `knowmate/secure/`: reader bundle/설치 identity·종료 guard 연동·진단 상태 기록.
- `scripts/ppt_reader/`, `scripts/ppt_events/`: 분리된 Events DLL, 보호 loader, native ownership,
  raw Count, bounded trace, deferred/external 진단·identity observation. 진단 인터페이스는 기본 빌드에서 제외된다.
- `knowmate/tests/`, `scripts/ppt_abi_prototype/`: fake/native ABI·metadata·writer·guard 검증과 fixture 소스.
  host 계약 JSON은 공개 Office 인터페이스 계약 값이며 문서 내용·PC 식별자가 아니다.
- `scripts/diagnostics/ppt_checkpoint121/`: 과거 live controller/support/OS observer/pin 검증 소스,
  실제 handshake fake 시험, 정확한 순서 결함 재현, 합성 PPTX 생성기와 이식성 검사.
  `run.py`는 import·COM·파일 기록 전에 오류를 내도록 잠갔다. `--live`로도 실행되지 않는다.
  이 잠금은 일회성 이전본의 안전장치이며 연구자가 임의로 해제해 실행하라는 지시가 아니다.

과거 candidate121은 commit `67c23cd6a9e851a0aeffd2ac7394579c03165353` + 미커밋 source fingerprint
`4d71bac5`를 별도 복사하여 `1.0.5.0`을 `1.0.21.0`으로 바꾸고 external diagnostic을 켠 패키지다.
recovery122는 별도 shutdown-disabled 변형이며 새 PC에 설치하지 않는다.
현재 보존 소스의 기준 버전은 `1.0.5.0`이다. 이번 추가 문서·이식 도구·커밋으로 fingerprint가 달라진다.
과거 fingerprint나 DLL 해시를 새 빌드 메타데이터로 복사하면 안 된다. 새 빌드는 실제 commit·dirty·해시로 식별한다.
이 브랜치에는 과거 121 패키지의 byte-identical 재생성 자동화나 복구122 생성기를 제공하지 않는다.

## 준비 조건

Windows 일반 사용자, Git, Python(이번 검증은 3.14.5, 다른 버전은 미확인),
저장소 `requirements.txt`가 필요하다. 기존 Python/패키지 버전을 추측해 같다고 기록하지 않는다.
Native 시험·빌드는 .NET Framework `csc.exe`, Windows SDK가 아닌 Framework CLR,
PowerPoint/office PIA 15 GAC 등록이 필요하다. 실제 Office 설치와 bitness는 별도 확인한다.
PIA DLL·Office 설치 파일을 저장소에 복사하지 않는다. 권한·의존성 부족은 FAIL 또는 UNEXECUTED로 보고한다.
로컬 NTFS의 새 임시 경로를 사용하고 symlink/reparse 시험에 필요한 권한이 없으면 승격하지 않는다.

아래 명령은 저장소 루트의 PowerShell 기준이다. 패키지 설치는 새 PC 사용자가 별도로 수행한다.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pytest --version
```

## Office 없이 시작할 검증

기존 사용자 설정·로그·문서 폴더를 사용하지 않는다. 아래 시험은 fake를 주입하며 Office를 활성화하지 않는다.
`test_controller.py`는 정확한 과거 handshake 코드에 fake service를 전달하므로 파일명과 달리 live 시험이 아니다.

```powershell
$trialTemp = Join-Path $env:TEMP ('aegis-ppt-handoff-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m pytest scripts/diagnostics/ppt_checkpoint121 knowmate/tests/test_ppt_identity_observation.py knowmate/tests/test_ppt_identity_controller.py knowmate/tests/test_ppt_identity_live.py -q --basetemp=$trialTemp
```

Windows native 기본 비활성 게이트도 Office 없이 확인할 수 있다(PIA·csc 필요).
전체 회귀는 `pytest knowmate/tests -v`이며 이 문서의 focused 통과로 대체하지 않는다.

```powershell
.\.venv\Scripts\python.exe -m pytest knowmate/tests/test_ppt_external_diagnostic.py::test_external_default_off -q
```

기존 전체 결과는 1973 PASS / 1 FAIL / 5 SKIP, writer 60 PASS / 0 FAIL / 2 UNEXECUTED(1314)였다.
이번 이전용 focused 검증은 **54 PASS**(fake/이식 52개 + x86/x64 default-off 2개)였다.
새 PC에서 별도로 재측정해야 한다. synthetic host/count/connection-point/sink는 실제 Office RCW·종료를 증명하지 않는다.

## 합성 fixture 재생성

```powershell
.\.venv\Scripts\python.exe scripts/diagnostics/ppt_checkpoint121/make_fixture.py --output-dir .aegisdesk_build/new-synthetic-fixture
```

이 명령은 고정된 합성 제목·본문만 가진 PPTX와 해시 JSON을 만들며 Office를 시작하지 않는다.
기존 출력 폴더가 있으면 거절한다. 문서 데이터나 이전 PC의 바이너리를 포함하지 않는다.
과거 시험은 legacy `.ppt`를 사용했다. 위 PPTX의 재생성 해시를 과거 `.ppt` 해시와 같다고 기대하면 안 된다.
legacy 변환은 별도 승인 아래 사용자가 PowerPoint에서 이 합성 파일만 97-2003 형식으로 저장한 뒤,
새 파일 해시·읽기 출력 계약을 다시 확인해야 한다. 그 전에는 legacy 재현은 미실행이다.
PPTX 생성과 ZIP 내부 XML 확인은 실제 add-in 추출 검증을 대체하지 않는다.
업무 문서를 변환하거나 저장하는 경로는 제공하지 않는다.

## 기본 비활성 빌드

기존 설치본을 덮어쓰거나 COM 등록하지 않는 컴파일 단계다. 새 출력 경로를 사용한다.
현재 버전 1.0.5.0으로 컴파일하므로 이 결과를 121 설치본에 적용하면 안 된다.

```powershell
$buildRoot = Join-Path (Get-Location) ('.aegisdesk_build/handoff-build-' + [guid]::NewGuid().ToString('N'))
$buildInfo = Join-Path $buildRoot 'metadata'
.\.venv\Scripts\python.exe scripts/build_guard.py --output-dir $buildInfo --ppt-auto-install-mode disabled
powershell -NoProfile -File scripts/ppt_reader/build.ps1 -BuildInfoDir $buildInfo
$env:AEGIS_BUILD_INFO_DIR = $buildInfo
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath "$buildRoot/dist" --workpath "$buildRoot/work" AegisDesk.spec
Remove-Item Env:AEGIS_BUILD_INFO_DIR
```

`-ExternalDiagnostic`/`-DeferredDiagnostic`는 명시적 진단 빌드 옵션이다. 후보 metadata의 `candidate` mode는
자동 설치와 shutdown 정책에 영향을 주므로 기본 build의 `disabled`를 편의상 바꾸지 않는다.
실제 재시험용 버전·복구·패키지 hash·설치 검토가 먼저 필요하다. 위 명령은 helper `prepare`나 EXE 실행을 포함하지 않는다.
이 이전 작업에서는 컴파일·패키징 명령 자체를 재실행하지 않았다.

## Live 관측을 다시 설계할 때의 조건

먼저 필수 controller identity/wire → 기존 host observation → 즉시 primitive export 순서의 변경안을
검토하고 offline에서 첫 unknown 이후 후속 host/Prepare 0회를 증명한다. 기존 `complete_host`, 동일 identity,
cleanup/ownership, generation, export 성공, one-shot Prepare/START/ACK/Quit 조건을 완화하지 않는다.
현재 알려진 getter 오류는 그대로이므로 보존된 runner를 성공한 종료 실험으로 취급하지 않는다.

새 live 실행은 사용자 승인, AegisDesk/PowerPoint 부재, 일반 사용자, synthetic 입력, 새로 직접 생성한
정확한 PID/생성시각/HWND 소유권·독립 OS handle 증명과 새 run 디렉터리가 필요하다.
unknown/fatal/refusal 이후 getter·status·Prepare·Quit 재시도 금지, 강제 종료 금지, 사용자 문서·프로세스 보호를 유지한다.
승인된 정상 정리도 별도 증거로 기록하며 Quit 성공으로 세지 않는다. 설치 UAC·다른 계정·사내 DRM·배포 검증은 독립 gate다.

이전 PC의 `package-pins.json`, `source-manifest.json`, `controller-manifest.json`은 경로·세션 바인딩이 있어 게시하지 않았다.
잠금 해제 검토 시 새 source/package/helper/fixture의 해시와 경로를 신뢰된 빌드 결과로 다시 바인딩해야 한다.
`verify_pins.py`의 검사를 건너뛰거나 과거 pin을 임의 수정해 통과시키면 안 된다. pin 생성 자동화는 미제공이며 live 준비는 미완료다.
과거 원본 스크립트 해시는 `historical-source.json`에 있으며 이식본과 차이를 확인할 수 있다.

원시 로그·설치 바이너리·개인 설정·실제 PID/HWND 등은 Git/PR에 포함하지 않는다.
이전 PC의 `.aegisdesk_build/` 증거는 로컬에만 보존된다. 새 PC에서는 상대 경로 목록만으로 원 로그를 읽을 수 없다.
