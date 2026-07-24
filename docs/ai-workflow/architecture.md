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

### 실패 모드
| 실패 | 감지 | 대응(격리/재시도/중단) |
|---|---|---|
| `quit()` 후에도 잔존(비Qt 요인: 워커 행오버) | `stop_worker`의 wait 타임아웃 | 기존 에스컬레이션이 `os._exit(0)` (유지) |
| `_shutdown()` 도중 예외 | 각 단계 독립 try/except(기존) | 다음 단계 계속 → `quit()`은 finally 위치로 보장 |
| 새 종료 경로 추가 시 `_shutdown()` 미경유 | 코드 리뷰 규칙 | `_quit_app`/`closeEvent` 외 종료 경로 금지 문서화 |

### 검증 계획
- 사외: `_shutdown()`이 `quit` 콜백을 호출하는지 주입 스파이로 단위 테스트(PyQt6 미의존 형태로 분리).
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
| `_purge_removed_folders` | `file_path`만 projection 조회, 삭제 판단·실행(기존 안전장치 유지) | `knowmate/collector/scheduler.py` |
| `_run_cycle` | 스킵 조건 판정(watch_folders 서명 비교 + 처리 0건) 후 purge 호출 여부 결정 | `knowmate/collector/scheduler.py` |
| state 파일 | 직전 사이클의 watch_folders 서명(정렬·정규화된 목록 해시) 보관 | `index_state.json` 메타 키 |

### 데이터 흐름
```
사이클 종료부:
  watch_sig = hash(정규화(watch_folders))
  if watch_sig == state["_watch_sig"] and 처리 0건:
      purge 스킵 (DB 조회 없음)                       # ★ 신규 빠른 경로
  else:
      file_paths = chunks 테이블에서 file_path 컬럼만 조회   # ★ projection
      (이하 기존과 동일: 소속 판정 → 대량삭제 차단 → dry_run → 삭제 → optimize)
  state["_watch_sig"] = watch_sig
```

### 핵심 결정과 트레이드오프
- 결정: projection 방식은 `table.search().select(["file_path"]).limit(...)` 대신
  **`table.to_lance().to_table(columns=["file_path"])`** 계열(전건 조회에 limit 불필요·벡터 미로드)
  을 1순위로 검토하되, 설치된 lancedb 0.6+ API에서 동작 확인 후 확정 → 근거·대안은 ADR-0002
- 결정: 스킵 조건은 "watch_folders 서명 불변 && 처리 0건" — 서명이 바뀐 사이클은 반드시 purge 실행
- 트레이드오프: state-DB 불일치(외부 요인으로 DB에만 남은 고아 경로)의 복구가 "구성 변경 또는
  파일 변경이 있는 사이클"로 지연된다. 불일치의 발생 원인 자체가 그런 사이클(삭제 실패 등)이므로
  실질 지연은 제한적이라고 판단 — 리뷰 검증 요청 항목.

### 실패 모드
| 실패 | 감지 | 대응(격리/재시도/중단) |
|---|---|---|
| projection API가 해당 lancedb 버전에 없음 | 구현 시 즉시 확인 | 기존 `to_arrow().to_pandas()`로 폴백하되 컬럼 select 가능한 다른 API 채택 |
| 스킵 오판(purge 필요한데 스킵) | watch_sig 비교 로직 테스트 | 서명에 정규화(구분자·대소문자) 포함, 의심 시 스킵 없이 실행하는 보수적 기본 |
| state 메타 키(_watch_sig)와 파일 경로 키 충돌 | 키 네임스페이스(_접두) | 기존 로직이 경로 키만 순회하는지 확인 후 도입 |

### 검증 계획
- 사외: ① projection 결과에 vector/text 부재 검증 ② 스킵 조건 단위 테스트(서명 동일+0건 → 조회
  스파이 미호출 / 서명 변경 → 호출) ③ watch_folder 제거 시나리오 회귀(기존 테스트 유지 통과).
- 사내 실측: 유휴 방치 1시간 동안 작업관리자 RSS 추이 — 수정 전(우상향 눌러앉음) 대비 평탄화 확인.

### 리뷰 이력
- (설계 PR 리뷰 대기)
