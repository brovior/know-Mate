# PPT 이벤트 어댑터 통합 후보

현재 소스의 Reader·Events·setup 버전은 1.0.5.0이다. 오프라인 통합 후보이며 설치된
1.0.4.0을 교체하지 않는다. 실제 Office·회사 DRM·다른 계정 UAC·배포 승인은 별도다.

## 코어와 종료 기능

Reader의 CLSID, 기본 lifecycle IID, 기존 reader IID와 Office 연결 인터페이스는 유지한다.
코어는 PIA와 Events 어셈블리를 정적으로 참조하지 않는다. Events DLL만 설치된 PIA 15의
전체 EApplication 33개 메서드를 구현한다. Microsoft PIA를 번들에 재배포하거나 설치하지 않는다.

종료 요청 시 호스트 ITypeInfo의 전체 순서·슬롯·인자 방향·참조 형식·취소 포인터를 확인한다.
그 뒤 고정된 Program Files의 버전 디렉터리에서 설치 기록, ACL, 링크 부재, DLL 해시와
어셈블리 이름·버전을 확인한다. 두 DLL의 검증된 디스크 바이트를 독립된 reflection-only
AppDomain에서 읽어 MVID를 얻고, 실제 로드된 core·Events의 MVID와 비교한다. 이는 디스크
해시와 메타데이터의 상관 확인이며 로드된 메모리 바이트의 해시 검증이 아니다. 버전별 고유
디렉터리와 Office 정상 종료 후 업데이트 조건을 유지한다. PIA는 정확한 강력한 이름과 GAC 로딩을 요구한다.
조건이 충족되어야 Events를 생성하고 연결한다. 사용자 경로 탐색·다운로드·전역 바인딩 변경은 없다.

공유 상태는 COM에 노출되지 않는 Active/Cancelled/Broken뿐이다. 닫기 콜백은 취소 값을
먼저 설정하고 내부 실패를 Broken에 누적한다. 연결 중·각 호스트 호출 후·Quit 직전에
Broken을 확인한다. Quit 반환 직후 Broken이면 Cancelled 관측을 보존하되 결과는
`ppt_quit_outcome_unknown`이며 이후 호스트 호출과 Unadvise를 하지 않는다. 다음 요청은
`not_supported`이다. finally에서 Active를 끄므로 연결 해제 실패 후 보관된 sink도 수동 닫기를
방해하지 않는다. 기존 fatal transport 후 추가 RPC 금지와 한 번만 Quit 시도하는 계약을 유지한다.
의존성이 없거나 잘못되면 종료 기능은 not_supported가 되며 코어의 읽기 계약은 유지된다.

## 패키지와 설치 기록

manifest의 외곽 schema 1, protocol 3과 추출 JSON schema 1은 유지하고 payload_schema는 2로 한다.
허용 payload는 Reader.dll, Reader.Events.dll, PptReaderSetup.exe 세 파일뿐이다.
설치 대상은 두 DLL뿐이며 COM 등록은 Reader에만 수행한다. 두 파일 모두 해시·버전·어셈블리
이름을 검사한다. 새 설치 기록 schema 2는 두 DLL의 files 해시 맵과 기존 core sha256을 갖는다.
1.0.4.0 이하의 기존 schema 1 기록만 호환 대상으로 읽는다. 새 버전의 부분 기록은 거부한다.

같은 버전 파일을 덮어쓰지 않는다. 저널은 기존 등록과 설치 기록 전체를 보존한다. 중간 실패는
해당 등록·기록을 복구하며 이미 복사된 버전별 파일은 남긴다. 정상 제거도 자신의 등록·기록만
제거하고 버전별 DLL을 유지한다. 와일드카드 복사·삭제나 ACL 완화는 도입하지 않는다.

보호된 정규 설치 기록의 버전이 helper보다 높으면 등록 누락·부분 등록을 수리 가능으로
분류하기 전에 거부한다. Apply도 Recover와 저널 생성 전에 같은 검사를 한다. 새 저널은
schema 2와 target_version을 기록한다. 복구 전에 기록과 등록 스냅샷 전체를 검증하며,
높은 버전·불일치·판단 불가능한 이전 schema 1 저널은 `journal_unresolved`로 남긴다.
기존에 설치된 1.0.4 helper의 동작을 소급 변경하지 않는다. 구버전으로 복구할 때는 현재
버전 helper로 정상 제거한 후 검증된 원본 구버전 패키지를 설치하며, 오래된 helper의
다운그레이드 거부 기능에 의존하지 않는다.

## 오프라인 확인 범위

