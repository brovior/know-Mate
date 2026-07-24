# architecture.md — 아키텍처 설계

| 상태 | 마지막 갱신 | 연결 문서 |
|---|---|---|
| Draft | (채택일로 갱신) | requirements.md, adr/, reviews/ |

> **사용법**: requirements.md의 Approved 요구를 받아 설계 블록을 작성한다.
> 상태 흐름: Draft → (GPT 독립 검증 = reviews/REVIEW-*.md) → Reviewed → (Claude 최종 확정) → Accepted.
> **개별 결정의 "왜"는 여기가 아니라 ADR에 쓴다** — 이 문서는 "무엇·어떻게"의 현재 스냅샷이고,
> ADR은 결정의 불변 이력이다. 설계가 바뀌면 이 문서는 덮어쓰고, ADR은 Superseded로 잇는다.

---

## 블록 템플릿

```markdown
## A-XXXX: <설계 제목>  (상태: Draft|Reviewed|Accepted / 대응 요구: R-XXXX)

### 개요
(설계 한 단락 요약 — 처음 읽는 리뷰어 기준)

### 컴포넌트와 책임
| 컴포넌트 | 책임 | 위치(모듈/경로) |
|---|---|---|

### 데이터 흐름
(입력 → 처리 → 출력. 텍스트 다이어그램 권장. 실패 경로·quarantine 분기 포함)

### 핵심 결정과 트레이드오프
- 결정: ... → 근거·대안 비교는 ADR-XXXX
(중요 결정마다 ADR 링크. ADR 없는 중요 결정 = 리뷰에서 지적 대상)

### 실패 모드
| 실패 | 감지 | 대응(격리/재시도/중단) |
|---|---|---|

### 검증 계획
(이 설계가 맞았는지 무엇으로 확인하나 — 테스트·실측 지표)

### 리뷰 이력
- reviews/REVIEW-YYYYMMDD-*.md → 처리 결과 요약 (수용 N / 기각 M)
```

---

<!-- 여기부터 실제 설계 블록을 추가한다. -->

## A-0001: 트레이 상주 앱 종료 모델 — 명시적 quit  (상태: Draft / 대응 요구: R-0001)

### 개요
현재 앱은 이벤트 루프 종료를 Qt의 암묵 규칙 `quitOnLastWindowClosed`(기본 True)에만 의존하고,
코드 어디에서도 `QApplication.quit()`을 호출하지 않는다. Qt는 "마지막으로 **보이는** 창이 닫힐
때"만 루프를 끝내므로, 창이 트레이로 숨겨진(hide) 상태에서 [종료]→`close()`를 하면 "보이는 창
닫힘" 사건이 발생하지 않아 `app.exec()`가 영영 반환되지 않는다 — 프로세스 잔존의 직접 원인.
수정: 트레이 앱 표준 관용구대로 암묵 종료를 끄고(`setQuitOnLastWindowClosed(False)`), 모든 종료
경로가 수렴하는 `_shutdown()` 끝에서 명시적으로 `quit()`을 호출한다.

### 컴포넌트와 책임
| 컴포넌트 | 책임 | 위치(모듈/경로) |
|---|---|---|
| `main()` | `app.setQuitOnLastWindowClosed(False)` 설정 | `knowmate/app/main.py` |
| `MainWindow._shutdown()` | 스케줄러 정지 → 트레이 숨김 → 워커 종료 → **마지막에 `QApplication.instance().quit()`** | `knowmate/app/main.py` |
| `lifecycle.stop_worker()` | 행오버 워커 에스컬레이션(기존 유지, 변경 없음) | `knowmate/app/lifecycle.py` |

### 데이터 흐름
```
[종료](트레이) ─┐
X (close_action=quit) ─┴→ close() → closeEvent → _shutdown()
                                      ├ scheduler.stop()
                                      ├ tray.hide()
                                      ├ stop_worker(worker)   # 행오버면 os._exit까지 에스컬레이션
                                      └ QApplication.quit()   # ★ 신규 — 창 가시성과 무관하게 루프 종료
X (close_action=tray) → event.ignore() + hide()               # 종료 아님(기존 유지)
```

