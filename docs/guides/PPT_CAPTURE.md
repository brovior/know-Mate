# PPT 종료 진단 묶음

새 진단 도구는 **구현 후보이며 live 실행은 잠겨 있다**. Windows x86/x64 native 시험과 구체적인
패키지의 Office 합성 검토가 끝나기 전에는 Office를 시작하지 않는다. 설치·등록·기존 checkpoint121의
잠금 해제나 일반 배포 자동종료 활성화를 포함하지 않는다.

이 도구는 한 실행에서 `internal` 또는 `external` 한 경로만 선택하고 Quit을 최대 한 번 요청한다.
내부 실패 뒤 외부 Quit으로 이어서 시험하지 않는다. 검토한 범위는
[r2 계획](../design/PPT_SHUTDOWN_CAPTURE_PLAN.md), 과거 실측은 [진단](../DIAGNOSTICS.md)에 있다.

## 현재 사용할 수 있는 기능

저장소 루트에서 기존 **새 도구 schema3 묶음**을 다시 판독한다. 원본을 덮어쓰지 않으며 새 폴더가 필요하다.

```powershell
python -m scripts.diagnostics.ppt_capture --replay C:\capture\run001 --output C:\capture\run001-reviewed
```

성공은 exit code 0, 증거 유실·실행 차단은 2다. 기존 checkpoint113/119/121의 schema2 trace는
기존 `scripts/ppt_reader/ppt_trace_report.py`로 판독한다. schema2 frame 완결만으로 뒤의 lifecycle
미도달이나 PPT 종료를 판정하지 않는다.

새 합성 입력은 설치된 `python-pptx`를 사용해 Office 없이 만든다. 기존 폴더·사용자 문서는 재사용하지 않는다.

```powershell
python -m scripts.diagnostics.ppt_capture --make-fixture --output C:\capture\new-fixture
```

고정 title/body 한 슬라이드의 `capture-<새 UUID>.pptx`와 `fixture.json`을 만든다. 실제 읽기 결과는
메모리에서 길이·SHA256만 비교한다. provenance 해시, 생성기 식별, 고정 XML 내용, 슬라이드 수 및
외부 관계 부재를 검증하므로 임의 업무 파일을 fixture pin으로 지정하는 경로로 사용하지 않는다.

## 진단 전용 패키지 빌드

Windows의 기존 `.venv`, .NET Framework C# compiler와 Office PIA 15.0이 필요하다.
새 candidate BuildInfo를 [기존 빌드 절차](ENVIRONMENT.md)에 따라 생성하고 다음 명령을 사용한다.
이 명령은 컴파일·reflection-only 검사만 수행한다.

```powershell
scripts\ppt_reader\build.ps1 -BuildInfoDir <새-candidate-BuildInfo-폴더> -CaptureDiagnostic
```

출력은 `<BuildInfoDir>\ppt_capture`이며 기존 폴더를 재사용하지 않는다. core/events/setup/collector는
진단 전용 `1.0.6.0`이다. 일반 제품 소스의 `1.0.5.0` 및 reader protocol3·공개 COM 메서드 의미는 유지한다.
버전 치환은 출력의 generated C# 사본에만 적용한다.

| 파일 | 역할 |
|---|---|
| `manifest.json` | 기존 helper가 읽는 schema1, core/events/setup 세 파일의 버전·해시 |
| `capture-manifest.json` | schema3/layout1, 네 바이너리·정확한 런타임 소스 집합·생성 사본 해시 |
| `PptCaptureCollector.exe` | Office/COM 없이 shared buffer를 디스크에 보존하는 별도 프로세스 |

빌드는 native 결과를 성공으로 만들지 않는다. 새 manifest의 x86/x64는 `UNEXECUTED`,
`live_enabled`와 `default_enabled`는 false다. 파일 해시·source pin·fixture pin·native/Office 검토가
서로 다른 패키지를 가리키면 새 도구는 시작 전에 거절한다.

후속 Windows 검토자는 다음 run pin을 준비한다. 아래 파일을 만드는 것만으로 live 잠금이 풀리지는 않는다.

| pin 필드 | 내용 |
|---|---|
| `version`, `package_manifest_sha256`, `helper_sha256` | `1.0.6.0`, 새 capture-manifest와 실제 setup helper의 SHA256 |
| `installed_directory` | 검토한 새 reader/events 두 DLL이 설치된 디렉터리. 실제 package SHA256과 일치해야 함 |
| `fixture_path`, `fixture_sha256`, `fixture_provenance_sha256` | 방금 만든 PPTX와 fixture.json의 위치·해시 |
| `fixture_text_length`, `fixture_text_sha256` | fixture.json의 text_length/text_sha256 |
| `office_bitness`, `office_exe_sha256` | 실제 Office 32/64와 신뢰한 POWERPNT.EXE의 해시 |
| `native_gate_path`, `native_gate_sha256` | 아래 구조의 실제 Windows native 검증 자료와 해시 |
| `office_approval_path`, `office_approval_sha256` | 구체적 패키지·선택 경로에 대한 별도 승인 자료와 해시 |

native gate JSON은 `package_manifest_sha256`과 `x86: "PASS"`, `x64: "PASS"`를 담는다. Office 승인
JSON은 같은 package 해시와 `approved: true`, `path: "internal"` 또는 `"external"`을 담는다.
이는 증빙을 고정하는 입력 형식이며, 임의로 PASS를 써서 승인 절차를 대신하는 파일이 아니다.
source review lock은 여전히 false이며 CLI·환경변수·JSON 옵션으로 우회할 수 없다.

