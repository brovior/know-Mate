# 성능·크래시 진단과 개선 후보

현재 원인 분석과 다음 검증의 정본이다. 작업 상태는 [ROADMAP.md](ROADMAP.md), 구현 계약은
[DESIGN.md](design/DESIGN.md), 완료 이력은 [UPDATE_NOTES.md](UPDATE_NOTES.md)를 따른다.

## 현재 판단 (2026-10-13)

PowerPoint 내부 추가 기능은 VDI와 사내 PC의 소수 문서에서 내용 일치·속도 개선이 보고됐다.
[운영 적용·설치 설계](design/PPT_ADDIN_INDEXING.md)는 완료했으며 운영 구현과 연속 인덱싱 검증은 남아 있다.
사내 크래시 기록도 확보하며 SQLite 전환이나 추적 수명 변경은 아직 구현하기로 결정하지 않았다.

| 항목 | 확인된 사실 | 미확정 사항·다음 검증 |
|---|---|---|
| 앱 비정상 종료 | 사용자가 사내 발생을 보고함. 진단 활성 시 사이클마다 추적 시작·종료 | 오류 모듈·예외 코드·종료 단계 확보 |
| 메일 처리 중 종료 후 WINWORD 잔류 | 트레이 종료 후 워커 강제 종료·하드 종료 로그 확보, 문서 완료 시 정리·임베딩 취소 구현 | §4의 사내 종료·PID 검증 대기 |
| 문서 상태 저장 | JSON 전체 읽기·재작성, 경로 조회는 메모리 딕셔너리 | 사내 JSON 읽기·쓰기와 파일 접근 시간을 분리 측정 |
| 유휴 폴더 확인 | 폴더 순회 후 삭제 확인에서 파일 존재를 다시 조회 | 로컬·SMB별 순회·존재 확인 비용 측정 |
| 검색 규모 | 검색용 벡터 인덱스 생성 코드 없음, 질의 처리는 동기 실행 | 청크 수와 검색·LLM 시간을 측정한 뒤 개선 선택 |
| 임베딩·Office 지연 | 32청크 호출에서 미계측 4.82~5.52초 보고, 응답 처리·CPU 계측 보강 | 새 구간 로그와 메모리 진단 설정 확보, Office open 편차 실측 |
| PowerPoint COM 읽기 | 내부 추가 기능의 합성 반복·실문서 비교에서 사용자 보고로 내용 일치와 속도 개선 확인 | 아래 §사내 검증 보고. 운영 설치·연속 인덱싱·메모리·종료 검증 대기 |

2026-10-09에는 Word·Excel `Quit()` 미반환 또는 후속 종료 미확인으로 소유 registry가 지워져 다음
사이클이 같은 Office를 재사용하거나 사용자 점유로 오판할 수 있는 경로를 보강했다. 보고된 장시간
사례의 원본 로그는 없어 실제 원인이 Quit hang, 강제종료 실패, identity 조회 실패 중 무엇인지는
확정하지 않았다. 새 로그는 PID·EXE·시간·정리 결과만 남기며 문서 내용은 기록하지 않는다.

### COM poison 복구 일관성 (2026-10-15)

실제 종료·소유 기록 제거 후 2초 목록이 남는 문제, 진행 중인 열거/guard가 옛 결과를 사용하는
경합, Word·Excel 설정 완료/재사용과 정리가 겹치는 경합을 제품 코드에서 보완했다.
최초 오류 뒤 Close RPC 오류가 생겼을 때 단계와 code를 섞던 경로도 분리했다.
공통 판독기는 내부 구조화 HRESULT와 bounded cause를 처리한다. 사용자 Office와
미검증 PID를 추정 종료하지 않으며 PPT Quit·kill 금지를 유지한다.

실제 제품 helper·getter·parse를 사용하는 fake 회귀는 event로 열거/identity 시점을 고정하고
실제 registry mutator로 cleanup ABA를 만든다. 정확한 generation·앱/proof pair,
잠금 밖 참조 해제, 최초 분류·이중 오류 증거·민감 문구 불로그 및 PPT fresh preflight를 확인한다.
설계 모델 통과와 구분하여 제품 회귀 결과는 PR의 검증 기록을 따른다.

사내에서는 동일 EXE의 RPC 단절 → actual exit → 다음 파일의 attempt/guard epoch·PID를
연결해 확인한다. 사용자 Word·Excel 동시 실행, 조회 권한 실패, PID 재사용, HWND 대체 창,
PPT 재접속·중단 Close도 확인해야 한다. 실제 Windows·Office·DRM·COM Release는 미실행이다.
잠금 검사 이후 새로운 OS 종료·사용자 실행을 원자화하지 않으며 즉시 GC를 보장하지 않는다.

## 1. 앱 크래시와 메모리 진단

### 현재 사용법과 검증 대기