### 핵심 결정과 트레이드오프
- 결정: 암묵 `quitOnLastWindowClosed` 의존을 버리고 명시적 `quit()`으로 전환 → 근거·대안은 ADR-0001
- 트레이드오프: 이후 모든 종료 경로가 `_shutdown()` 경유를 강제받는다(경로 누락 시 종료 안 됨).
  현재 종료 경로는 트레이 [종료]와 `close_action=quit` 둘뿐이며 둘 다 `closeEvent→_shutdown`으로
  수렴함을 코드로 확인.

### 정상 종료 계약 (데이터 무결성 — R-0001 NFR-1의 보장 근거, 리뷰 M-1 반영)
`quit()`은 `_shutdown()`의 **마지막** 단계이며 앞 단계들이 다음을 보장한 뒤에만 도달한다:
1. **신규 작업 차단**: `scheduler.stop()`이 유휴 타이머·복귀 워처를 정지(`IdleScheduler.stop`) →
   새 사이클 트리거 없음.
2. **진행 중 작업의 state 저장**: `stop_worker`의 graceful 경로(`cancel()` + `wait(8s)`)는 워커가
   현재 파일을 마친 뒤 취소 분기에서 `save_state()`를 호출하고 반환하는 것까지 기다린다
   (`CollectorWorker._run_cycle`의 취소 분기·정상 완료 경로 모두 `save_state` 후 종료 — 기존 코드).
   `save_state`는 tmp→replace **원자 교체**(기존 `test_atomic_save_uses_tmp_then_replace`로 보장).
3. **COM 정리**: 워커 `run()`의 finally가 `quit_com_apps()`로 소유 Office를 정리(기존 코드).
4. **로그 flush**: 정상 경로는 인터프리터 종료 시 `logging.shutdown()`(atexit), 하드 종료 경로는
   `_default_hard_exit`가 명시적으로 `logging.shutdown()` 후 `os._exit`(기존 코드).
- terminate/`os._exit` 강제 경로에서는 위 2·3이 생략될 수 있다 — 이는 "행오버로 이미 멈춘 워커"
  한정이며, 그 사이클의 state 미저장은 다음 사이클 재인덱싱으로 자가 복구된다(감수하는 트레이드오프).

### 실패 모드
| 실패 | 감지 | 대응(격리/재시도/중단) |
|---|---|---|
| `quit()` 후에도 잔존(비Qt 요인: 워커 행오버) | `stop_worker`의 wait 타임아웃 | 기존 에스컬레이션이 `os._exit(0)` (유지) |
| `_shutdown()` 도중 예외 | 각 단계 독립 try/except(기존) | 다음 단계 계속 → `quit()`은 finally 위치로 보장 |
| 새 종료 경로 추가 시 `_shutdown()` 미경유 | 코드 리뷰 규칙 | `_quit_app`/`closeEvent` 외 종료 경로 금지 문서화 |

### 검증 계획
- 사외 단위: `_shutdown()`이 각 단계(스케줄러 stop → stop_worker → quit) **순서대로** 호출하고,
  quit이 stop_worker 반환 전에 불리지 않음을 주입 스파이로 검증(PyQt6 미의존 형태로 분리).
- 사외 통합(가능 시): `QT_QPA_PLATFORM=offscreen`으로 QApplication을 띄워 창 숨김/표시/
  `close_action=quit` 세 분기에서 이벤트 루프가 실제 종료되는지 통합 테스트(리뷰 m-2 반영).
  offscreen 불가 환경이면 closeEvent 분기 단위 테스트 + 아래 실기 3경로를 릴리스 체크리스트로 고정.
- 사내 실기: ① 창 숨김 상태 [종료] ② 창 표시 상태 [종료] ③ `close_action=quit` X 클릭 —
  세 경로 모두 작업관리자에서 프로세스 소멸 확인. ④ 인덱싱 중 [종료] — 기존 에스컬레이션 동작 확인.

### 리뷰 이력
- (설계 PR 리뷰 대기)

## A-0002: purge 조회 경량화 — 컬럼 projection + 조건부 스킵  (상태: Draft / 대응 요구: R-0002)

