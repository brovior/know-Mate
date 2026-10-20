# PPT 진단 수집 구현 — 작성자와 분리한 직접 리뷰

상태: **소스 구현 및 작성자와 분리한 직접 리뷰 완료, Windows gate 대기**.
검토자 `/root`, 개발 담당 `ppt_capture_implementation` 및 `ppt_capture_runner_implementation`.
사용자의 구현 요청에 따라 r2 계획을 구현하며, 기존 계획 승인과 이번 코드 리뷰를 구별한다.

## 기준과 환경

- 기준 HEAD는 `b75c98511cea3ddb7d842c7204dd11b4f9767ea3`다.
- 검증 도구는 pytest 8.3.5/pluggy 1.5.0/iniconfig 2.1.0/PyYAML 6.0.2를 사용했다.
- baseline identity 33 PASS, 관련 PPT 115 PASS / 11 SKIP. baseline 전체 회귀는 PyQt6/pyarrow/lancedb 미설치로 collection 오류 8건이다.
- 현재 환경은 Linux/Python 3.12이며 Windows·Office·mcs/mono/csc/dotnet이 없다. 실제 native/COM 실행과 새 C# 컴파일을 통과로 표시하지 않는다.

## 직접 발견한 항목

| ID | 심각도 | 재현·조건 | 개발자 반영 및 후속 확인 |
|---|---|---|---|
| I-01 | High | 다른 run의 request_return만으로 request_complete=True가 됨. 직접 호출로 재현 | 유효 binding·관측 범위 행만 집계하도록 수정. foreign request_not_run에도 동일 조건 적용. 최종 회귀 통과 |
| I-02 | Medium | OS 관측이 없어도 ref drop=9ms/uninit=9.5ms/capture end=10ms로 post_cleanup_observed_ms=0.5를 보고함. 직접 재현 | 실제 retained-handle 관측 구간에 제한하고 미관측은 unavailable. 최종 회귀 통과 |
| I-03 | Medium | external Quit은 controller에서 실행되는데 host Quit 행만 세어 0회로 집계 | external quit_begin과 host/controller별 관측 횟수를 분리. Invoke 직전 소유 검사 거절이면 0회, 실제 진입이면 1회인 추가 회귀 통과 |
| I-04 | High | add-in 생성 시 readonly mapping adopt가 parent의 PID/owner binding보다 먼저 실행되면 영구 null | Bind Start에서 owner STA가 one-shot lazy adopt하도록 수정. pre-attach 범위는 unknown. Windows 실제 순서는 native gate 미실행 |
| I-05 | Medium | OnConnection generation 갱신이 lazy attach 이전에 발생하면 이후 record generation이 0으로 남음 | 최신 generation을 trace에 보관하고 attach 시 주입하도록 수정. native gate 미실행 |
| I-06 | High | 수집 실패 후 supervisor가 다음 tick에서 새 COM 단계를 진행할 수 있음 | irreversible admission latch를 추가. OS 관측은 계속하되 새로운 COM 허용을 되살리지 않음 |
| I-07 | High | 다른 PID/run의 target_state 또는 request_begin이 종료·deadline 결정에 섞일 수 있음 | backend의 검증된 binding과 시간 범위를 만족하는 행만 supervisor 결정에 사용 |
| I-08 | Medium | stop/close 예외가 최종 summary 생성을 건너뜀 | 정리 실패를 증거 유실로 기록하고 가능한 결과 묶음을 계속 생성 |
| I-09 | Medium | bool을 seq=1로 받아들이거나 잘못된 숫자형·추가 필드·중복 JSON key·부분 tail을 허용 | 고정 필드, 정확한 int/bool, 소유 thread/apartment 및 완전한 line을 검증 |
| I-10 | High | 일부 핵심 런타임 파일만 pin하면 build 후 변경을 놓침 | 실제 source_paths와 manifest의 정확한 파일 집합·각 해시를 비교. 임의 문서 대신 고정 합성 fixture와 provenance 검증 |
| I-11 | High | raw Invoke의 PyIDispatch를 동적 속성을 기대하는 제품 read_slides에 전달 | 명시적 Slides/Count/ExtractSlide 경계와 기존 strict decode_slide를 사용하는 합성 전용 읽기로 수정 |
| I-12 | Medium | passive ref bool 이벤트가 실제 true 입력도 false로 계측 | 실제 입력/출력 값을 보존하고 setByAegis=false 유지. Windows 실행용 실제 sink body harness 추가 |
| I-13 | Medium | 4096 slot의 정상 summary가 약 2.64MB인데 replay metadata reader가 1MB로 거절 | evidence 문서와 package metadata의 상한을 구분. 실제 4096 slot bundle/replay 회귀 통과 |
| I-14 | High | 살아 있는 child가 해시 계산 뒤 최종 JSONL을 계속 바꿀 수 있음 | child 전용 private spool에서 부모 소유 결과로 완성 행만 복사·fsync. 늦은 쓰기에도 최종 묶음·replay가 불변인 회귀 통과 |
| I-15 | Medium | emit 실패가 reference clear 또는 CoUninitialize를 건너뛸 수 있음 | logging 실패와 정리를 finally로 격리. drop begin/end 및 uninit begin 장애 주입에서 실제 wrapper drop→uninit 순서 통과 |
| I-16 | High | installed_directory의 파일 해시만으로 실제 COM 등록·해당 서비스 로드의 연결을 알 수 없음 | pin helper의 fresh /check와 등록 fingerprint, 같은 MachineReadGuard 아래 재비교를 추가. 실제 등록·loaded module API는 Windows gate 미실행 |
| I-17 | Medium | 실제 생성기의 기본 printerSettings binary 때문에 정상 합성 PPT도 verify_fixture에서 거절 | 생성 시 printerSettings 및 관계·참조를 제거. root가 실제 PPTX 생성/검증·업무 파일·내용 변조·외부 관계·binary 거절 5개 회귀를 작성해 모두 통과 |
| I-18 | Medium | directory lease가 없거나 attributes-only access로 열려 실제 교체 방어가 없고, close 순서상 final write 전 해제될 수 있음 | FILE_LIST_DIRECTORY 포함 ancestor lease를 별도 Native 수명으로 보유하고 bundle finish 후 release_output에서 해제. 정리 순서 fake 회귀 통과. Windows 실제 rename/reparse gate는 미실행 |

