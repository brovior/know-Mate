# 이슈 B (보류) — 쿼리 비동기화로 UI 프리징 제거

> 상태: **미착수 / 별도 PR 예정**
> 분리 사유: 동시성 모델 변경으로 회귀 위험이 커서, 안전한 개선(이슈 #4)과
> 묶지 않고 격리해 신중히 처리한다. 1b가 빠져도 "쿼리 중 프리징"은 기존부터
> 있던 동작이라 회귀가 아니다(나빠지는 것 없음).

## 배경 / 현재 동작

- `bridge.sendQuery`가 **메인 스레드에서 `agent.handle()`를 동기 실행**
  (`knowmate/app/bridge.py:53`). 임베딩 + 벡터검색 + LLM HTTP 호출(수십 초)
  동안 Qt 메인 이벤트 루프가 막혀 **창 드래그·로딩 애니메이션까지 멈춘다.**
- 로딩 점 애니메이션 JS(`showLoading`, `app.js`)와 `waiting` 가드는 이미 존재.
- 목표: `agent.handle()`를 QThread 워커로 분리해, 응답 대기 중에도 UI가
  살아있게 한다. (병렬 처리가 목적이 아니라 **메인 루프 비차단**이 목적)

## 위험 분석

### A. Qt 스레딩 메커니즘
1. **QThread 수명 관리** — 워커를 지역변수로 만들면 함수 반환 시 GC되어
   "QThread: Destroyed while thread is still running" 크래시. 참조를 `self`에
   보관하고 `finished`에서 정리해야 한다.
2. **스레드 친화성** — `Bridge`는 메인 스레드 QObject. 워커에서 시그널
   `responseReady` emit은 안전(큐드 커넥션)하지만, 워커가 위젯/`self._win`
   등 UI 객체를 직접 건드리면 즉시 크래시. 워커는 "순수 계산 + 시그널"만.

### B. 공유 상태 스레드 안전성 (이 프로젝트의 핵심 위험)
3. **LanceDB 연결의 스레드 친화성** ⚠️ 최우선
   - `knowledge_agent`가 파이프라인(Indexer→LanceDB table)을 싱글톤 캐시.
     메인 스레드에서 만든 table 객체를 워커에서 사용하게 됨.
   - 동시에 수집기(collector) 워커가 같은 LanceDB에 **쓰기** 중일 수 있음
     ("수집 쓰기 + 쿼리 읽기" 동시 발생). CLAUDE.md 원칙8이 multiprocessing을
     피한 것이 파일 락 충돌 때문 — 스레드 동시 접근도 같은 연결 객체를
     공유하면 위험.
   - **완화**: 쿼리 워커는 자기 스레드에서 LanceDB 연결을 새로 열고, 메인에서
     만든 table 객체를 공유하지 않는다(읽기 전용 분리). LanceDB는 버전
     스냅샷이라 읽는 중 쓰기로 깨지진 않으나, connection 객체 공유는 금지.
4. **sentence-transformers/torch 싱글톤 lazy-init 레이스**
   - `embedding.py`의 `_local_model`은 전역 싱글톤 + 락 없음. 두 쿼리가 거의
     동시에 첫 호출 시 모델 이중 로드/부분초기화 공유 위험(로드가 수 초라
     레이스 창이 큼).
   - **완화**: `_get_local_model`에 `threading.Lock`으로 init 보호.
5. **COM(win32com)** — 인덱싱 경로에서만 사용(STA, 스레드 종속). 쿼리 경로는
   COM 미사용이라 직접 위험 없음. 경로 분리만 유지하면 됨.

### C. 동시성·순서·생명주기 (UX)
6. **응답 오배치** — 비동기 시, 응답 대기 중 사용자가 모드 전환/새 대화를
   하면 늦게 도착한 응답이 바뀐 `currentThread`에 잘못 append됨.
   - **완화**: 요청에 `request_id`(또는 thread_id+mode)를 실어 보내고 응답에
     echo → JS가 현재 컨텍스트와 일치할 때만 렌더, 불일치는 폐기.
7. **워커가 조용히 죽으면 영구 프리징** — `run()`에서 예외가 새면
   `responseReady`가 영영 emit 안 됨 → JS `waiting=true` 고정, 입력창 영구
   비활성, 로딩 점이 안 사라짐.
   - **완화**: 워커 내부를 `try/except/finally`로 감싸 무슨 일이 있어도 반드시
     한 번 emit(성공 또는 에러 블록) + JS 워치독 타임아웃(예: 90초).
8. **취소 부재** — LLM 호출(30~60초)은 중간 취소 불가(urllib timeout만 있음).
   - **완화**: 최소한 6번(request_id)로 "이전 결과 무시" + 앱 종료 시 워커
     graceful 정리. 진짜 취소는 범위 밖으로 명시.
9. **스레드 누수/폭주** — 매 쿼리마다 새 QThread 생성 후 미정리 시 누수.
   single-flight가 깨지면 임베딩 모델 동시 2회 실행 → CPU 폭주.
   - **완화**: 백엔드에서도 single-flight 강제(진행 중이면 거부) + `finished`
     에서 워커 정리. 또는 `QThreadPool`+`QRunnable`(시그널은 QObject holder).

### D. GIL 현실 체크
- GIL로 완전 병렬은 안 되지만 목표는 메인 이벤트 루프 비차단. urllib I/O와
  torch C확장은 GIL을 풀어주므로 창 드래그·애니메이션이 살아난다. 순수 파이썬
  CPU 구간(청크/병합)에선 약간의 잔김 가능 — 프리징보다는 훨씬 낫다.

## 권장 설계 (위 위험을 한 번에 막는 형태)
1. **single-flight + request_id**: 진행 중이면 새 요청 거부, 모든 응답에 id echo
   → JS가 현재 컨텍스트 일치 시에만 렌더
2. **워커는 순수 계산만**, 끝나면 `responseReady(id, blocks)` emit, 내부
   `try/finally`로 반드시 emit
3. **워커 참조를 `self`에 보관**, `finished`에서 정리(수명 크래시 방지)
4. **임베딩 모델 init에 Lock** 추가
5. **쿼리 파이프라인의 LanceDB는 워커 스레드 기준으로 연결**(메인 table 공유
   금지), 수집기와 연결 분리
6. **JS 워치독 타임아웃**으로 영구 프리징 최종 방어

## 제안 분할 (2단계)
- **(a) 기본 스레드화** — QThread 분리 + 반드시-emit(try/finally) + 워커 수명
  관리. 이것만으로 프리징 해소. (필수)
- **(b) 동시성 견고화** — request_id 정합성 + 모델 Lock + LanceDB 연결 분리 +
  JS 워치독. 엣지케이스 차단.

## 완료 기준
- LLM 응답 대기 중에도 창 드래그·로딩 점 애니메이션 정상 동작
- 응답 도착 시 올바른 스레드/모드에만 렌더(오배치 없음)
- 워커 예외·앱 종료 시에도 UI 잠금/크래시 없음
- `pytest knowmate/tests` 전체 통과(필요 시 `qtbot.waitSignal`로 비동기 검증)

## 관련
- 선행: 이슈 #4의 2a·2b·2c(프록시 우회·모드 정정) — 비동기로 돌렸을 때 실제
  LLM 응답이 와야 end-to-end 검증 가능
- 원 출처: `IMPROVEMENT.MD` 1b, `CLAUDE.md` 5장 원칙8(QThread 워커 + pyqtSignal)