`scripts/ppt_abi_prototype/run_product.py`는 실제 제품 DLL에 x86/x64 원시 COM 호출을 하고
합성 호스트로 loader·validator·guard를 연결한다. 일반 pytest의 관리형 guard 시험은
PIA 없는 합성 sink를 사용하고, 실제 PIA sink의 ABI는 이 별도 실행기가 검증한다.
후자는 설치 경로/ACL 경계 안쪽의 내부 loader 진입점을 사용하므로 실제 보호된 설치의
성공으로 해석하지 않는다. 설치 트랜잭션 시험의 레지스트리·파일·ACL·환경은 메모리 모형이다.
`ProductSetupFixtures`는 실제 Machine.Check·Apply·Recover를 컴파일한다. PayloadValid만
이 시험에서 모형이며 별도 payload 시험이 실제 구현을 검사한다. 먼저 로드된 동일 이름·버전의
다른 core/Events 빌드는 실제 DiskMvid·RequireLoaded와 guard로 거부를 확인한다. 다른 경로의
unsigned LoadFrom은 이 PC에서 별개 어셈블리를 로드하므로 이 시험을 전체 loader의 캐시
재현이나 실행 중인 DLL 교체 검증으로 해석하지 않는다.

## 순방향 복구 계획

설치된 1.0.4.0 DLL과 installation.json의 바이트·해시·ACL 스냅샷을 보존한다. 원본 helper와
manifest를 포함한 완전한 설치 패키지가 없어 현재는 같은 바이트로 정상 재설치할 수 없다.
재컴파일 DLL의 MVID·타임스탬프 차이를 고치거나 manifest를 조작하여 이를 대신하지 않는다.

후속 복구 후보는 **1.0.6.0**의 새 정식 빌드로 준비한다. 알려진 1.0.4.0의 추출 코드와
종료 거부 동작을 복원하고, 현재의 안전한 설치 기록 호환·명시적 파일 검증을 유지한다.
새 복구 빌드에서는 자동 종료를 비활성화하고, 실제 사용하지 않는 이벤트 연결 경로가
실행되지 않음을 시험해야 한다. 버전·정책·실제 파일과 일치하는 build_guard/manifest를
생성하고 frozen payload 검사까지 통과시켜 불변 아티팩트로 보관한 뒤에만 설치를 검토한다.

이 복구 패키지는 아직 제작·설치하지 않았다. 이를 구현할 때 일반 빌드의 candidate 정책과
복구 전용 종료 비활성화 정책을 명시적으로 분리하고, 정책 불일치 거부 시험을 추가해야 한다.
업데이트/제거는 정상 setup의 사전 조건을 그대로 따른다. disabled·unsafe 등록은 덮어쓰지
않으며 실제 UAC와 설치 복구 검증 전에는 1.0.5.0 설치도 계속 보류한다.

## External diagnostic trace transport (offline review)

`AEGIS_PPT_EXTERNAL_DIAGNOSTIC=1`, `AEGIS_PPT_LIFECYCLE_TRACE=1` and a nonempty
N-format `AEGIS_PPT_TRACE_RUN` opt in. The explicit directory still passes the
existing held-ancestor, local NTFS, no-reparse writer checks. No profile fallback.

The host records 128 primitive entries (stage, outcome, three integers, QPC ticks).
The first refusal is latched separately before cleanup. Identity comparison is
separate from acquisition/release success. Guard entered, reading and deferred
service depth are separate concepts; no entry gate is relaxed. Full-buffer loss is
counted, so absent stages are only `not_reached` in a complete untruncated frame.

Bind, Ready and ACK never consume an export. At a terminal request/poll boundary,
a captured snapshot is queued to a local-only worker. Reentrant lifecycle exports
are deferred until the surrounding request unwinds. Lifecycle body cleanup may
queue a final snapshot; the worker receives no Office object and performs no COM
call, message pump, or cleanup retry. Unknown publishes an incomplete snapshot;
subsequent lifecycle cleanup can publish one final snapshot. At most two immutable
`host-PID-external-0.log` / `host-PID-external-1.log` files use CREATE_NEW. The ordinary
`host-PID.log` writer is independent. No callback performs file I/O.
The independent best-effort export may overlap the CLR callback's return. Its
contract is no callback-thread file I/O, no Office/COM access, no owner wait and no
guard-state mutation; it does not promise execution after callback return and
does not add an owner dispatch.

Each frame includes run ID, PID/creation, QPC frequency, snapshot number, loss count,
first refusal and an END record. A writer exception only updates telemetry failure
counts; it never enters PptCleanupErrors or changes cancellation, Broken, HRESULT,
or the original return value. Missing files are not success: the reader reports
unrecovered, partial or mismatched evidence without another host RPC. Process loss
can prevent the queued worker from completing; that limitation is explicit.
Frames are cumulative and selected by snapshot slot, not file arrival time. A
valid final snapshot 1 with zero drops is sufficient when snapshot 0 is absent.
If no valid final frame is recovered, or entries were dropped, absent stages are
unknown rather than evidence that a stage was not reached.

`ppt_trace_report.py` reads local frames only. The native synthetic controller logs
its existing Bind/Prepare/START/ACK/Quit/Completion and OS-exit observations with the
same run ID/PID creation and normalized QPC timestamps. A real controller must set
these environment fields before launch and use the same envelope; enabling host
telemetry alone does not establish an independent OS observation.

Coverage entry points: `test_ppt_external_diagnostic.py` (transition, native COM,
terminal transport and invariant fixtures), `test_ppt_trace_report.py` (evidence
loss), `test_ppt_lifecycle_diagnostics.py` (shared writer). Fixture processes are
Office-free. Real Office exit, company DRM and actual deployment remain untested.