기본값 `collector.memory_diagnostics_enabled: false`는 유지한다. 일반 사용량 로그가 필요하면
`true`와 `memory_diagnostics_mode: basic`을 사용한다. 기존 true 설정도 mode를 추가하지 않으면
basic으로 동작한다. 설정을 바꾼 뒤 앱을 재시작한다. 계약은
[설계 문서의 메모리 진단](design/DESIGN.md)을 따른다.

| 모드 | 기록·동작 | 부하 해석 |
|---|---|---|
| OFF | 조회·진단 로그·추적·진단 GC 없음 | 평소 기본값 |
| basic | Windows Private Bytes·delta와 이미 로드된 Arrow 풀 수치 | tracemalloc API·스냅샷·강제 GC 없음. 외부 tracing 자체의 부하는 남음 |
| detailed | 기존 프로세스 전체 depth 10 추적·스냅샷 비교·종료 GC | 명시 선택한 짧은 재현에만 사용. 외부 추적의 depth·peak·수명 보존 |

basic은 Python 값을 `n/a`, `python_scope=not_measured`로 기록한다. 미로드 PyArrow를 수치 조회
때문에 import하지 않으며 Windows가 아니거나 조회 실패하면 해당 수치는 `n/a`다. Private 조회가
실패한 표본과 다음 유효 표본의 delta도 `n/a`다. Arrow peak는 사이클 peak가 아닌 풀 수명 고수위다.
Private에서 Python·Arrow를 빼서 네이티브 사용량을 확정하지 않는다.

기존 단계·메일 첫/약 50건/마지막 묶음·optimize 전후 표본 빈도는 유지한다. COM 정리 뒤 basic은
`cycle_resources_released`를 남기며 GC·추적 시작/종료·스냅샷 로그는 생성하지 않는다.
이 표본은 작업 참조 정리 뒤 사용량이지 전체 메모리 반환 보장이 아니다. detailed는 실제 GC 전후와
자체 trace 시작/종료·스냅샷 경계를 유지한다. 자체 추적 current는 추적 이후 살아 있는 할당만 포함하며
`python_scope`·`trace_started_at_ns`·`tracemalloc_mib`로 범위와 추적기 사용량을 표시한다.

진단의 Python 예외는 종류만 보고하고 수집 결과를 바꾸지 않는다. 오류 보고 자체가 실패해도 COM
정리·진단 참조 해제·idle 전환을 계속하며 optimize 전후 콜백 실패로 성공/쿨다운을 바꾸지 않는다.
수집 완료 시그널은 `_run_cycle()` 끝에서 전송되어 COM·진단 종료보다 먼저다. 다음 자동 수집은
`isRunning()`으로 중복 실행을 막는다. 강제 QThread 중단·네이티브 크래시는 finally를 보장하지 않는다.

**운영 권장 전 검증 대기**: 같은 입력·임시 인덱스 조건의 OFF/basic을 각각 최소 3회 비교하고,
외부 tracing이 없는 상태에서 basic median 경과시간 증가 5% 이내를 확인한다. 실제 Windows EXE에서
30분 이상 반복, 취소·다음 사이클·앱 종료와 저장 결과 동일성도 확인한다. 사외 fake 성능 비교는
Windows Private·Office·사내 API 검증을 대신하지 않는다. 현장 시험 전 기본 OFF를 유지한다.

### 로컬 합성 비교 (2026-10-15)

Linux에서 합성 32×1024 임베딩 응답을 실제 JSON 해석·벡터 검증 함수로 처리했다.
워밍업 뒤 각 모드를 별도 프로세스에서 3회씩 순서를 바꿔 실행한 경과시간 중앙값이다.
일반 로그는 이미 로드된 Arrow 풀과 단계·50회 단위 표본을 실제 파일에 기록했다.

| 같은 작업의 반복 수 | OFF | basic | detailed |
|---|---:|---:|---:|
| 1,800회 | 12.955초 | 12.865초 | 미실행 |
| 30회 | 0.210초 | 0.227초 | 5.603초 |

긴 OFF/basic 비교는 목표인 중앙값 증가 5% 이내를 충족했다. 작은 시간 차이를 성능 향상으로
해석하지 않으며 짧은 사이클의 표본·파일 로그 비용은 남는다. 각 비교 안에서 입력 hash·처리
벡터 수·checksum은 같았다. 초기 1,500회 detailed 측정은 시간이 과도해 중단하고 별도 짧은 동일
작업 비교로 나눴다. 긴 상세 진단이나 실제 Windows 부하가 검증됐다는 의미는 아니다.
Private Bytes·Office·사내 API·실제 메모리 누수와 크래시 감소 효과는 미측정이다.

### 가설과 한계