새 adapter는 pin이 맞는 setup helper의 읽기 전용 `/check`가 Machine 등록·버전·bitness를 fresh하게
확인한 뒤 진행한다. helper 미반환은 10초 뒤 차단하며 강제 종료하지 않는다. 고정 Aegis 등록 트리의
해시를 보관하고, caller에서 같은 MachineReadGuard를 얻은 뒤 다시 비교한다. 설치·등록을 수정하지 않는다.

## Office 없는 Windows native 확인

```powershell
python -m pytest knowmate/tests/test_ppt_capture_buffer_native.py -v
```

이 시험은 x86/x64에 대해 실제 익명 mapping과 owner STA를 사용한다. 잘못된 layout/run/PID/생성시각/
thread/type 및 재사용 header 거절, release/acquire publication, nested callback depth, off-thread·append
재진입·포화 유실, Connect 선생성 뒤 Bind에서의 단회 채택과 generation 보존을 확인한다.
Office를 시작하거나 설치하지 않는다. Linux skip과 compiler 부재는 `UNEXECUTED`다.

native harness 통과만으로 live 잠금을 해제하지 않는다. collector 급종료·최종 drain·디스크 실패,
대상 외 PID 종료·WCT 미반환 및 실제 event ABI/사용자 문서 보호를 포함한 나머지 필수 gate는
[계획 검증표](../design/PPT_SHUTDOWN_CAPTURE_PLAN.md#9-검증과-실패-시-행동)를 따른다.
현재 이 저장소의 live 진입은 코드에서 잠겨 있고 CLI 옵션으로 해제할 수 없다.

## 결과를 읽는 순서

`summary.json`에서 다음 값을 따로 읽는다.

| 값 | 의미 |
|---|---|
| `request_complete` | controller 요청이 반환했는가. PPT 종료 증거가 아님 |
| `capture_closed` | 관측을 끝낸 이유·시각. deadline은 PPT 종료가 아님 |
| `os_exit` | 원래 retained target handle의 signal. 다른 PID 종료와 구분 |
| `evidence_loss` | slot 누락·counter·binding·collector/tail 오류 등으로 증거가 부족한가 |
| `post_cleanup_observed_ms` | wrapper drop·CoUninitialize 이후 실제 retained-handle 관측 범위. 미확인이면 null |

호출 횟수는 실제 확보한 기록의 하한이다. `quit_calls`는 host Quit 진입과 controller의 실제 Quit
Invoke 진입을 합친 값이며 각 위치별 횟수도 별도로 기록한다. 미관측 stage는 `unknown`이다.
선택 controller getter는 `omitted_by_plan`으로 남기며 필수 canonical/self-control·기존 host 네 행·
정리·파일 저장 gate가 실패하면 요청하지 않는다.

host capture는 **Bind에서 mapping을 채택한 때부터**다. 그 전 lifecycle은 unknown이며
header의 `capture_start_ticks`, `pre_attach_records`, `capture_scope`로 범위를 표시한다.
callback에서는 고정 primitive slot만 쓰고 파일 I/O·COM 추가 조회·수집기 응답 대기·강제 GC를 하지 않는다.
요청 전에 확인한 수집 실패는 admission에서 거절한다. 이미 시작한 같은 요청 중 생긴 누락은
기존 guard 동작을 바꾸지 않고 `evidence_loss`와 사후 `unknown`으로 남긴다.

결과 폴더에는 `summary.json`, `manifest.json`, `controller.jsonl`, `host.jsonl`, `os.jsonl`,
`coverage.json`, `hashes.json`만 만든다. 자유 예외 설명·문서/메일 본문·창 제목·dump를 저장하지 않는다.
취소·timeout에도 강제 종료·WM_CLOSE·대화상자 자동응답을 하지 않는다. 남은 target/caller와 마지막
관측 상태를 기록하고, 다음 실행은 기존 프로세스가 정리된 새 실행이어야 한다.

collector/caller는 별도 private 임시 spool에 primitive만 쓰고 supervisor가 완성 행을 결과 폴더에
복사·fsync한다. pipe EOF나 child exit를 무한 대기하지 않는다. closure 뒤 늦은 쓰기는 private spool에만
남으므로 해시를 만든 결과 묶음은 바뀌지 않는다. child가 남으면 private spool도 남았음을 요약에 표시한다.
native에서는 검증한 조상 디렉터리 lease를 유지해 결과 경로의 rename/reparse 교체를 차단한다.

`os.jsonl`에는 실제 Python/pywin32/Windows 버전·bitness, Office EXE 버전/해시·bitness, 세션·일반권한,
fresh 등록 결과와 고정 이름의 Win32 loaded-module 버전을 담는다. PowerPoint/Addins의 HKCU/HKLM·32/64
목록은 제한된 ProgID·LoadBehavior만 읽으며 Description/FriendlyName/등록 경로는 제외한다.
CLR은 실제 `clr.dll` module 버전으로 관측하며 managed assembly 전체 목록은 unknown이다.
창 제목·lock object 이름은 기록하지 않는다. WCT는 OUT_OF_PROC 숫자 결과만 별도 2초 worker로 관측하며,
권한 거절·미반환·비어 있는 결과를 원인 해소로 해석하지 않는다.

현재 native child/수집기·directory lease·registry/Office 연결·실제 module/WCT API는 Windows에서 미실행이다.
fake 회귀는 실제 파일 spool/freeze·deadline·타 PID·누락/외부 trace·정리 장애를 검증하며 Windows 성공을 대체하지 않는다.