## 확대 회귀에서 확인한 기존 제한

2026-10-20 root가 `test_ppt_*.py` 전체에서 PyQt6 collection 의존인 preparation 모듈만 제외해 실행한
결과는 527 PASS / 56 SKIP / 9 FAIL이었다. 8건은 pyarrow·PyQt6 미설치에 따른 기존 production 시험
실패이고, 1건은 pytest가 monkeypatch 인자의 `Forbidden.__class__`를 조회하는 준비 단계 실패다.
실패한 기존 모듈을 변경 전 기준 소스에서 별도로 실행해 같은 9 FAIL을 모두 확인했다.
이 수치는 runner 최종 보강 전 회귀이며 최종 focused 검증과 구분한다.

저장소 지침의 `pytest knowmate/tests -v`도 root가 실행했다. 기존과 같은 collection 오류 8건
(PyQt6·pyarrow·lancedb 부재)으로 중단되었으며 전체 통과로 표시하지 않는다.

## 최종 통합 확인

collector의 정확한 숫자형·고정 필드·중복 key·부분 tail 검증, manifest의 critical source pin 완전성 및 합성 fixture 출처,
실제 launch/caller·retained target·상속 capability 증명, controller/collector/WCT 미반환 시 supervisor 완료 상한을 개발 담당에게 전달했다.
이 항목들은 최종 구현의 실제 소스·fake 회귀·문서와 대조했다. live review lock은 Windows 필수 gate 전 유지한다.

추가로 ACK 대기 뒤 Quit 직전에도 HWND의 PID/native owner thread를 검사하고, PowerPoint/Addins 등록은
COM 대신 제한된 registry 값만 수집하도록 보강했다. 실제 WCT는 OUT_OF_PROC와 숫자 thread 정보만
사용한다. managed assembly 전체 목록을 Toolhelp 모듈 목록과 같다고 주장하지 않고 부재는 unknown으로 남긴다.

root가 개발 완료 후 별도로 실행한 최종 통합 결과는 **114 PASS / 6 SKIP**이다.
대상은 `test_ppt_capture.py`, `test_ppt_capture_runner.py`, `test_ppt_capture_fixture.py`,
`test_ppt_capture_build.py`, `test_ppt_capture_buffer_native.py`, 기존 identity observation/controller/live,
trace_report 및 `scripts/diagnostics/ppt_checkpoint121` 전체다. 실행은 40.55초였다.
마지막 Quit 경계 보강 뒤 root가 실제 adapter의 소유 검사 거절/정상 Invoke 두 회귀를 추가하고,
runner+fixture를 다시 실행해 **33 PASS**를 확인했다. 중복을 제외한 최종 관련 검증은 **116 PASS / 6 SKIP**이다.

추가 두 회귀는 제품 동작을 바꾸지 않으며, 원래 checkpoint121 소스와 live 잠금도 변경하지 않았다.
Python 컴파일과 tracked/untracked 전체 whitespace 검사도 확인했다. 원래 제품 reader 버전은 1.0.5.0이며,
진단 후보의 1.0.6.0 치환은 생성한 C# 사본에만 적용한다. 일반 배포 자동종료는 활성화하지 않았다.

## 남은 실제 검증

- 현재 source review에서 재현한 지적은 수정·회귀로 확인했다. 실제 종료 원인은 확정하지 않았다.
- Linux에서는 C# 컴파일·x86/x64 실제 mapping/event body 6건을 실행하지 못했다.
- 실제 collector 급종료/drain/디스크 장애·capability·directory lease·registry·WCT·Office event ABI 및
  사용자 문서 보호의 native 통합 검증은 아직 미실행이다. fake 성공을 이 검증의 성공으로 합산하지 않는다.
- 새 live/private 진입은 고정 source gate에서 차단된다. 승인 JSON이나 CLI/environment 옵션만으로 해제되지 않는다.
- Windows gate 결과·정확한 패키지 pin·별도 Office 검토가 마련되기 전에는 설치·live 실행·배포를 진행하지 않는다.

검토 가능한 실행·판독 절차와 pin 형식은 [PPT_CAPTURE.md](../../guides/PPT_CAPTURE.md)에 있다.
이 기록은 구현·검증 결과를 보존한다. 브랜치·커밋 및 공개 검토 상태는 연결된 PR을 따른다.