사내 LLM은 반복 추적 시작·종료를 유력 원인으로 제시했다. 네이티브 스레드의
`PyTraceMalloc_Track()`와 `tracemalloc.stop()` 경합으로 크래시가 발생한
[CPython 보고](https://github.com/python/cpython/issues/128679)가 있다. 보고의 재현 환경은
Python 3.12/3.13·Linux다. 이 프로젝트의 Windows·Python 3.11에 동일 결함이 적용되거나,
사용 중인 라이브러리가 해당 경로를 실행한다는 증거는 아직 없다.

진단을 끄면 추적뿐 아니라 스냅샷·강제 GC도 함께 꺼진다. 크래시가 사라져도
반복 `stop()`만 원인이라고 확정하지 않는다. 앱의 사용자 종료 처리에서 실행하는
`QThread.terminate()`·`os._exit()`와 Office 워치독의 프로세스 종료도 구분한다.

### 증거 확보 순서

1. 사내 앱 버전·빌드 커밋·실제 Python/의존성 버전, 진단 설정, 발생 시각·작업·종료 요청
   여부, 직전 로그와 Windows 응용 프로그램 오류의 오류 모듈·예외 코드를 확보한다.
2. 먼저 OFF/basic의 같은 작업 로그를 PID·사이클 ID별로 비교한다. 상세 자료가 꼭 필요할 때만
   detailed의 COM 정리·GC·스냅샷 비교·추적 종료 경계를 짧게 확인한다. 추가 자료가 필요하면 `faulthandler` 또는 Windows 크래시 덤프도 검토한다.
3. 동일 조건에서 진단 활성/비활성을 비교하고, 관련성이 확인되면 추적을 한 번 시작해 유지하는
   방식, 스냅샷, 강제 GC를 분리해서 비교한다. 간헐적 증상이므로 한 번 성공으로 판정하지 않는다.

detailed 로그는 `after_gc_collect` 뒤의 스냅샷 비교와 추적 종료를 구분한다. 전후 표본은 Python
예외로 작업이 종료된 경우에도 남으므로, 완료 표본만으로 작업 성공을 판정하지 않는다.
`sys.excepthook`은 있으며 `faulthandler`는 없다. 테스트는 진단 비활성의 무계측, 외부 추적 보존,
표본 범위·배치 결과·정리 순서 등을 확인하며 Windows 네이티브 경합 재현 검증은 아니다.
추가 진단은 문서·메일 본문과 복호화 평문을 저장하지 않는다. 덤프는 프로세스 메모리를
포함할 수 있으므로 사내 취급 절차를 확인하고 외부로 반출하지 않는다.

## 2. 상태 JSON과 유휴 스캔

### 현재 구조

문서 상태는 `index_state.json`이다. `collector/state.py`가 사이클 시작에 전체 JSON을
딕셔너리로 읽고, 수집기는 `state.get(path)`로 수정 시각·크기·포맷 정보를 비교한다.
파일마다 JSON 기록을 순차 검색하지 않는다. 변경 없는 문서는 추출·임베딩하지 않는다.

저장은 전체 JSON을 임시 파일에 쓴 뒤 교체한다. 현재는 상태가 바뀐 경우에만 기본
50건 또는 30초 확인 시점과 종료에 묶어 저장한다. 변경 없는 사이클은 문서 상태를 재저장하지
않는다. 기록에는 파일마다 `chunk_ids`도 포함되므로 비용은 파일 수뿐 아니라 청크 수에 좌우된다.

폴더 순회와 별도로 `CleanupManager._find_orphans()`가 기록된 파일마다 `Path.exists()`를
호출한다. 감시 폴더별로 상태 전체를 훑고 `_count_indexed()`도 다시 순회한다.
문서·메일 폴더 열거와 실제 파일 존재 확인 비용은 상태 조회 색인으로 없어지지 않는다.

### 합성 측정 (2026-10-06)

Windows·Python 3.11.0에서 합성 경로·현재 문서 상태 필드·UUID 청크 ID로 측정했다.
`json.loads`, 기존 `classify_changes`, `json.dumps(ensure_ascii=False, indent=2)`를 각각
5회 실행한 중앙값이다. 실제 파일의 내용은 사용하지 않았다.

| 파일 기록 | 파일당 청크 | JSON 크기 | JSON 해석 | 변경 비교 | 저장 문자열 생성 |
|---:|---:|---:|---:|---:|---:|
| 300 | 10 | 0.26 MiB | 1.0 ms | 0.1 ms | 6.1 ms |
| 3,000 | 10 | 2.60 MiB | 14.1 ms | 1.0 ms | 70.1 ms |
| 10,000 | 10 | 8.68 MiB | 63.1 ms | 4.8 ms | 266.1 ms |
| 3,000 | 100 | 14.45 MiB | 69.0 ms | 1.3 ms | 283.0 ms |
| 3,000 | 500 | 67.09 MiB | 260.3 ms | 1.0 ms | 1,104.9 ms |

디스크 I/O, 추적 활성 시 비용, SMB, Office, 실제 앱 전체는 포함하지 않는다.
비교는 `classify_changes` 측정이며 수집기 전체 시간이 아니다.
같은 로컬 파일을 3,000번 `exists()`로 확인한 참고 측정은 204 ms였지만,
서로 다른 3,000개 파일이나 SMB의 대용은 아니다.
사용자가 기억하는 3,000개 파일 환경의 심한 부하는 재현하지 못했다.

### 개선 시작 조건

먼저 상태 읽기·저장, 폴더 열거, 삭제 확인을 별도로 실측한다. 상태 전체 처리가
주요 비용이면 SQLite를 후보로 삼는다. 파일 경로를 키로 변경 행만 저장하며 JSON 이전과
LanceDB 저장 후 상태 복구를 설계한다. 상태 저장 개선이며 검색 벡터DB 교체가 아니다.
파일 접근이 주요 비용이면 열거 결과의 재사용을 검토하되, 접근 불가·열거 중단을
삭제로 오인하지 않는 안전 조건을 먼저 정한다.

## 3. 검색·인덱싱 개선 후보

| 후보 | 시작 조건·판단 자료 |
|---|---|
| 검색용 벡터·스칼라 인덱스 | 실제 행 수·검색 시간을 측정. 근사 검색은 재현율과 갱신 운용 검증 |
| 필요한 컬럼만 조회 | 현재 호출 위치 재확인. 과거 위반 목록을 그대로 적용하지 않음 |
| 질문 처리 비동기화 | [상세 설계](design/ISSUE_B_query_async.md)에 따라 UI 응답성과 스레드·종료 안전성 검증 |
| LLM 스트리밍 | 사내 API 지원 확인과 UI 비동기화 선행 |
| 추출·임베딩 병행 처리, 배치 조정 | 단계별 실측 후 단일 쓰기·평문 메모리 상한·취소·파일별 실패 격리 설계 |

검색은 문서 벡터 검색, 조건에 따른 경로 키워드 보강, 로컬 범위의 메일 검색을 수행한다.
항상 3회 검색하지는 않는다. 저장 규모는 원본의 ‘파일 수×용량’뿐 아니라
총 청크 수, 행 크기, fragment(저장 데이터의 작은 조각) 수로 평가한다.

기존 `[query]`·검색 결과 로그로 검색과 LLM 시간을 구분한다. 파일당 임베딩 약 20초,
DRM PowerPoint의 open 편차, COM 종료 유예 도입 후 12.2% 저하는 과거 보고이며
현재 환경의 측정값으로 취급하지 않는다. EDR·부하는 원인 후보이며 확정하지 않았다.

### 임베딩 미계측 지연 (2026-10-10)

사내 xlsx 인덱싱에서 32청크·시도 0 호출 네 건이 전체 5.81~6.44초, connect 0.00초,
ttfb 0.88~0.96초, read 0.03~0.33초, 미계측 4.82~5.52초로 보고됐다. 이전 미계측에는
응답 JSON 해석과 벡터 검증도 포함돼 이 로그만으로 원인을 확정할 수 없다. xlsx 추출과
DB 저장은 이 호출의 계측 범위 밖이다.

느린 호출 로그는 기존 connect·ttfb·read에 setup(연결 준비·취소 소켓 등록), json(응답 해석),
validate(벡터 검증)를 추가한다. 미계측은 이 여섯 구간을 전체 경과시간에서 뺀 나머지다.
ttfb에는 요청 전송과 응답 헤더 대기가 모두 포함된다. cpu는 같은 호출에서 해당 스레드가
사용한 CPU 시간이므로 구간 합계에 더하지 않는다. 경과시간에 비해 cpu가 작아도 특정
대기 원인을 단정하지 않는다([Python 문서](https://docs.python.org/3.11/library/time.html#time.thread_time)).
tracing·trace_depth는 느린 호출 기록 시점의 메모리 추적 상태이며 진단 기능을 켜거나 끄지 않는다.

로컬 합성 응답 32청크의 해석·검증은 메모리 추적 없이 약 0.03초, tracemalloc 깊이 10에서
약 0.27초로 사내 지연을 재현하지 못했다. 새 구간 로그와 실제
`collector.memory_diagnostics_enabled`와 `memory_diagnostics_mode` 설정으로 다음 확인 대상을 정한다. 본문·벡터·키는 기록하지 않는다.

### PPT 추가 기능 사내 검증 보고 (2026-10-13)

다음은 사용자가 시험 도구 결과를 전달한 보고다. 원본 로그 파일과 문서 본문을 확인하거나
보관한 결과는 아니다. 추가 기능은 호스트의 열린 문서에서 경로로 대상을 찾는 시험 DLL 0.1.3.0이다.

| 대상 | 보고된 기존/추가 기능 시간 | 결과 |
|---|---|---|
| 합성 문서 3회 | speedup 7.3, 301.18, 7.76 | 모두 pass·equal=true |
| VDI 실제 사내 문서 | 521.6초 / 5.08초, 로그 speedup 약 102 | pass·equal=true·source_unchanged=true |
| 원래 사내 PC의 문제 PPT | 약 46초 / 약 3.6초, 로그 speedup 12.86 | pass·equal=true·source_unchanged=true |
| 사내 PC의 추가 확인 | 총 3개 파일에서 유의미한 개선 보고 | 개별 추가 시간·원본 로그는 미제공 |

합성 2회차는 기존 약 1372초, 추가 기능 약 4.5초가 보고돼 기존 방식의 큰 지연이 포함됐다.
그 원인은 미확정이며 301배를 대표 개선값으로 쓰지 않는다. 표의 반올림 시간으로 로그 speedup을
다시 계산해 덮어쓰지 않는다. equal은 슬라이드 문자열·순서·건수와 오류 없는 결과를 기존 추출과
비교한 것이며 원본 화면 전체와의 대조나 두 방식의 공통 누락을 검증한 것은 아니다.

시험 중 COM 로드 충돌과 자동 로드 설정, 잘못된 Application identity 거부가 수정·재시험됐다.
최종 경로 기반 시험의 성공은 모든 PC의 설치나 대량 운영의 안정성을 증명하지 않는다.
[운영 설계](design/PPT_ADDIN_INDEXING.md)에 따라 별도 설치 도구·운영 리더·복구·로그를 구현하고
배포본에서 일반 권한 읽기, 여러 문서/사이클, 메모리·취소·Office 종료를 확인해야 한다.

### PowerPoint COM 슬라이드 읽기 (2026-10-11)

사내 로그 분석 보고에서 PPTX 추출은 전체 8201.9초 중 5578.9초(68.0%)이며, COM 단계의
read가 5507초로 보고됐다. 사용자가 같은 느린 파일을 VDI에서 측정한 결과는 전체 122.6초,
read 117.82초, 27장, 최장 슬라이드 17.79초다. 사내 PC의 같은 파일 read는 424초다.
후속 상세 계측에서 셀 읽기 구간이 주 병목으로 좁혀졌다. 실행 환경별 차이와 각 조회 안의
DRM 처리·COM 왕복·Python 래퍼 비용은 아직 분리하지 않았다.

`[ppt] 슬라이드 읽기`는 1부터 시작하는 순회 번호와 실제 방문한 도형·그룹·표·셀 개수,
최대 그룹 깊이, 오류 건수 및 구간 합계를 INFO에 기록한다. `operations`는
`구간:실행횟수/합계초`이며 구간 안의 모든 COM 속성 호출 수를 뜻하지 않는다.
`slides_next`·`shapes_next`·`group_next`는 순회 대기, `cell_text`는 표 셀 읽기,
`text`는 일반 텍스트 읽기다. 순회 횟수에는 마지막 StopIteration 확인도 포함된다.
`slide=0`은 슬라이드 목록·순회 준비, `shape=0`은 아직 특정 도형에 들어가기 전이다.

`[ppt] 도형 읽기`는 1초 이상이면 INFO, 그 외에는 DEBUG다. `shape=2.3`은 두 번째
도형의 세 번째 그룹 자식 위치이고 이름·ID·본문은 조회하거나 기록하지 않는다.
그룹 도형의 시간은 자식 시간을 포함하므로 부모·자식 시간을 더하지 않는다.
표의 행·열 개수는 기존 조회 결과를 재사용하고 셀 개수는 읽기를 시도한 셀 수다.
슬라이드별 진단 시간은 해당 슬라이드의 순회 대기부터 포함한다. 기존 최장슬라이드의
측정 경계는 유지한다. `[ppt] 읽기 구간 합계`는 슬라이드 목록 조회와 마지막 순회 확인을
포함한 파일 전체의 구간 합계를 기록한다.

`[ppt] 읽기 진행`은 한 구간이 응답하지 않으면 10초 간격으로 마지막 위치·구간·표 셀
좌표와 대기시간을 남긴다. 별도 스레드는 COM을 호출하지 않으며, 파일 읽기 종료 시
중지한다. 기존 read 단계·워치독 시작시각, RPC 단절 처리와 Office 종료 정책은 유지한다.

VDI의 기존 측정 스크립트에서 수정된 `PowerPointComReader.parse()`를 호출해 같은 파일을
비교한다. 완료된 슬라이드 요약과 느린 도형·진행 로그만 공유하고 추출 본문은 남기지 않는다.
추출 내용·표 셀·그룹 순서가 같음을 확인한 뒤 지연 구간을 기준으로 후속 최적화를 선택한다.

#### VDI 후속 실측과 일괄 읽기 조사

사용자 보고: 전체 추출 123.26초, read 118.44초, 표 셀 8,299개 읽기 91.45초
(read의 77.2%), 일반 텍스트 도형 1,682개 읽기 12.21초, 오류 0건이다.
10번 슬라이드는 표 1,550셀·17.83초, 17번은 그룹 포함 도형 913개·12.55초다.
직전 전체 122.6초와 비슷하며 계측 보강만으로 속도 향상을 확인한 것은 아니다.
오류 0건은 기존 추출 내용과 동일하다는 검증을 대신하지 않는다.

현재 셀 읽기는 `Cell(row, col).Shape.TextFrame.TextRange.Text` 전체를 한 구간으로 잰다.
8,299회는 셀 읽기 구간 횟수이며 정확한 COM 호출 수가 아니다. 셀당 평균 약 11ms에는
객체 접근·문자열 반환·Python 처리가 함께 포함되므로 DRM 비용이 없다고 결론내리지 않는다.

2026-10-11에 공식 API와 공개 구현을 확인한 결과:

| 경로 | 일괄 읽기의 의미 | 적용 조건 |
|---|---|---|
| 기존 PowerPoint COM | 문서화된 Table API는 Cell 접근을 제공하며 전체 값 배열 API는 찾지 못함 | 속성 조회 준비 비용 감소만으로 셀별 왕복을 없앨 수 없음 |
| Office.js Table.values | 표 전체 값을 string[][]로 로드. 표 목록 확보 후 여러 표의 load를 큐에 넣고 한 번 sync할 수 있음 | PowerPointApi 1.8. 확인된 사내 Office 2016에서는 미지원으로 현재 후보 제외 |
| PowerPoint 내부 추출 함수 | 외부의 한 번 요청에 내부 함수가 셀·그룹을 순회하고 문자열/배열을 한꺼번에 반환하는 설계 후보 | 승인된 추가 기능 설치·로드·스레드 처리 검증. 내부 셀 순회와 DRM 처리 비용 자체는 남음 |
| 파일/메모리 스트림 직접 파싱 | PPTX를 읽어 로컬 메모리에서 셀·그룹을 순회하며 COM을 사용하지 않음 | DRM 문서의 읽기 가능한 바이트를 승인된 경로로 확보할 수 있어야 함 |

Office.js의 [Table.values](https://learn.microsoft.com/en-us/javascript/api/powerpoint/powerpoint.table?view=powerpoint-js-1.8)는 공식 일괄 값 조회 API다.
[지원표](https://learn.microsoft.com/en-us/javascript/api/requirement-sets/powerpoint/powerpoint-api-requirement-sets)의 Windows 최소 버전은
2504·빌드 18730.20030이며 볼륨 라이선스 영구/LTSC 제품에는 1.8이 제공되지 않는다.
사내 제품은 Office 2016으로 확인됐고 VDI 제품·정확한 빌드는 미확인이다. SDK 내부의 실제 조회 비용과 DRM 호환성은 미검증이다.
사내 추가 기능은 [Office.js 로컬 참조](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/referencing-the-javascript-api-for-office-library-from-its-cdn)를 사용할 수 있지만
관련 파일과 실행 환경을 포함한 폐쇄망·숨김 문서 연동 검증이 필요하다.

내부 함수 후보는 [추가 기능의 COM 객체 노출](https://learn.microsoft.com/en-us/visualstudio/vsto/calling-code-in-vsto-add-ins-from-other-office-solutions?view=visualstudio)을 통해
외부에서 호출할 수 있다. 외부 호출을 Office 주 스레드로 전달해야 하며,
[VSTO 구조 문서](https://learn.microsoft.com/en-us/visualstudio/vsto/architecture-of-vsto-add-ins?view=visualstudio)는
자동화로 시작한 Office에서 정상 작동을 보장하지 않는다. 실제 로드·원문 미변경·사용자 문서
보호를 먼저 검증한다. 전체 문서 반환은 취소 간격·메모리 비용도 비교해 반환 단위를 정한다.
[Application.Run](https://learn.microsoft.com/en-us/office/vba/api/powerpoint.application.run)으로 별도 VBA 함수를
호출하는 시제품도 가능하지만 현재는 AutomationSecurity=3으로 매크로를 차단한다.
성능 실험을 위해 운영 보안 설정을 낮추거나 대상 문서에 코드를 주입하지 않는다.

유사 도구의 공개 구현:

- [Microsoft MarkItDown](https://github.com/microsoft/markitdown/blob/main/packages/markitdown/src/markitdown/converters/_pptx_converter.py): python-pptx로 파일 스트림을 열고 table.rows와 row.cells를 순회한다. COM 일괄 조회가 아니다.
- [Unstructured](https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/partition/pptx.py): python-pptx로 파일/스트림을 열고 셀 텍스트 행렬을 만든다. COM을 사용하지 않는다.
- [Apache POI](https://github.com/apache/poi/blob/trunk/poi/src/main/java/org/apache/poi/sl/extractor/SlideShowExtractor.java): 로드한 슬라이드 객체의 표를 행·열별로 순회하고 그룹을 재귀 처리한다.
- [Aspose.Slides](https://docs.aspose.com/slides/net/extracting-text-on-cloud-platforms-using-aspose-slides/): 파일/스트림에서 GetPresentationText를 호출해 슬라이드별 텍스트를 얻는 Office 독립 라이브러리다. 사내 DRM을 읽거나 현재 표 구조·순서를 동일하게 보존한다는 증거는 없다.
- [공개 VSTO 텍스트 추출 사례](https://note.com/mikan05/n/n9b644efd1e10): 추가 기능 안에서 표 셀을 순회해 하나의 문자열로 조합한다. 속도 수치는 없으며 예시의 클립보드 반환과 위치 정렬은 그대로 적용하지 않는다.

셀 순회 자체보다 PowerPoint 밖에서 매 셀마다 프로세스 간 요청을 반복하는지를 구분한다.
Office 2016에서는 승인된 내부 추가 기능 배포 가능 여부를 확인한다. 가능한 경로의
VDI 시제품을 기존 리더와 같은 파일로 비교해 전체·표 읽기 시간, 행·열·셀 수와 실제 문자열
동일성을 메모리에서 검증한다. 병합 셀·빈 셀·셀 내 줄바꿈·중첩 그룹도 비교한다.
내부 함수가 외부 왕복 비용을 줄일 것이라는 판단은 구조상 가설이며 아직 실측하지 않았다.
2026-10-11 당시 사내 PowerPoint 시제품·DRM 비교는 미실행이었다. 최신 사용자 보고는 위 §사내 검증 보고를 따른다.

대안 중 [getFileAsync](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/get-the-whole-document-from-an-add-in-for-powerpoint-or-word?tabs=powerpoint)는
전체 파일을 얻지만 공식 문서상 로컬 임시 복사본을 만든다. 평문 파일 비저장 계약을 충족한다고
가정하지 않는다. [형식 지원표](https://learn.microsoft.com/en-us/javascript/api/office/office.document?view=common-js)에서
PowerPoint는 Text 형식을 지원하지 않는다. 클립보드 Copy는 공유 클립보드를 변경하며
표 전체의 문자열/HTML 제공 보장과 DRM 허용 여부를 확인하지 못해 우선순위에서 제외한다.

#### 사전 검증 도구 확인 결과 (2026-10-11)

Office 없이 비교 계약·실문서 변환 차단·본문 미출력·오류 시 중단을 검증했다.
전체 회귀는 1,095건 통과·3건 건너뜀이며, 이후 추가·최종화한 PPT 회귀 24건도 통과했다.
C# 시제품은 .NET Framework 컴파일과 가짜 개체를 이용한 실제 순회 코드 검증을 통과했다.
PowerShell 등록 스크립트는 구문만 확인했다. 실행 정책으로 Build 실행이 제한돼 정책 변경 없이
컴파일러를 직접 사용했으며, 추가 기능 설치·등록 해제는 수행하지 않았다.

자체 검사 결과의 `performance_verified=false`처럼 위 결과는 성능 향상의 증거가 아니다.
Office 2016의 실제 로드·스레드 전달, 합성 PPT 변환, 사내 DRM 문서의 내용 보존·시간·종료 안전성은
미검증이다. 두 후보는 사전 비교 대상이며 운영 적용은 사내 실측 후 결정한다.

#### 다음 검증

사용자가 사내 Office 2016·인터넷 폐쇄망 조건을 확인했다. `Table.values`의 PowerPointApi 1.8은
Office 2016에서 사용할 수 없어 현재 시제품 후보에서 제외한다.

`scripts/diag_ppt_read.py`와 시험용 C# COM 추가 기능으로 기존 읽기·PPTX 변환·내부 순회를
비교한다. 실행·등록·판독은 [PPT 검증 안내](guides/PPT_PROBE.md)를 따른다. 운영 코드는 변경하지
않았다. 변환은 합성 파일만 허용하고 실제 문서는 저장 없이 기존 COM/내부 읽기와 비교한다.
추가 기능 등록은 사내 허용 후 사용자가 수행하며 자동 설치·정책 변경은 없다.
사내 Office·DRM 실측은 미실행이다. 같은 파일의 내용·표/그룹 순서·건수와 전체 시간 비교가 완료 기준이다.

추가 제안인 COM→PPTX 사본 저장→XML 읽기는 외부 셀 호출을 줄일 수 있으나
현재 대상은 이미 DRM PPTX다. 사본이 DRM으로 보호되면 직접 파싱 가능 여부를 확인해야
하며, 복호화 사본을 저장하면 CLAUDE.md의 평문 파일 비저장 계약과 충돌한다.
운영 경로에 임시 평문 저장을 도입하지 않는다. 이번에는 변환 실험을 실행하지 않았다.

## 4. Office 종료 정리 확인

`collector.com_quit_call_timeout_sec`는 Word·Excel의 `Quit()` 호출 제한(기본 10초)이며,
`com_quit_grace_sec`는 반환 후 자연 종료를 기다리는 유예(기본 5초, 0이면 생략)다.
종료 타이머는 COM 객체를 사용하지 않고 검증된 PID·생성 identity 사본만 정리한다.

- `Office Quit 반환/예외`와 `Office Quit 시간 초과 발화`로 호출 자체의 상태를 구분한다.
- `Office 정리 결과`의 `actual_exit`는 옛 대상의 종료 또는 PID 재사용 확인이다.
  `remaining`, `identity_unknown`, `failed`가 있으면 소유 기록을 보존한다.
- 핸들 열기·생성 identity 조회·종료 요청·종료 대기의 실패는 PID·EXE와 오류 코드로 기록한다.
  종료 요청 성공만으로 실제 종료를 판정하지 않는다.
- `Office 종료 미확인으로 … COM 연기`는 문서 자체 실패가 아니다. 같은 EXE는 30초 간격으로
  재정리를 시도하고, 그동안 실패 이력을 추가하지 않는다. 일반 OOXML 파싱·다른 Office는 계속된다.

타이머 정리가 진행 중이면 `callback_completed=False`로 남기며 성공 완료로 집계하지 않는다.
PowerPoint는 기존처럼 Quit·강제종료하지 않고, 자연 종료나 PID 재사용이 확인된 옛 기록만 제거한다.
WINWORD 잔존만으로 PPT까지 건너뛰었다고 단정하지 않는다. 실제 Windows·Office·DRM 환경에서
Quit 멈춤과 강제종료 실패 로그를 확인해야 하며, 강제종료가 실패하면 COM 호출 복귀는 보장되지 않는다.

### 메일 처리 중 강제 종료 후 Word 잔류 (2026-10-10)

문서 완료 시 Office 정리와 임베딩 취소를 구현하고 fake·localhost HTTP·HTTPS 회귀 검증을
완료했다. 실제 Office·DRM 검증은 사내에서 수행한다.
사용자는 PR #97 병합 버전에서 문서 처리가 끝나 메일 인덱싱 중인 상태로 트레이의 ‘종료’를
눌렀고, 앱 종료 후 작업관리자에 WINWORD가 남았다고 보고했다. 로그는 스케줄러 중지 → 수집기
취소 요청 → `[embed] 느린 호출` → 워커 종료 시간 초과 → `terminate()` → 하드 종료 순서다.

보고 당시에는 문서·메일 처리 뒤 `finally`에서 COM을 정리해 메일 처리 중에도 Word가 남을 수
있었다. 일반 종료 대기는 8초인데 임베딩 HTTP 연결·응답은 30초 대기 후 한 번 재시도할 수
있었다. 스레드 강제 중단·하드 종료는 COM 정리 완료를 보장하지 않는다. 당시
`COM 블로킹 추정`은 워커 종료 시간 초과의 공통 문구로 실제 COM 지연을 확정하지 않는다.
임베딩 대기가 취소 처리를 늦췄다는 것은 가설이며, 요청 소요시간과 잔류 PID의 소유·정리
로그를 추가 확인해야 한다.

현재 동작과 검증 기준:

1. 문서 처리가 끝나면 생성 워커에서 Word·Excel을 정리한다. 사이클 종료의 최종 정리도 유지하고
   정리 미확인 상태를 보존한다. 메일 자체는 Office 정리 실패와 무관하게 진행한다.
2. 수집 취소는 해당 워커의 활성 HTTP 소켓을 중단하며 임베딩 재시도·후속 배치를 차단한다.
   스레드별 연결로 검색 요청을 보호하고, 취소된 메일은 기존 데이터·실패 이력을 유지한다.
   임베딩·Office·DB 정리 중 종료에는 최대 60초 유예를 주며 실제 작업 단계를 기록한다.
3. fake·localhost 회귀로 문서→메일 정리 순서와 느린 임베딩 취소를 검증하고, 사내에서
   검증된 소유 Word·Excel의 실제 종료와 사용자 Office 보호를 확인한다.

이미 연결된 응답 대기는 소켓을 중단하지만 DNS·연결 수립 자체의 즉시 취소는 보장하지 않는다.
OS·HTTP 연결 제한과 종료 유예로 기다리며, 워커가 끝내 복귀하지 않으면 기존 강제 종료를 유지한다.

PID·생성 identity로 검증된 소유 프로세스만 정리하고 PowerPoint Quit·강제 종료 금지를
유지한다. 하드 종료 경로의 무조건 종료를 유지하며 그 경로에 블로킹 정리를 추가하지 않는다.

## 5. 완료된 개선과 과거 분석

현재 완료 상황은 [수정노트](UPDATE_NOTES.md)에서 확인한다. 주요 개선은
재인덱싱 시 기존 데이터 보존, 메일 후보 누락 방지, 성공 캐시, 상태 저장 집약,
fragment 수 기반 DB 정리, Office RPC 단절 복구다.

- [PERF_ANALYSIS.md](archive/PERF_ANALYSIS.md): 2026-09-06 분석 초안. 일부 근거가 반박됐으며 현행 지시가 아님.
- [PERF_ANALYSIS_REVIEW.md](archive/PERF_ANALYSIS_REVIEW.md): 교차검증·결함 발견 이력. 발견 후 수정된 항목 포함.
- [ISSUE_B_query_async.md](design/ISSUE_B_query_async.md): 질문 비동기 실행·읽기/대화/종료 보호 계약. 실제 사내 Windows·Office 장시간 검증은 남아 있음.

과거 3,000개 파일 부하와 ‘상태에 색인 추가’의 정확한 기록은 이번 문서·Git 이력 검색에서
찾지 못했다. SQLite 보류 기록만으로 같은 논의였다고 단정하지 않는다.
