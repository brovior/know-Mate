# PPT 종료 수집 계획 r2 — 새 검증자 최종 독립 검토

**판정: 계획 검토 종료 기준 충족. 미해결 Critical/High/Medium/Low 각 0건.**
구현·시험 통과·설치·live 실행·배포 승인 판정은 아니다.

- 검토일: 2026-10-20
- 검증자: `capture_plan_final_review` — 계획 작성과 앞선 검토에 참여하지 않은 새 컨텍스트의 별도 에이전트
- 대상: [전체 계획 r2](../../design/PPT_SHUTDOWN_CAPTURE_PLAN.md), [고정 사본](plan-r2.txt)
- 두 파일의 SHA-256: `aca9def288c72d6ac2f3304055f140a6c7570d011cfc26753eafc1313405d656`
- 제공된 기준: `brovior/know-Mate` main `b75c98511cea3ddb7d842c7204dd11b4f9767ea3`
- 방법: 계획 전체를 먼저 읽고 현지 소스·정본과 독립 대조했다. 선행 리뷰·처리 기록의 판정을 근거로 사용하지 않았다.

## 발견사항

새 설계 결함을 발견하지 않았다. 억지로 finding을 추가하지 않았으며 심각도 하향이나 환경 시험 면제로 종료 기준을 맞추지 않았다.

| 심각도 | 미해결 |
|---|---:|
| Critical | 0 |
| High | 0 |
| Medium | 0 |
| Low | 0 |

따라서 수정 요구가 있는 finding ID·발생 조건·재검증 항목은 없다. 아래는 판정 근거와 별도 미실행 gate이며 결함 건수에 합산하지 않는다.

## 실제 근거와 계획 대조

| 검토 항목 | 실제 근거 | r2에서 확인한 계약과 판단 |
|---|---|---|
| 과거 결과의 범위 | `docs/DIAGNOSTICS.md`의 113/115 내부 반환 후 생존, 119 identity 거절, 121 controller unknown·host 미실행 | §1–2가 과거 결과와 가설을 분리한다. 121 읽기 성공이나 외부 Quit을 내부 종료 증명으로 사용하지 않는다. |
| controller·host 관측 순서 | `ppt_identity_observation.py`의 `collect`/`describe` 및 합쳐진 `get`/`item` 경계; `ppt_identity_controller.py:63`의 9행 선행 조건; `PptExternalIdentityObservation.cs:177`의 순차 `Describe` | §3–4가 필수 관측·host·저장을 요청과 분리하고 알려진 선택 getter는 `omitted_by_plan`으로 표시한다. 핵심 identity를 먼저 얻되 첫 unknown 이후 후속 조사를 금지한다. 기존 `complete_host`의 4행·cleanup·소유권·self-control 조건을 유지한다. |
| internal과 external의 종료 권한 | `PptReader.cs:245`의 `AllowNormalShutdown`; `PptExternalConnect.cs:39`와 `ProcessFence.TryClaim`; `PptExternalBootstrap.cs`·`PptExternalProtocol.cs`; `controller_support.py`의 외부 Quit handshake | §3의 internal은 Bind/Observe 후 기존 `TryQuitIfIdle`만 1회 호출한다. Prepare/START/ACK/COMPLETE를 섞지 않는다. external은 기존 Ready/status·ACK·정상 반환 후 COMPLETE를 유지한다. 관측·mapping·환경변수 자체를 권한으로 승격하지 않으며 process fence 재설정·동일 PID 재시도가 없다. |
| trace 범위와 PPT 밖 보존 | `PptExternalTrace.cs:50`의 terminal 잠금과 `:55`의 PPT 내부 ThreadPool; `ppt_trace_report.py:52` 이후 intact frame의 부재 stage 판독 | §5–6이 독립 collector와 상속 mapping, publish/persisted 순번, final drain을 정한다. schema2 frame을 이후 lifecycle 완결로 사용하지 않는다. 요청 완결·수집 종료·OS 종료·유실을 각각 판정한다. |
| callback·사용자 문서 보호 | `PptEventSink.cs:19`의 Active 때 자체 Cancel 설정; `PptExternalGuard.cs`의 상태·Count·Quit 및 lease 정리; `PptReader.cs`의 종료 lifecycle | §5–6은 정상 callback 중첩을 보존하고 append 자체 재진입·off-thread 손실과 구별한다. `cancel_in/out/set_by_aegis`를 기록하되 다른 subscriber의 최종 Cancel은 추측하지 않는다. 이벤트 인수의 문서 조회·callback I/O·강제 GC를 추가하지 않는다. |
| 실제 종료와 caller 정리 | `observe_processes.py:60`의 어느 관측 PID 종료 때 감시 중단; `run.py:187`의 30초 참조 보유와 강제 GC; `knowmate/secure/ppt_shutdown.py`의 pending·OS-only observation | §6–7은 retained target handle만 종료 근거로 쓰고 다른 PID 종료와 분리한다. 정상 참조 해제→균형 잡힌 CoUninitialize→caller exit를 별도 기록한다. 실제 post-cleanup 관측 시간만 보고하며 hang·deadline을 종료 성공으로 바꾸지 않는다. |
| 보안·호환성·활성화 경계 | `AGENTS.md`·`CLAUDE.md`; `PPT_CHECKPOINT_HANDOFF.md`; `PPT_ADDIN_INDEXING.md` 자동종료 후보 절 | §1·8–9는 본문 비출력, 진단 전용 빌드, 새 pin, 기본/released 비활성, 기존 runner 잠금과 ABI·ACL·writer gate를 유지한다. 새 도구의 COM/Win32는 secure에 격리하며 운영 QThread 계약을 대체하지 않는다. |