### 개요
`_purge_removed_folders`는 "watch_folders에서 제거된 폴더의 청크를 DB에서 삭제"하는 정리
단계인데, 판단에 `file_path` 목록만 필요함에도 매 사이클 전체 테이블(벡터+암호문 포함)을
pandas로 로드한다. 수정 ①: 조회를 `file_path` 단일 컬럼 projection으로 교체. 수정 ②:
watch_folders 구성이 직전 사이클과 동일하고 이번 사이클 처리 건수가 0이면 purge 자체를 스킵
(구성 변화가 없으면 "제거된 폴더"가 새로 생길 수 없음). CleanupManager(파일 단위 orphan)는
state 기반이라 무관 — 변경 없음.

### 컴포넌트와 책임
| 컴포넌트 | 책임 | 위치(모듈/경로) |
|---|---|---|
| `_purge_removed_folders` | `file_path`만 projection 조회, 삭제 판단·실행(기존 안전장치 유지), 성공 완료 여부 반환 | `knowmate/collector/scheduler.py` |
| `_run_cycle` | 사이클 시작 시 불변 스냅샷·op_sig 계산, 스킵 판정(서명·0건·24h), 성공 시에만 meta 갱신 | `knowmate/collector/scheduler.py` |
| purge 메타 sidecar | `reconciled_sig`·`last_purge_ts` 보관(tmp→replace 원자 교체). 기존 state 스키마 불변 | `index_state.meta.json` (신규) |

### 데이터 흐름
```
사이클 시작:
  snapshot = 정규화(watch_folders)   # normcase/normpath·구분자 통일·중복 제거·정렬(불변 스냅샷,
                                     # 서명 계산과 purge 판정이 동일 스냅샷 사용)
  op_sig = SHA-256(canonical JSON)   # {"v":1, "folders":[...], "dry_run":bool,
                                     #  "max_delete_ratio":float} 를 sort_keys=True·고정
                                     # separator·UTF-8로 직렬화(필드 경계 모호성 제거, 리뷰2 m-2)
사이클 종료부:
  elapsed = now - meta["last_purge_ts"]
  if elapsed < 0: meta 비정상으로 간주 → 즉시 purge (벽시계 역행 방어, 리뷰2 m-1)
  if op_sig == meta["reconciled_sig"] and 처리 0건 and elapsed < 강제주기(기본 24h)
     and 백오프 미해당:
      purge 스킵 (DB 조회 없음)                        # ★ 빠른 경로 — O(1)
  else:
      file_paths = chunks 테이블에서 file_path 컬럼만 projection 조회
                   # Arrow 컬럼 직접 순회 — pandas 변환 생략(리뷰2 B-1 대안3).
                   # projection 불가 시 조용한 전체 로드 금지: 최적화 비활성 경고 + purge를
                   # 구성 변경 사이클로 한정(리뷰2 M-3)
      (이하 기존과 동일: 소속 판정 → 대량삭제 차단 → dry_run → 삭제 → optimize)
      성공 완료 시에만: meta["reconciled_sig"]=op_sig; meta["last_purge_ts"]=now  # 원자 갱신
      실패·차단 시 (핫루프 방지, 리뷰2 M-2):
          meta["last_attempt_ts"]=now; meta["failed_sig"]=op_sig (+사유)
          - 일시적 예외: 재시도 백오프(기본 30분, config화) 적용 — 매분 전건 재조회 금지
          - 대량삭제 차단: 동일 op_sig에 대해 자동 재시도 안 함(구성·차단율 변경으로 op_sig가
            바뀌어야 재실행). 차단 사실은 기존 UI 알림(1회)로 노출

meta 저장: index_state.json 이 아니라 **별도 sidecar 파일**(index_state.meta.json, tmp→replace
원자 교체). 기존 state 스키마(경로→dict)와 소비자 코드를 일절 건드리지 않는다(마이그레이션 불필요).
```

- **처리 0건의 정의**: 이번 사이클에서 소비자 루프가 꺼낸 태스크(성공·실패·연기 포함)가 0건이고
  취소되지 않았음. 실패·연기가 있던 사이클은 스킵하지 않는다(보수적).