표의 소스 파일은 `scripts/ppt_reader/`, `scripts/ppt_events/`, `scripts/diagnostics/ppt_checkpoint121/` 아래의 실제 파일을 읽어 대조했다. `PptShutdownState.cs`, `PptShutdownGuard.cs`, `PptShutdownEvents.cs`, `PptExternalObservationEntry.cs`, `PptExternalProcessFence.cs`, `ppt_identity_live.py`, `verify_pins.py`도 함께 확인했다.

공유 mapping의 타입·상속 동일성·run binding·x86/x64 publish 가시성, 손실 counter 및 WCT 비반환은 구현 전제로 증명됐다고 세지 않았다. 계획이 native 시험 전 새 live 도구 잠금 유지와 실패 시 재설계를 명시하므로 실행 gate로 남긴다. 구현 시 이 조건을 충족하지 못하면 이번 계획 판정을 시험 통과 근거로 사용할 수 없다.

## 미실행·자료 한계와 계속할 조건

| ID | 미실행·제한 | 계속할 조건 |
|---|---|---|
| G-B-01 | 현재 workspace는 부분 스냅샷이며 Git checkout이 없다. 제공된 기준 commit과 전체 저장소의 동일성을 직접 검증하지 못했다. `docs/ROADMAP.md`, 일부 위임 dev 지침과 `knowmate/tests/`도 없다. | 후속 구현 전 완전 checkout에서 AGENTS/CLAUDE 위임 자료·현재 변경 상태·정본 상태를 재확인한다. |
| G-B-02 | 제품 구현과 새 fake 회귀, `pytest knowmate/tests -v`, 진단 빌드·frozen package·pin 검증은 모두 미실행이다. | 계획 §8 순서로 구현하고 §9 회귀·default-off·기존 필수 gate를 통과한다. 과거 결과를 새 구현 결과로 복사하지 않는다. |
| G-B-03 | Windows native mapping·메모리 가시성·ACL·handle/seq·디스크/수집기 실패·WCT 시험은 미실행이다. | §9의 x86/x64 native 시험을 통과하기 전 새 live 도구 잠금을 유지한다. 권한 부족은 UNEXECUTED로 보존한다. |
| G-B-04 | Windows/Office 합성·실제 event ABI·사용자 문서 경합·hang 시 외부 수집·계측 오버헤드, 사내 DRM·UAC·배포 시험은 미실행이다. 과거 원 로그·설치본도 없다. | 구체적인 새 패키지·절차의 별도 승인 시험 및 운영 정본의 독립 gate를 따른다. 자동종료 출시 비활성을 유지한다. |

이번 검토는 파일 읽기·정적 대조·대상 hash 확인과 이 리뷰 문서 저장만 수행했다. 계획 원문·제품 코드를 수정하지 않았고 설치·등록·Office/live 실행·커밋·GitHub 쓰기를 수행하지 않았다.