- **강제 reconciliation**: 스킵이 계속되더라도 `last_purge_ts` 기준 24h(기본, config화) 경과 시
  0건이어도 purge를 1회 실행 — 외부 요인으로 생긴 state-DB 불일치가 무기한 방치되지 않는 상한.
  이 사이클은 O(N)이되 경로 데이터만 다룬다(R-0002 NFR-1 개정 문언과 일치 — 리뷰2 B-1/M-1 반영).

### 핵심 결정과 트레이드오프
- 결정: projection 방식은 `table.search().select(["file_path"]).limit(...)` 대신
  **`table.to_lance().to_table(columns=["file_path"])`** 계열(전건 조회에 limit 불필요·벡터 미로드)
  을 1순위로 검토하되, 설치된 lancedb 0.6+ API에서 동작 확인 후 확정 → 근거·대안은 ADR-0002
- 결정: 스킵 조건은 "op_sig(구성+dry_run+차단율) 불변 && 처리 0건 && 24h 미경과". 서명은 purge가
  **성공 완료된 경우에만** 갱신한다(실패·차단 시 미갱신 → 재시도 보존). → 근거는 ADR-0002
- 결정: 메타는 sidecar 파일(index_state.meta.json) — 기존 state 스키마·소비자 무변경 (리뷰 M-2 반영)
- 트레이드오프: 불일치 복구가 최대 24h(강제 주기)까지 지연될 수 있다 — 무기한 방치는 강제
  reconciliation으로 차단(리뷰 B-1 반영). sidecar 파일이 1개 늘어난다.

### 실패 모드
| 실패 | 감지 | 대응(격리/재시도/중단) |
|---|---|---|
| projection API가 해당 lancedb 버전에 없음 | 구현 단계에서 배포 고정 버전으로 확정·문서 기록 | **조용한 전체 로드 폴백 금지** — 최적화 비활성을 경고하고 purge를 구성 변경 사이클로 한정(리뷰2 M-3) |
| purge 도중 일시적 예외 | purge 반환/예외 | reconciled_sig 미갱신 + **재시도 백오프**(기본 30분) — 매 사이클 전건 재조회 금지(리뷰2 M-2) |
| 대량삭제 차단 지속 | 차단 판정 | 동일 op_sig 자동 재시도 안 함 — 구성·차단율 변경 시에만 재실행, UI 알림 1회(리뷰2 M-2) |
| 시스템 시각 역행 | now < last_purge_ts | meta 비정상 간주 → 즉시 purge(리뷰2 m-1) |
| 스킵 오판(purge 필요한데 스킵) | op_sig 비교 로직 테스트 | SHA-256 + 정규화 스냅샷(프로세스 간 안정, 리뷰 m-1 반영). 잔여 위험은 24h 강제 reconciliation이 상한 |
| sidecar 메타 파일 손상/유실 | 로드 실패 | meta 없음 = "스킵 불가"로 간주(보수적) → 그 사이클 purge 실행 후 재생성 |
| 장기 유휴로 스킵만 반복 | last_purge_ts 경과 | 24h 초과 시 0건이어도 강제 purge(리뷰 B-1 반영) |

### 검증 계획
- 사외: ① projection 결과에 vector/text 부재 검증 ② 스킵 조건 단위 테스트(서명 동일+0건+24h 미경과
  → 조회 스파이 미호출 / 서명·dry_run·차단율 변경 또는 24h 경과 → 호출) ③ purge 예외·대량삭제 차단
  시 meta 미갱신 → 다음 사이클 재실행 검증 ④ meta 파일 부재/손상 시 purge 실행(보수적 폴백) ⑤
  watch_folder 제거 시나리오 회귀(기존 테스트 유지 통과) ⑥ op_sig가 경로 대소문자·구분자 차이에
  불변임을 검증.
- 사내 실측: 유휴 방치 1시간 동안 작업관리자 RSS 추이 — 수정 전(우상향 눌러앉음) 대비 평탄화 확인.
  lancedb 실환경에서 projection API의 컬럼 미로드(pushdown) 실측(리뷰 '확인 필요' 반영).

### 리뷰 이력
- (설계 PR 리뷰 대기)
