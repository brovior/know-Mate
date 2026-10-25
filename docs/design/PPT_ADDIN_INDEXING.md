# PowerPoint 추가 기능의 운영 인덱싱 적용 설계

상태: **자동 설치·정상종료 후보 구현, Windows 승인 시험 전** (2026-10-15). 기본 `disabled` 빌드는
설치 상태만 점검하고, Machine 안전을 확인하면 미설치 auto는 기존 COM으로 읽는다.
개발 시 명시한 `candidate`/`released` 빌드는 앱 시작부터 UAC·설치·원래 사용자 재점검을
실행한다. 설치 모드는 빌드 출처에 내장하며 사용자 설정·환경변수·manifest 편집으로 켜지지 않는다.
`released`라는 이름도 Windows 검증 성공을 뜻하지 않으며, 최종 산출물의 승인 시험 전 배포하지 않는다.
시험 결과의 정본은 [DIAGNOSTICS.md](../DIAGNOSTICS.md#ppt-추가-기능-사내-검증-보고-2026-10-13),
시험 절차는 [PPT_PROBE.md](../guides/PPT_PROBE.md)다.

## 자동 설치 빌드의 첫 실행 흐름

1. 포터블 폴더의 `AegisDesk.exe`를 평소처럼 실행한다.
2. 앱이 PPT 읽기 기능을 확인한다. 없거나 업데이트가 필요하면 설치 도구를 자동 실행한다.
3. Windows 권한 요청에서 **예**를 누른다. 다른 관리자 계정의 인증이 필요한 PC는 그 승인이 필요하다.
4. 설치 도구가 끝나고 앱이 다시 확인한 뒤, 기존 자동 인덱싱 설정에 따라 수집을 시작한다.
   시작 요청이 대기 중이면 이어서 처리한다. 설치 성공만으로 자동 인덱싱을 새로 켜지는 않는다.

위 목표 흐름에서 사용자의 명령 입력·DLL 빌드는 필요 없다. 권한 요청을 거절하면 기존 방식으로 읽는다.
PowerPoint가 열려 있으면 설치를 미루고 기존 방식으로 읽는다. 설치 중에도 앱 화면은 사용할 수 있다.
`addin` 검증 모드에서는 준비되지 않은 PPT만 다음 사이클로 미룬다.

기존 배포 파일에는 이번 변경이 자동 반영되지 않는다. 일반 배포의 기본은 `disabled`로 유지하며,
후보·최종 파일의 검증과 빌드 명령은 [운영 검증 절차](../guides/PPT_READER.md)에 따른다.

## 1. 적용 범위와 전제

- `.ppt`와 현재 COM으로 라우팅되는 DRM/OLE2 `.pptx`에 적용한다. 일반 ZIP `.pptx`는 PlainReader를 사용한다.
- CollectorWorker의 기존 QThread, Office 점유·소유 확인, 파일 단위 실패 격리와 DB 커밋 계약을 유지한다.
- 추가 기능은 PowerPoint의 연결 콜백으로 받은 호스트에 이미 열린 문서만 읽는다.
  추출은 파일 저장·변환·새로 열기·닫기·Quit, 클립보드 사용을 하지 않는다.
  정상종료는 별도 lifecycle 계약으로만 요청한다([자동종료](#9-powerpoint-자동종료-후보)).
- 추출 코어는 시험 DLL 0.1.3.0에서 검증한 코드와 공유한다. 시험용과 운영용의 설치 식별자는 분리한다.
- 사내 보고는 소수 문서의 정확성·시간 비교다. 대량 인덱싱·사용자 Office 실행 경합·장시간 메모리 안정성은 미검증이다.

## 2. 배포·설치 계약

| 항목 | 결정 |
|---|---|
| 운영 추가 기능 | 표시 이름 `Aegis Desk PPT Reader`, ProgID `AegisDesk.PptReader` |
| DLL | `AegisDesk.PptReader.dll`, 현재 요구 버전 `1.0.5.0` |
| COM 계약 | 경로·슬라이드 번호 전달 규약 3, 추출 결과 JSON 스키마 1 |
| 식별자 | 시험용과 다른 운영 CLSID·추출 인터페이스 IID를 구현 시 고정. Office 연결 IID는 기존 규약 유지 |
| 설치 위치 | `%ProgramFiles%/AegisDesk/PptReader/<version>/`의 보호된 고정 경로 |
| 설치 범위 | HKLM Machine. Office 비트수에 해당하는 Registry32 또는 Registry64 |
| 설치 도구 | 배포본의 `PptReaderSetup.exe`: 일반 권한 점검, 설치/업데이트·제거 작업만 승격 |
| 자동 실행 | 앱 시작 시 인덱싱 전에 상태 점검, 필요한 설치/업데이트만 도구 실행 |
| 권한 | 수정 작업 때 설치 도구만 관리자 권한 요청. AegisDesk는 일반 권한 |

개발 빌드에서 C# DLL과 설치 도구를 미리 컴파일해 onedir 배포본에 포함한다. 대상 PC에서
Python·C# 컴파일러·RegAsm·인터넷 다운로드를 요구하지 않는다. Office 32/64비트에 DLL은
AnyCPU로 대응하지만 현재 보고된 결과를 32비트 호환성 검증으로 취급하지 않는다.

### 앱의 자동 준비 흐름

자동 준비는 `auto`/`addin` 모드에서 앱 시작 후 수집 워커를 시작하기 전에 실행한다.
UI 스레드를 동기 대기로 막지 않고 설치 도구의 일반 권한 `/check` 결과를 비동기로 받는다.
점검을 위해 PowerPoint를 띄우지 않는다. 앱은 HKLM·보호된 DLL 디렉터리를 직접 수정하지 않는다.

| 점검 결과 | 앱의 행동 |
|---|---|
| 배포 요구 버전이 정상 등록됨 | 설치 도구 승격 없이 인덱싱 시작. 실제 로드·규약은 읽기 때 확인 |
| 미설치·구버전·자체 설치 파일/등록 누락 | 준비 조건 확인 후 설치 도구를 자동 실행, 필요한 작업만 관리자 권한 요청 |
| PowerPoint 실행 중·비트수/상태 미확인 | 설치를 보류. 자동으로 Office를 종료하거나 비트수를 추측하지 않음 |
| 사용자가 권한 요청 거절·관리자 승인 불가·설치 실패 | setup 수정 종료·잠금 해제 확인 뒤 auto는 기존 COM, addin은 준비 대기. 종료 미확인은 PPT 준비 대기 |
| 이미 설치됐지만 Office에서 비활성화되거나 실제 로드/규약 실패 | 자동 재설치·강제 활성화하지 않고 읽기 복구와 점검 안내 |
| `com` 모드 | 자동 설치·업데이트를 요청하지 않고 기존 읽기 방식 사용 |

사용자가 UAC를 명시적으로 거절한 대상 버전만 준비 상태에 기록하고 다음 실행에서도 같은 권한 창을
반복 띄우지 않는다. 새 reader 버전이나 명시적 수동 설치 재시도 때만 다시 요청한다.
Office·mutex 경합과 다른 설치 실패는 거절로 기록하지 않으며 다음 앱 시작 때 다시 점검한다.
기존 launch_failed/check_failed/mutation_failed 억제 기록은 정상화하고 실제 permission_declined는 유지한다.
미완료 예약·저널·종료 미확인은 재시도 사유와 별도로 PPT를 계속 보류한다.
준비 기록에는 버전·상태·고정된 사유만 저장하고 문서 정보는 넣지 않는다.

설치 관찰 중에는 최초 수집 워커의 시작 전체를 보류해 앱이 새 PowerPoint를 만들지 않게 한다.
관찰이 끝나 `ppt_pending`으로 워커를 생성하면 PPT만 미루고 다른 문서·메일을 수집한다. 설치 도구의 종료 후
일반 권한으로 다시 점검하고 성공한 경우에만 새 추가 기능 준비 상태로 전환한다.
앱을 관리자 권한으로 재시작하지 않으며 설치 도구에 문서 COM 객체를 전달하지 않는다.
설치 성공 후 생성되는 워커가 서비스를 처음 조회하므로 이전 서비스 캐시를 이어받지 않는다.
등록 확인만으로 Office 내부 로드 성공을 표시하지 않는다.

일반 권한 설치 도구는 승격한 자식 작업의 종료·결과까지 기다린 뒤 앱에 결과를 반환한다.
자식 실행 요청 성공을 설치 완료로 표시하지 않는다. 앱은 비동기로 그 결과를 기다리며,
새 앱 실행도 설치 잠금이 활성인 동안 PPT 수집을 시작하지 않는다. 앱 종료나 설치 도구 오류 뒤에는
등록·파일 상태를 다시 확인하고 불완전한 설치를 준비 완료로 취급하지 않는다.

### 설치·업데이트 순서

1. 앱이 수집 시작을 보류한 상태에서 설치 도구를 자동 실행한다. PowerPoint는 종료된 상태여야 한다.
   수동 복구·업데이트·제거에서는 사용자가 AegisDesk와 PowerPoint를 먼저 종료한다.
2. Office 실행파일 위치와 PE 비트수를 확인한다. OS/Python 비트수로 Office 비트수를 추정하지 않는다.
   설치 경로나 비트수를 확정하지 못하면 중단하고 확인을 요청한다.
3. 설치/업데이트는 설치 도구가 관리자 권한을 확인하거나 해당 작업만 UAC로 재실행한다.
   일반 권한 점검은 승격하지 않으며, 설치 후 인덱싱도 기존 앱의 일반 권한에서 실행한다.
   거절하면 등록을 변경하지 않는다. Office가 실행 중이거나 상태를 확인하지 못하면 중단하며 강제 종료하지 않는다.
4. DLL·필수 의존성·어셈블리 버전을 확인하고 버전별 고정 디렉터리에 복사한다.
   임시 폴더나 이동 가능한 앱 배포 폴더를 CodeBase로 등록하지 않는다.
5. 해당 운영 키의 이전 값을 보존한 뒤 CLSID·ProgID·CLR 기본/어셈블리 버전 키를 먼저 등록하고,
   PowerPoint Addins 키와 `LoadBehavior=3`을 마지막에 기록한다. 중간 실패는 해당 키의 이전 값으로 복구한다.
   동시에 두 설치 도구가 수정하지 않게 Machine 범위의 설치 잠금을 사용한다.
6. 설치 버전·범위·Office 비트수와 등록 경로만 점검 결과로 표시한다.
   등록 성공을 PowerPoint 내부 로드·읽기 성공으로 표시하지 않는다. 실제 로드는 다음 실행에서 확인한다.

설치 도구는 자체 추가 기능 키만 수정한다. 다른 추가 기능, Office의 충돌 차단 기록,
UAC·매크로·Trust 정책을 변경하지 않는다. 시험용 등록도 일괄 삭제하지 않는다.
이전 버전 DLL은 최초 구현에서 자동 삭제하지 않으며, 사용 중 파일을 덮어쓰지 않는다.

제거도 PowerPoint를 종료한 상태에서 같은 범위·비트수의 운영 키만 해제하고 파일을 정리한다.
업데이트 실패는 이전 등록으로 돌아가며, 새 DLL이 시작 시 Office를 충돌시키는 경우는 사용자가
설치 도구로 운영 추가 기능을 제거하거나 검증된 이전 버전으로 되돌려야 한다.
`ppt_reader_mode=com`만으로 Office 시작 시 추가 기능의 자동 로드를 막을 수는 없다.

### 설치 점검이 필요한 항목

.NET Framework 가용성, 회사의 실행·추가 기능 허용 정책, 다른 사용자 세션의 PowerPoint,
Machine 설치·업데이트 권한은 배포 시험에서 확인한다. 현재 두 환경의 성공을 모든 PC에서의
무인 설치 성공으로 해석하지 않는다. 관리자 권한이 없는 PC에서도 기본 auto는 기존 COM을 사용한다.

## 3. 설정과 코드 연결

새 기본 설정은 배포 템플릿 `knowmate/config.yaml`에만 둔다.

```yaml
collector:
  ppt_reader_mode: auto
```

| 값 | 동작 |
|---|---|
| `auto` | 호환되는 운영 추가 기능 우선. 안전한 실패에만 기존 COM으로 복구 |
| `com` | 기존 읽기 방식만 사용하는 복구 설정 |
| `addin` | 추가 기능만 사용하는 검증 설정. 기존 COM으로 조용히 복구하지 않음 |

기존 사용자 config에 키가 없으면 auto를 적용하고, 잘못된 값은 경고 후 com을 사용한다.
설정은 `main.py → get_extractor → AutoReader → ComReader → PowerPointComReader`로 주입한다.
secure 모듈이 전역 설정을 직접 조회하지 않는다. 초기 구현은 설정 UI 항목을 추가하지 않는다.

- `secure/ppt_addin_reader.py`: 운영 ProgID 조회, 로드/호출 규약/운영 DLL 버전 확인, 슬라이드 추출.
- `secure/ppt_payload.py`: 응답 크기·스키마·위치·건수·오류·시간 검증과 표 문자열 변환.
- `PowerPointComReader`: 기존 파일 open/close·StageTimer·소유 확인을 담당하고 읽기 방식을 선택한다.
- 시험 도구의 decoder는 같은 순수 검증 함수를 사용한다. 운영 코드가 scripts 패키지를 import하지 않는다.
- 시험용과 운영용 C#은 동일 추출 코어를 공유하되 ProgID/CLSID·어셈블리와 연결 외피를 구분한다.
  운영 외피는 `ProtocolVersion()`과 `ReaderVersion()`으로 실제 서비스의 호환성을 확인한다.

COM 앱과 서비스 참조는 생성한 워커에서만 사용한다. 서비스와 준비 상태는 앱 참조 수명에 묶어
보관하고 참조 해제·재연결·poison 시 함께 무효화한다. PID가 같아도 이전 서비스 참조를 재사용하지 않는다.
캐시에 문서 COM 객체·원문·슬라이드 JSON을 보관하지 않는다. 수동 설치 변경 후에는 앱/Office를 재시작한다. 자동 준비는 첫 워커 생성 전에 끝내므로 앱 재시작 없이 진행한다.

## 4. 파일 한 건의 읽기 계약

1. 기존 Office 가드를 통과하고 `_get_ppt_app()`으로 확인된 앱을 사용한다.
2. auto/addin에서 서비스가 연결되어 있는지와 계약·운영 버전을 확인한다.
   `Connect=True`로 강제 활성화하거나 실행 중 등록하지 않는다.
3. 원본을 기존처럼 `ReadOnly=True, WithWindow=False`로 한 번 연다.
4. 추가 기능에 절대 경로와 슬라이드 번호를 전달한다. 추가 기능은 호스트의 Presentations에서
   정규화 경로가 일치하는 대상이 정확히 하나일 때만 그 문서를 읽는다.
5. 슬라이드별 응답을 검증해 문자열로 합친다. 운영에서 기존 방식과 이중 비교하지 않는다.
   응답 JSON은 슬라이드 처리 후 놓고, 부분 결과는 검증에 실패하면 전부 버린다.
6. Python이 자신이 연 문서만 닫는다. 닫기 확인에 실패한 추출을 성공 커밋하지 않는다.

COM 폴백에서 계속 처리한 도형 읽기 오류도 합산해 확인한다. 폴백 결과가 부분 추출이면
성공으로 커밋하지 않고 기존 인덱스 세대를 보존한 채 파일 실패로 기록한다.

표의 공백·줄바꿈은 기존 `format_table`을 공유하고, 도형·그룹·표·셀 및 슬라이드 순서를 보존한다.
**빈 슬라이드를 최종 문자열에서 생략하는 기존 운영 리더의 동작**도 유지한다.
시험 도구 Snapshot.text의 빈 슬라이드 구분자를 그대로 운영 반환값으로 쓰지 않는다.
텍스트 계약을 유지하는 변경이므로 UID·청크 ID·임베딩·인덱스 버전을 변경하지 않으며 전체 재인덱싱을 강제하지 않는다.

## 5. 실패·복구와 워치독

| 상태 | auto | addin |
|---|---|---|
| 미설치·비활성·규약/운영 버전 불일치 | 기존 COM 선택, 사유 표시 | 파일 손상으로 분류하지 않고 PPT 대상을 준비 대기로 연기 |
| 일반 추출 실패·호스트 대상 없음/중복·잘못된 JSON·내부 읽기 오류 건수 > 0 | 부분 결과 폐기, 이미 열린 같은 문서에서 기존 COM을 처음부터 1회 시도 | 파일 실패 처리, 부분 결과 커밋 금지 |
| 기존 COM 재시도도 실패 | 기존 파일 단위 실패 분류·백오프 | 해당 없음 |
| RPC 단절·프로세스 종료 | poison·복구 경로, 같은 파일 즉시 재시도 금지 | 동일 |
| 워치독 발화·취소 요청·문서 닫기 실패 | 기존 COM으로 재시도하지 않음, 실패/취소 경로 | 동일 |

HRESULT는 바깥 DISP_E_EXCEPTION뿐 아니라 안전하게 추출한 EXCEPINFO의 내부 scode와
추가 기능의 원래 코드까지 확인한다. 포장된 RPC 단절을 일반 오류로 오인해 폴백하지 않는다.
재시도 전 파일 크기·mtime 변경, 취소·워치독 발화 여부를 확인한다. 변경된 파일은 재시도하지 않고
기존 변경 감지 경로로 넘긴다. 원래 추가 기능 실패 사유와 최종 결과를 각각 보존한다.

dispatch/open/read/close 단계와 read 시작시각을 유지한다. 폴백이나 슬라이드 전환으로 파일의
워치독을 해제·재무장하거나 제한 시간을 늘리지 않는다. Word·Excel 종료 워치독에 PPT를 추가하지 않는다.
현재 PowerPoint MultiUse 보호로 Quit·강제 종료하지 않으므로 COM hang의 즉시 해제는 보장되지 않는다.
이 설계는 읽기 비용 개선이며 그 제한의 해결로 표시하지 않는다.

## 6. 기록·복구 안내

서비스 준비 상태는 앱 참조당 1회, 파일별로 선택 방식(addin/com)·폴백 사유·운영 버전·규약·슬라이드 수·
문자 수·open/read/close 시간과 결과를 남긴다. 콜백 안에서는 임의 예외 문구나 본문을 출력하지 않는다.
기존 상세 진단의 실패 단계·오류 종류·행/열·코드와 슬라이드당 16건 상한을 유지한다.
설치 도구와 앱 모두 본문·JSON·클립보드·평문 파일을 진단 자료로 만들지 않는다.

파일별 폴백 실패는 기존 실패 화면·백오프에 연결한다. 설치 준비 문제는 전역 준비 상태로 기록하여
모든 PPT를 손상 문서로 쌓지 않는다. 최초 미설치·업데이트는 앱의 자동 준비 흐름으로 처리하고,
사용자는 Windows 권한 요청만 승인한다. 수동 설치 도구는 거절 후 재시도·복구·제거용으로 유지한다.

## 7. 구현 순서와 통과 기준

1. **공유 코어·어댑터**: 실제 COM 없이 응답 검증·순서·빈 슬라이드·표 변환·오류 비노출을 확인한다.
   C# 하네스로 호스트 문서 선택, 대상 없음/중복, 스레드·연결 보호를 검증한다.
2. **운영 리더 연결**: auto/com/addin 분기, 일반 실패의 1회 폴백, 부분 결과 폐기,
   RPC 내부 코드·취소·닫기 실패·시간 초과의 재시도 금지와 워치독 수명 보존을 검증한다.
3. **설치·빌드**: DLL 누락 빌드를 실패 처리하고 버전·빌드 출처를 기록한다. 실제 Windows에서
   자동 점검/새 설치/업데이트/제거, 권한 거절·거절 기록, Office 실행 중 보류, 중간 실패 복구,
   32/64비트와 등록 범위를 검증한다. 일반 권한 앱의 설치 후 재점검·수집 시작, UI 비차단,
   중복 실행 방지와 `com` 모드의 승격 없음, 기존 비활성 추가 기능의 자동 재활성화 금지도 검증한다.
4. **사내 승인 시험**: 기존 검증 PPT들을 운영 경로에서 다시 읽어 최종 텍스트·청크 결과를 비교한다.
   신규 또는 선택한 재인덱싱 대상만 사용하며 AppData/인덱스 전체를 지우지 않는다.
   일반 PPTX·다른 Office 문서·미설치 PC·사용자 PowerPoint 점유·앱 종료·재시작도 확인한다.
5. **연속 인덱싱**: 여러 문서/사이클의 addin 사용·폴백률·읽기 시간·실패·메모리·Office 잔류를 관찰한다.
   fake 회귀와 실제 설치/읽기 시험을 모두 통과한 뒤 베타 배포한다.

코드 단계에서는 UPDATE_NOTES를 갱신하고 관련 테스트와 저장소 전체 검사를 수행한다.
Windows/Office에서 실행하지 못한 항목과 기존 실패는 따로 보고한다. 실제 Windows 설치·읽기 시험 전까지 이 구현을
운영 배포 완료나 자동 설치 검증 완료로 표시하지 않는다.

## 8. 개발 작업 상세

### 8-1. 현재 연결

`main`의 최초 워커 생성과 bridge·트레이·유휴 시작 요청은 `ppt_preparation`의 동일 게이트를 통과한다.
`secure/ppt_installation.py`는 설치 점검·실행·Machine 잠금을, `ppt_policy.py`는 내장 모드와 요구 버전을 담당한다.
모드·준비 정책은 extractor와 COM 리더로 주입하며 서비스 캐시는 `_tls.ppt` 수명에 연결한다.
`ppt_payload`의 순수 decoder와 `scripts/ppt_shared/PptExtractionCore.cs`를 운영·시험 경로가 공유한다.
운영은 추출 오류와 Close 실패를 성공으로 반환하지 않으며 시험 등록 도구를 앱에서 호출하지 않는다.

설정 기본값은 `knowmate/config.yaml`에서 읽는 작은 기본값 함수로 기존 사용자 설정에 보충한다.
지금 config 로더는 기존 사용자 파일 전체를 템플릿과 병합하지 않으므로 `.get(..., "auto")`를
여러 호출부에 반복하는 것으로 기본값 주입을 대신하지 않는다. fake/plain·비Windows는 설치 점검을 생략한다.

### 8-2. 시작 게이트와 준비 상태

앱의 상태는 `checking → installing → rechecking → ready/com_only/ppt_pending`으로 둔다.
`com` 모드는 자동 설치를 요청하지 않지만 Machine 준비 예약·수정·저널의 안전 확인은 생략하지 않는다.
아래 표를 상태 전이의 정본으로 사용한다. 미확인·실패를 `ready`로 바꾸지 않는다.

| 확인된 상태 | auto | addin | com |
|---|---|---|---|
| 요구 등록 정상 + 준비 예약/수정 잠금/저널 안전 | ready | ready | com_only |
| 수정 미실행이 확정된 거절·읽기 전용 점검 오류·정책 실패 + Machine 안전 확인 | com_only | ppt_pending | com_only |
| 설치 실패 후 실제 자식 종료·예약 해제·저널 복구·잠금 안전 확인 | com_only | ppt_pending | com_only |
| disabled/conflict + Machine 안전 확인 | com_only, 자동 수정 금지 | ppt_pending, 자동 수정 금지 | com_only |
| 설치 중·부모/자식 종료 미확인·잠금 접근 불가·예약 unresolved·미완료 저널 | ppt_pending | ppt_pending | ppt_pending |

읽기 전용 `/check`의 출력 오류는 수정 실행을 뜻하지 않는다. 그러나 다른 준비 요청의 안전까지
확인하지 못했다면 com_only로 돌아가지 않는다. `/prepare` 종료 미확인은 출력 오류와 별개 상태다.
상태 변경은 Qt 주 스레드에서 하고, setup은 QProcess 또는 동등한 비동기 process handle로 감시한다.
수집기 QThread 안에서 UAC나 등록을 요청하지 않는다.

- 첫 자동 점검은 단일 인스턴스 선점·config 읽기 후, 첫 worker 생성 전에 한다.
- bridge의 두 시작 경로, 트레이, 유휴 시작을 coordinator에 위임한다. 설치 대기 중
  실패 기록 조작·작업 예약과 worker 시작은 구분하고 대기 요청을 중복 실행하지 않는다.
- 표에서 안전이 확인된 `auto` 준비 실패만 워커에 `com_only` 정책을 전달해 서비스 조회 반복을 피한다.
  `addin`은 COM 대상 PPT를 파일 실패·백오프가 아닌 준비 대기로 분류한다.
  plain PPTX·Word·Excel·메일은 일반 사이클에서 계속 처리한다.
- 앱 종료 시 coordinator는 완료 콜백·수집 시작을 취소한다. 이미 승격된 설치 프로세스를
  강제 종료하거나 설치 도중 COM 수집을 시작하지 않는다. 다음 실행은 재점검한다.
- 사용자별 단일 인스턴스 락은 Machine 설치 잠금을 대체하지 못한다. 다른 세션/사용자의 앱도 고려한다.

거절 기록은 원래 일반 권한 사용자의 `%APPDATA%/AegisDesk/ppt_reader_preparation.json`에
요구 버전·고정 사유만 원자적으로 저장한다. 새 버전은 한 번 재요청할 수 있다.
파일이 깨졌으면 같은 실행에서 자동 재요청하지 않고 수동 재시도를 안내한다.
설치 시도는 앱 실행당 최대 한 번이다. Office·mutex 점유와 11 이외 실패는 다음 실행에서 재점검하며
영구 억제하지 않는다. 설치 실패가 남긴 예약·저널은 안전 확인 또는 명시 복구 전까지 보류한다.
수집 재시도 버튼은 UAC 거절 기록을 지우지 않는다. 명시 수동 재시도는 수집을 종료한 뒤 검증 절차에 따른다.

### 8-3. 설치 도구의 입출력과 승격

`PptReaderSetup.exe`는 일반 권한 `/check`, `/prepare`, 수동 `/remove`, 내부 승격 작업을 제공한다.
`/check`는 Office 실행 없이 읽기 전용이고, 다음 구조의 본문 없는 JSON 한 건만 stdout으로 반환한다.

```json
{"schema":1,"operation":"check","status":"ready","reason":"registration_ok","required_version":"1.0.4.0","installed_version":"1.0.4.0","office_bitness":64,"scope":"Machine","machine_safe":true,"auto_install_mode":"candidate","auto_install_validated":false}
```

상태는 `ready/missing/outdated/repairable/disabled/conflict/busy/unknown/failed`를 구분하고,
Machine 예약·저널의 안전 확인 여부를 별도 필드로 반환한다.
앱은 구조·허용값·exit code·작업 요청의 일치를 검증한다. 실패 JSON에는 고정 문자열 `stage/error_type`과 실제 Win32 오류 또는 null인 `winerror`를 묶어 선택적으로 추가한다. 원문 예외·SID·SDDL은 출력하지 않는다. 출력 없음·깨진 출력·시간초과는 실패다.
요청의 native 호출 후 실패는 기존 진단 세 필드와 함께 `native_stage/ntstatus/io_status` 세 필드를
모두 추가한다. `native_stage`는 `commit_return/commit_verify/commit_close`만 허용하며,
상태는 8자리 대문자 16진수이고 미수집 `io_status`는 null이다. 이 묶음은 `stage=request_commit`,
`machine_safe=false`, 비정상 종료의 `failed/busy`에만 허용한다. 호출 자체의 NT 실패는
`IOException`·`winerror=null`로 남기고, 호출 성공 뒤 검증·닫기 실패는 실제 오류 종류와 Win32
코드를 유지한다. schema 1과 기존 JSON 형식도 계속 읽으며 부분 묶음·잘못된 값은 거절한다.
설치 요청의 종료 코드는 `0=작업 완료`, `10=Office/설치 점유`, `11=권한 거절`,
`12=환경/정책 미확인`, `13=수정 실패`로 고정하고 schema 버전에 포함한다.
`0`만으로 ready가 되지 않으며 원래 사용자 권한의 `/check`를 다시 수행한다.
`auto_install_mode`는 앱·도구·manifest와 일치해야 한다. candidate도 자동 실행되지만
`auto_install_validated`는 released 모드에서만 true이며 외부 승인 시험의 통과 증거가 아니다.
명시 거절 11을 확인한 뒤 부모 재잠금이 실패하면 거절 증거를 유지하고 Machine 안전은 별도로 미확인 처리한다.

`/prepare` 일반 프로세스는 필요한 경우 `ShellExecuteEx(runas)`로 같은 도구의 작업 모드만
승격하고 `SEE_MASK_NOCLOSEPROCESS`로 얻은 실제 자식 handle을 기다린다.
실행 요청 성공만으로 완료를 판정하지 않는다. handle을 받지 못하면 종료 미확인으로 PPT 수집을 보류한다.
UAC 취소와 실행 실패는 구분한다.
중간 파일·사용자 AppData에 관리자 결과를 써서 전달하지 않고 자식 exit code 후 재점검으로 판정한다.
다른 관리자 계정으로 승인해도 HKLM과 OS의 ProgramFiles만 수정하고 그 관리자의 HKCU/AppData는 쓰지 않는다.
등록할 Office 정보·경로를 사용자 프로세스가 준 값만 믿지 않고 승격 작업이 다시 확인한다.

UAC 대기·설치의 관찰 제한 시간이 지나면 Qt coordinator만 대기를 분리하고 `ppt_pending`으로 둔다.
helper·승격 자식을 kill/terminate하지 않는다. 다른 문서·메일은 계속 가능하다.
`/check`는 짧은 읽기 전용 QProcess로 실행할 수 있지만 `/prepare`는 QProcess 소유 수명에 묶지 않는다.
일반 helper를 Windows CreateProcess의 독립 process로 실행하고 앱은 별도 handle로 관찰한다.
앱 종료 시 관찰 handle과 자기 stdout 임시 파일 handle만 닫으며 helper/자식을 kill-on-close job에 넣지 않는다. stdout은 PIPE가 아닌 부모 생성 임시 파일 handle을 상속한다. Windows에서는 현재 사용자/Admin/System의 protected DACL을 생성 시 적용하고 CREATE_NEW·공유 없음·DELETE_ON_CLOSE로 열며, helper는 새 경로 open 대신 복제된 handle을 상속한다. 실제 helper 종료 뒤에만 최대 16KiB+1을 읽어 검증하며, 관찰 종료는 상속 handle의 쓰기를 중단하지 않는다. 읽기·정리 실패도 독립 `/check`를 생략할 근거가 아니다.
일반 helper는 UAC 요청 전부터 자식의 실제 종료·재점검까지 계속 살아서 준비 예약을 보유한다.
부모 helper의 종료·관찰 timeout·QProcess finished를 승격 자식 종료와 동일시하지 않는다.

설치 대상은 도구가 정한 고정 디렉터리와 자체 키뿐이다. 임의 source/destination/registry path를
승격 옵션으로 받지 않는다. helper 옆의 배포 manifest와 payload를 확인한 뒤 복사한 파일도 재검증한다.
manifest의 해시는 파일 일치 확인이며 배포자의 신뢰를 보장하는 서명으로 설명하지 않는다.
설치된 파일·저널은 일반 사용자가 바꿀 수 없는 ACL을 적용하고 링크/재분석 지점 경유를 거부한다.
사내 서명·허용 정책은 Windows 배포 시험의 별도 확인 항목이다.

### 8-4. Machine 잠금·점검·복구

**준비 예약**은 UAC 요청 전부터 자식의 실제 종료 확인까지, **수정/읽기 mutex**는 실제
수정 또는 PPT COM 호출 구간에만 유지한다. mutex가 없다는 사실로 과거 준비 요청이 끝났다고 판단하지 않는다.

구현 후보는 모든 세션에서 열람 가능한 `%ProgramData%/AegisDesk/PptReaderRequests/`의
요청별 작은 durable 예약 파일과 global named mutex다. 요청은 UUID·요구 버전·원래 사용자 SID·
helper PID/생성시각·자식 PID/생성시각(확인 전 null)·고정 상태를 기록한다. 문서/본문은 기록하지 않는다.
이 예약은 동기화/보류 판단만 하며 관리자 작업을 승인하는 근거로 사용하지 않는다.

일반 helper는 mutex 아래 다른 미완료 예약/저널을 확인하고 자기 예약을 원자적으로 저장한 뒤
mutex를 놓고 UAC를 요청한다. 승격 자식은 실제 작업 전 같은 UUID의 예약과 프로세스 identity를
확인하고 PID/생성시각을 기록하며, 수정 mutex를 획득한다. helper는 실제 자식 handle의 종료 후
Machine 상태를 재점검해 안전한 완료 상태를 기록한다. handle을 얻지 못하거나 helper가 죽으면
예약을 지우지 않고 unresolved로 판정한다. 거절도 수정 미실행이 확정된 뒤 완료로 기록한다.

예약 디렉터리는 Users 읽기/열거·새 요청 생성, Creator Owner 자기 요청 갱신,
Administrators/System 관리 권한으로 제한한다. 디렉터리 owner/Admin/System을 신뢰하는 기존 모델에서
비소유 일반 사용자의 타인 예약 갱신·삭제를 제한한다. owner의 암묵적 WRITE_DAC는 남아 있으므로
적대적인 owner로부터의 격리는 보장하지 않는다. 이 격리가 필요하면 관리자 소유 저장 구조 등으로
재설계하기 전 활성화하지 않는다.

기존 `%ProgramData%/AegisDesk`는 상속 차단 여부만으로 거절하지 않는다. 유효 account owner 또는
Admin/System, canonical·비null DACL과 해석 가능한 ACE를 요구한다. 부모에 적용되는 Allow에서
owner/Admin/System 외 주체의 파일 생성·attributes/EA 쓰기·삭제/DELETE_CHILD·DACL/owner 변경은
거절하고 읽기·실행·하위 디렉터리 생성은 허용한다. deny 상쇄를 추정하지 않는다.
`PptReaderRequests`는 기존 protected·canonical·정확한 SID/권한/상속 규칙을 유지한다.
비호환·빈 기존 폴더를 자동 인수하거나 재ACL하지 않는다. 예외는 §8-7의 정확히 확인한 구버전
부모 DACL 복구뿐이다. 신규 폴더는 생성 시 ACL을 원자 적용하며,
ProgramFiles 설치 보호의 기존 엄격한 판정은 유지한다.

일반 helper와 승격 자식은 OS CommonApplicationData의 로컬 NTFS 고정 경로를 부모부터 열어
`RequestDirectoryLease`를 유지한다. reparse를 거절하고 같은 handle의 파일 ID·최종 경로·owner/DACL을
확인한다. READ만 공유하는 handle은 namespace 교체를 제한하며 WRITE_DAC·모든 attributes/EA를
동결하지 않는다. 민감 작업 전 재확인도 마지막 검사 이후의 owner 권한 변경까지 막는 보장은 아니다.
helper는 예약 전부터 UAC·실제 자식 종료·완료 저장까지 lease를 보유하고, 자식은 Load/EnterChild/수정과
복구를 자기 lease 안에서 수행한다. 충돌하는 기존 handle은 대기 대신 보류한다. 실제 Windows에서
lease 공존·원자 저장·같은/다른 관리자 UAC를 확인하기 전 설치 성공으로 간주하지 않는다.
일반권한 최초 생성·ACL 설정이 해당 Windows/회사 정책에서 가능한지 **미검증**이다.
필요 권한을 확보하지 못하면 자동 prepare를 시작하지 않는다. 수정 미실행과 Machine 안전이
확인되면 auto는 기존 COM, 안전 확인이 불가능하면 PPT 준비 대기로 처리한다.
사용자에게 별도 관리자 초기화를 정상 선행 절차로 요구하지 않는다. **앱 실행과 UAC 승인 외의
수동 작업 없이 첫 설치가 가능한 Windows prototype gate를 통과해야 자동 설치를 배포한다.**
이 후보가 gate를 통과하지 못하면 다른 자동 동기화 메커니즘을 설계·검증해야 하며,
수동 초기화에 성공한 결과를 자동 설치 완료로 보고하지 않는다.

준비 요청은 `RequestWriter`의 명시적 CreateOnly/ReplaceExisting으로 저장한다. lease의 검증된
자식 아래 정규 UUID 이름만 사용하며 schema1·8192byte 상한·불변 필드·상태 전이를 검증한다.
같은 폴더의 CREATE_NEW 임시파일에 owner와 protected/canonical DACL을 생성 시 적용하고,
같은 source handle로 기록·Flush(true)·내용/ID/보안 검증·native rename을 수행한다.
`NtSetInformationFile(FileRenameInformation=10)`은 RootDirectory=NULL·단순 basename만 받는다.
기존 대상의 검사 handle은 모두 닫고 lease를 재확인한 뒤 교체한다. 외부 reader 때문에 실패하면
기존 대상·임시 증거를 보존하며 공유/ACL 완화·POSIX 플래그·delete+move로 우회하지 않는다.
ProgramFiles 설치 기록·저널의 `Contract.AtomicWrite`는 기존 managed 저장을 유지한다.

요청 file owner는 일반 helper의 SID, 승격 시 Administrators, System 실행 시 System으로 제한한다.
해당 token이 정상 설정할 수 있는 경우만 허용하고 새 특권을 켜지 않는다. helper의 FullControl,
Admin/System FullControl·Users Read는 계속 유지한다. 디렉터리 owner/DACL은 변경하지 않는다.
검사 후 대상 handle을 닫는 구간은 기존 mutex/ACL/lease 신뢰 경계로 보호하며 hostile owner나
동일 helper 계정 내 비협조 프로세스까지 원자 CAS로 격리한다고 주장하지 않는다.

정상 완료는 동기 native SUCCESS와 유효한 IO 상태·최종 source 검증·정리를 함께 확인한다.
계약 밖 미완료 반환은 buffer/source/lease/mutex를 보존한 채 더 진행하지 않고 앱 관찰만 분리한다.
뒤늦은 file-handle signal을 성공으로 바꾸거나 helper를 종료하지 않는다. 실제 발생 시 후보 활성화를 중단한다.
기존 미완료 예약/저널/임시파일은 자동 정리하지 않으며 orphan-only 임시파일의 관리 복구는 별도 과제다.

새 앱과 다른 사용자/세션의 `/check`는 전체 예약을 읽고 helper/자식 PID와 생성시각을 확인한다.
PID 재사용·identity/프로세스 조회 불가·기록 파손은 unresolved다. helper 부재만으로 예약을 제거하지 않는다.
재부팅·오래된 예약도 시간 경과로 자동 만료하지 않는다. 실제 프로세스 종료와 Machine 저널/등록 안전을
확인한 재점검 또는 관리자 수동 복구만 완료 처리한다. 어느 세션의 UAC/자식 실행 가능성도
확인할 수 없으면 PPT를 보류한다. 앱이 직접 타인의 예약/관리자 저널을 수정하지 않는다.

새 global named mutex 한 개로 설치 수정과 PPT COM 진입을 직렬화한다.
일반 워커는 PPT dispatch 전 획득해서 read/close 완료까지 보유하고 setup은 전체 수정 구간에서 보유한다.
워커는 mutex 획득 뒤 미완료 예약/저널을 재확인하며 하나라도 있으면 COM 진입을 보류한다.
자식은 자기 요청 외의 미완료 예약이 있으면 수정하지 않는다.
잠금 이름·접근권은 일반 사용자 동기화와 다른 관리자 UAC 모두 가능한 Windows 하네스로 검증한다.
생성·접근 거부는 설치/읽기 점유로 보류하며 잠금 없음으로 추정하지 않는다.
포기된 mutex를 얻으면 설치 저널·등록 상태를 확인한 뒤에만 읽기/수정을 허용한다.

setup은 락 획득 전후 PowerPoint 프로세스를 모든 확인 가능한 세션에서 점검한다.
조회 접근 거부·비트수 미확인은 설치 보류다. 사용자 PowerPoint가 검사 직후 실행하는 경합은
앱의 락만으로 막을 수 없으므로 수정 직전 다시 점검하고 버전별 새 파일을 사용한다.
Windows 시험에서 실행 경합의 안전성을 확인하지 못하면 자동 업데이트 배포를 보류한다.

읽기 전용 점검은 Office App Paths/설치 정보에서 실제 POWERPNT.EXE 후보를 찾고 PE 비트수로 확정한다.
후보가 충돌·누락되면 추측하지 않는다. Machine 등록은 OS가 제공하는 해당 ProgramFiles 경로를
사용하며 Office 32비트라도 Python 비트수에 의존하지 않는다.
HKCU와 HKLM의 운영 CLSID·ProgID·Addins를 Office와 같은 레지스트리 뷰에서 읽는다.
HKCU shadow/다른 경로·버전·미소유 키는 `conflict`, 기존 Addins의 LoadBehavior가 비활성이면
`disabled`다. 자동 repair로 `3`을 덮어쓰지 않는다. Office Resiliency를 지우거나 강제 활성화하지 않는다.
시험용 키는 설치·점검 충돌 대상으로 무단 수정하지 않으며 운영과 별개로 남긴다.

수정 전에 보호된 Machine 저널에 자체 키의 존재 여부·값 종류·값·현재 버전을 보존한다.
DLL 복사 검증 → CLR·CLSID·ProgID 등록 → Addins 노출 → 재점검 → 저널 완료 순서다.
실패·다음 실행의 미완료 저널은 락 아래 이전 상태로 복구하고 복구 실패면 준비 완료로 취급하지 않는다.
새 키만 삭제하고 기존 키는 값 종류까지 복원한다. 다른 키는 건드리지 않는다.
이전 DLL은 최초 구현에서 자동 삭제하지 않는다. 검증된 제거/정리 작업만 자체 버전 폴더를 정리한다.
동일 버전 경로의 파일 해시가 다르면 덮어쓰지 않고 재빌드 버전 갱신 또는 수동 복구를 요구한다.
제거는 설치 기록의 scope/bitness/고정 경로를 확인하고 Machine의 운영 키만 처리한다.
HKCU 충돌이나 시험용 등록을 함께 제거하지 않는다. 설치 기록 불명확 시 삭제를 중단한다.

### 8-5. 운영 읽기 성공 조건

운영 서비스는 `ProtocolVersion()=3`, `ReaderVersion()=1.0.5.0`, JSON schema 1을 최초에 확인한다.
첫 릴리스는 요구 운영 버전과 정확히 일치할 때만 선택한다. 향후 호환 범위를 넓히는 것은 별도 변경이다.
경로는 원본을 연 Python의 절대 경로와 host Presentations의 FullName을 비교한다.
UNC·대소문자·중복·상대경로는 기존 하네스 계약을 유지하고 경로 alias는 추측해서 동일시하지 않는다.

응답 검증은 시험 decoder의 슬라이드당 32Mi 문자 상한을 유지하며 bool을 숫자로 받지 않는다.
counts·nodes·셀 문자열·finite 시간·schema·요청 slide·오류 위치를 검사하고
`counts.errors>0`이면 운영 성공 결과를 반환하지 않는다. 파일 전체 원문은 기존 문자열 계약에 따라
메모리에 조합하되 COM 객체/JSON/응답 문자열을 서비스 캐시에 누적하지 않는다.

외부 HRESULT, pywin32 EXCEPINFO scode, 허용된 추가기능 오류 코드·내부 HRESULT를 순수 함수로
분류한다. RPC 치명 코드가 하나라도 있으면 fatal을 우선한다. 임의 COM description을 로그로 찍지 않는다.
기존 `_poison_if_fatal`은 PPT 경로에서 이 분류를 사용하고 실패 코드 테스트를 먼저 추가한다.

폴백은 같은 열린 문서에서 1회이며 결과 버퍼·addin 오류를 분리한다.
현재 ComWatchdog는 실제 종료 `killed>0`일 때만 fired/stage를 남기고 PowerPoint는
`terminable=False`라 종료되지 않는다. 따라서 PPT timeout 판정은 fired나 강제종료 여부에 의존하지 않는다.

scheduler는 파일의 실제 COM 진입에서 기존 `_com_timeout_for_size`로 제한 시간을 산정해
`time.monotonic()+timeout`의 파일별 deadline을 한 번 생성하고 secure에 읽기 전용 확인 callback을 주입한다.
`com_timeout_base_sec<=0`이면 기존 timeout 비활성화를 존중해 deadline을 None으로 둔다.
슬라이드 전환·addin 폴백·close로 deadline을 재생성하거나 제한 시간을 늘리지 않는다.

callback은 deadline 도달·취소·외부 fired·원본 size/mtime 변경을 read 시작, 각 슬라이드 호출 전후,
일반 오류의 폴백 결정 전, close 후 최종 반환 전에 확인한다.
닫기 완료·추출 반환 시점의 monotonic end와 성공/expired 판정을 파일별 불변 결과로 확정하고
워치독을 정리한다. 결과는 파일 세대 토큰·COM 시작/종료시각·deadline·expired·close 성공을
담는 frozen 기록으로 scheduler 컨텍스트에 보관한다. 별도 종료 훅으로 secure에서 확정하며
기존 `extract(path)->str` 반환 계약은 유지한다. 결과 누락·세대 불일치·close 미확인은 성공이 아니다. scheduler는 추출 반환 직후 이 확정 결과를 확인하며 COM deadline을 다음
임베딩 구간으로 연장하지 않는다. COM 호출이 늦게 정상 반환하거나 일반 오류를 반환해도
COM 종료 시 deadline을 넘었으면 폴백·성공 커밋을 금지한다.
임베딩 후 DB 커밋 직전에는 확정된 COM 성공/expired 판정과 현재 취소·원본 변경만 확인한다.
그때 `time.monotonic()>옛 COM deadline`으로 정상 임베딩을 거부하지 않는다.
시간초과 뒤에도 기존 안전한 close 처리만 수행하며 Office 강제 종료를 새로 도입하지 않는다.
secure가 scheduler를 import하지 않는다. callback은 파일 단위 수명이며 다음 파일에 넘기지 않는다.
이 설계는 늦게 돌아온 결과의 사용을 막지만 영원히 멈춘 PPT COM 호출을 해제하지 못한다.

PPT deadline 초과는 별도의 '강제 종료 없이 시간초과' 건수·단계로 기록한다.
기존 Word/Excel 실제 강제해제 `timeout_count`와 섞지 않는다.
`killed=0`·fired 없음에서 늦은 정상값과 일반 오류, close 중 deadline 초과,
정상 COM 뒤 COM 제한보다 긴 임베딩을 마치고도 정상 커밋, 확정 expired 결과의 미커밋,
커밋 직전 취소/원본 변경, timeout disabled, 다음 파일 deadline 독립성을 fake clock으로 검증한다.
기존 COM 경로도 diagnostics 오류 건수를 공개 accessor로 합산해 부분 읽기를 거부한다.
기존 일반 close 오류는 PPT 전용 `close_unconfirmed` 예외로 실패 처리하며 추출 return은 close 성공 뒤 한다.
이 보완은 `com` 모드에도 적용한다. Word·Excel의 close 정책은 이번 범위에서 바꾸지 않는다.

### 8-6. 구현 단위와 검증 전달

| 순서 | 변경 단위 | 완료 조건 |
|---|---|---|
| 1 | C# 공용 코어·운영 외피·순수 payload/오류 분류 | 시험 reader 회귀, 운영 버전·경로·부분읽기·본문 비노출 하네스 통과 |
| 2 | PowerPointComReader 모드·캐시·폴백·close 성공조건 | config 주입, fatal 포장·취소·독립 deadline(killed=0/disabled/COM 종료 확정/긴 임베딩 후 정상 커밋)·mtime 변경·부분결과 미커밋 fake 검증 |
| 3 | setup check/prepare/remove·준비 예약·Machine 락·저널 | Windows 최초 예약 ACL·UAC 대기중 앱종료/재시작·다른 사용자·helper 중단/핸들없음·중간 복구·점유 시험 |
| 4 | Qt coordinator·모든 시작 경로·준비 대기 분류 | 상태 전이표·check 출력오류와 prepare 종료미확인 분리·모든 시작게이트·timeout에 helper 비종료·다른 세션 경합 검증 |
| 5 | 빌드/배포·selftest·베타 안내 | 운영 DLL/setup/manifest 누락·버전/해시 불일치면 빌드 실패, 일반권한 포터블 시험 |

빌드는 `scripts/build_impl.bat`에서 build_guard 후 운영 C# payload를 컴파일하고
`AegisDesk.spec`가 해당 산출물을 포함한다. `app/selftest.py`는 등록·승격·Office 실행 없이
필수 payload·어셈블리·manifest와 앱 build commit/source_fingerprint 일치를 확인한다.
개발 source 실행의 payload 누락은 auto=com, addin=준비 대기다. 배포 빌드 누락은 허용하지 않는다.

각 코드 변경은 UPDATE_NOTES와 해당 테스트를 포함하고 전체 fake 회귀를 수행한다.
설계 검증 당시에는 Windows 설치·UAC·다른 사용자 세션·32비트 Office·사내 정책을 검증하지 않았고, 현재 구현에서도 해당 실환경 항목은 미검증이다.
세 문서의 시험 DLL 결과를 운영 DLL/장시간/자동설치 성공으로 승격하지 않는다.
검증 agent는 **시작 게이트의 누락, lock/설치 자식 수명, disabled/conflict 자동수정,
rollback 권한, 부분 추출 성공, fatal HRESULT 은폐, 빌드 출처 누락**을 우선 확인한다.

Windows API 근거: [ShellExecuteEx process handle](https://learn.microsoft.com/ko-kr/windows/win32/api/shellapi/ns-shellapi-shellexecuteinfow),
[UAC 권한 승인과 자격증명](https://learn.microsoft.com/ko-kr/windows/security/application-security/application-control/user-account-control/how-it-works).

### 8-7. 구버전 부모 폴더 권한 복구 후보

`candidate`/`released`의 정상 준비 경로에서 `parent_policy`가 실패하면 기존 **1.0.0.0** 설치만
추가 검사한다. `/check`, disabled/source 실행, 제거·일반 COM 경로는 권한을 복구하지 않는다.
일반 `RequestDirectory.Compatible`와 정상 lease의 거절 기준은 유지한다.

복구 전 설치 기록 schema 1의 고정 경로·버전·bitness·ProgID·CLSID·SHA-256·assembly와
HKLM 세 키를 대조한다. 같은 1.0.0.0의 실제 두 설치 이력인 `70fe63f`의 인코딩된 CodeBase와
`c5c638a`의 공백을 푼 CodeBase만 고정 DLL 경로와 정확히 비교한다. 다른 경로·버전은 허용하지
않는다. 실제 Reader 설치 루트·버전 폴더·파일은 Admin/System 소유의 보호된 ACL이어야 한다.
고정 외곽 `ProgramFiles/AegisDesk`만 역사적 생성 방식의 inherited ACL을 허용한다. 소유자는
여전히 Admin/System이며 canonical/non-null ACL과 비관리자 쓰기 차단을 요구한다. Windows
TrustedInstaller의 고정 service SID에는 현재 폴더(None)와 자식 디렉터리(CI|IO)에서
FullControl 상한의 부분 권한을 허용한다. Windows가 specific 상속 시 저장하는 CI(non-IO)는
None+CI|IO의 정확한 합집합이므로 외곽의 고정 TI에 한해 같은 범위로 판정한다. CREATOR OWNER는 OI|CI|IO에서만 같은 상한을
허용한다. ID 유무·generic/specific·SYNCHRONIZE·split/duplicate 표현 차이는 의미로 판정하되,
다른 SID의 inherit-only 쓰기·scope 확대는 거절한다. 실제 Reader 루트·파일에는 이
TrustedInstaller/CREATOR OWNER 예외를 적용하지 않는다. 원래 사용자의 HKCU
shadow·disabled를 사전 검사하고, 승격 자식은 HKLM·파일을 다시 확인한다.

부모 소유자는 원래 helper의 실제 토큰 SID 또는 Admin/System만 허용한다. generic mask는
unsigned 32bit file mapping(GR=0x120089, GW=0x120116, GX=0x1200a0, GA=0x1f01ff) 후
역할별 상한으로 비교한다. BA/System ACE가 모두 있어야 하며 실제 handle 열기 권한은 OS가
검증한다. 소유자·BA·System은 FullControl 부분집합, CO는 OI|CI|IO의 같은 상한만 지원한다.
Users RX는 None/CI/OI|CI, 역사적 쓰기는 CI에서 RX|0x116 상한이며 쓰기 비트가 최소 하나
남아 있어야 Repairable이다. Windows가 CI/generic 쓰기를 None/specific + CI|IO/generic으로
나눠 저장하면 두 역할의 쓰기 기여가 정확히 같은 경우에만 CI의 동등 표현으로 합쳐 분석한다.
기존 합법적인 None/P의 CreateDirectories 기여는 별도로 보존한다. 쌍이 없는 IO 쓰기는 거절한다. Users 부모 전용 권한은 None에서 RX|CreateDirectories 상한이다.
추가 SID는 알려진 상속 scope의 읽기/실행 부분집합만 보존한다. deny·callback/object·unknown ACE,
unknown mask/flags·NP·동등 쌍으로 증명되지 않은 IO 쓰기와
DELETE/DELETE_CHILD/WRITE_DAC/WRITE_OWNER는 거절한다.
부모 바로 아래는 기존 protected `PptReaderRequests` 하나뿐이어야 한다. child/request/backup의
프로그램 생성 exact 정책은 완화하지 않는다. 의미 분석과 별도로 lease의 원시 owner/group/DACL과
file identity를 재검사하므로 의미가 같은 변경도 작업 중에는 `namespace_changed`로 중단한다.

원래 helper는 mutex 안에서 별도 `RepairPreparationLease`와 기존 strict child/writer를 통해
schema 1 예약을 **UAC 전에** durable 저장한다. 일반 lease를 완화하거나 새 marker/schema를
추가하지 않는다. 승격 자식은 UUID만 받고 실제 helper의 PID·생성 시각·토큰 SID·실행 이미지·
payload를 확인한 뒤 같은 예약을 elevated로 갱신한다. 다른 관리자의 HKCU는 사용하지 않는다.

Admin/System만 쓸 수 있는 설치 루트의 `repair-backups/<UUID>.json`에 원본·중간·최종 SDDL,
부모 file identity, helper/child identity와 payload hash를 남긴다. 감사 파일은 protected canonical
ACL의 Admin/System FullControl·Users Read를 열린 파일 handle에서 정확히 확인한다. SID·SDDL은
앱 stdout/로그에 출력하지 않는다. machine 차단 근거는 감사 파일이 아니라 기존 미완료 예약이다.

백업 파일 교체에서 Win32 1175(원래 두 이름 보존)만 100ms 뒤 최대 한 번 재시도한다. 최초
교체 전에 source/target file identity·hash·길이·raw owner/group/DACL과 루트 identity/ACL을
고정하고, 재시도 직전 모두 동일하며 다른 partial이 없음을 다시 증명한다. strict ACL 검사를
완화하지 않는다. 다른 오류·변조·두 번째 실패는 추가 교체/자동 삭제 없이 중단한다. 이 처리는
백업 전용이며 일반 설치 기록·저널·요청 저장 경로에는 적용하지 않는다.

부모만 대상으로 `NtSetSecurityObject(DACL_SECURITY_INFORMATION)`를 두 번 호출한다. 첫 저장은
상속 권한을 명시화하고 protected로 만든 뒤 재조회하며, 두 번째 저장은 증명된 Users/CI 또는
동등 None/CI|IO 쌍의 각 ACE의
expanded specific mask에서 0x116만 제거한다. 읽기/실행/SYNCHRONIZE 잔여 권한은 보존하고
부모에만 RX|CreateDirectories를 보장한다. NTFS의 generic 매핑·동일 잔여 중복 제거를 예상값에
반영하며 실제 readback은 exact 비교한다. owner/group과 모든 기존 하위 ACL·내용은
보존한다. 하위 전파를 일으키는 SetSecurityInfo/Set-Acl 등으로 대체하거나 재귀 수정하지 않는다.
예상치 않은 sibling이 발견되면 보존하고 pending으로 중단한다.

자식 종료 후 원래 helper는 오래된 ACL을 가진 특수 lease를 폐기하고 정상 lease로 재취득한다.
원래 사용자·설치·최종 ACL·예약을 확인한 뒤 **같은 mutex 구간에서 복구 예약 complete와 다음
설치 예약 reserved를 연속 저장**한다. 복구와 Reader 업데이트에 UAC가 각각 필요할 수 있다.
최종 ready는 기존 설치 완료 후 원래 사용자 점검에서만 결정한다.

| 중단 상태 | 관리자 `/recover-parent UUID`의 허용 동작 |
|---|---|
| backup·부분 파일 없음, helper/child 종료 입증, 현재 Repairable 및 normal lease 거절, 설치 1.0.0.0 재증명 | ACL·설치·등록 변경 없이 기존 reserved/elevated 예약만 complete. `reservation_released_retry_required`, outdated/unsafe로 반환하고 새 prepare 필요 |
| 온전한 protected backup과 같은 file identity, 원본/중간/최종 ACL 일치 | 안전한 최종 DACL로만 전진한 뒤 예약 완료. Reader 업데이트는 다음 prepare에서 진행 |
| backup 없음+최종 safe ACL, backup 손상/temporary, 다른 버전 예약, 대상·PID identity 불명, 다른 sibling | 보존하고 pending 유지. 자동 삭제·광범위 권한 복원 없음 |

protected 원본과 첫 저장 후 중간 ACL을 구분하거나 복구 완료로 간주하지 않는다. 둘 다 현재
Repairable이면 관리자 명시 회수는 예약만 해제하고, 다음 prepare가 새 UUID·백업부터 재시작한다.
이 분기의 identity/원시 ACL 불변 보장 구간은 새 recovery lease 취득부터 complete 저장까지다.
과거 helper 시작 당시 같은 객체였다고 주장하지 않는다. ready는 반환하지 않으며, 늦은 자식은
complete 상태 검사에서 거절된다. 복구는 같은 버전/payload에만 허용하고 구버전 미완료 증거는
자동 삭제하지 않는다. 실행 결과와 미검증 범위는 [진단](../DIAGNOSTICS.md#ppt-acl-표현-호환-2026-10-25)을 따른다.

### 8-8. 관리자 설치 진단 전달 후보

1.0.5.14 후보는 관리자 helper의 최초 실패와 후속 복구 실패를 일반 helper 결과와 앱 로그에
전달한다. 진단은 설치 승인·완료·안전 판정의 근거가 아니며 기존 예외 전파·종료 코드·독립
Machine.Check·예약/저널/백업 schema와 권한 정책을 바꾸지 않는다.

일반 helper는 매 승격 전에 무작위 이름의 일회성 local named pipe를 만든다. native 생성은
FIRST_PIPE_INSTANCE·REJECT_REMOTE_CLIENTS·OVERLAPPED와 현재 사용자/Admin/System만의
protected ACL을 적용한다. 자식은 Anonymous SQOS로 연결해 일반 서버가 관리자 token을
impersonate하지 못하게 하고 서버 PID/생성시각을 확인한다. 서버는 수신 직후 보관한 peer PID를
실제 ShellExecuteEx가 반환한 자식 process handle과 대조한다. 인증 실패는 진단 폐기다.
파일이나 예약 필드를 추가하지 않는다. 기존 두 인수 승격 실행은 유지하며 진단 인수는 nonce와
원래 helper PID/생성시각만 추가한다. 설치·파일·레지스트리 대상 경로를 입력받지 않는다.

내부 wire는 길이 2KiB 이하의 단일 binary frame과 EOF다. 고정 magic/schema·요청/nonce GUID·
버전·phase·실제 자식 PID/종료 코드·마지막 단계·primary/secondary tuple을 엄격 검증한다.
자식 connect/write는 각각 500ms, 실제 자식 종료 뒤 부모 수집·취소 정리는 최대 1000ms로
제한한다. pending I/O 취소와 callback 예외를 정리하며 진단 실패가 기존 설치 예외를 덮거나
helper를 kill하지 않는다. 원래 자식 process 종료 관찰은 기존처럼 독립해서 유지한다.

일반 JSON의 선택 `child_diagnostics` 배열은 최대 두 요소이며 허용 순서는 복구→설치 또는
단일 복구/설치/제거다. 요소는 phase, transport, child_exit_code, last_stage, last_target,
primary, secondary 일곱 필드다. 자식 종료 코드는 실제 해당 handle에서 확인한 값이며 일반
helper의 최종 코드와 같다고 요구하지 않는다. 첫 실패는 고정하고 후속 rollback 실패만 별도로
보존한다. 원래 LastError/HRESULT는 wrapper/상태 변환 전에 포착하며, 처리 후 성공한 예상
예외는 terminal 실패로 보고하지 않는다. 정책 거절에는 원래 OS 오류를 합성하지 않는다.

Python은 기존 base 계약을 그대로 검증한다. 유효한 base에서 선택 배열만 손상되면 배열을
통째로 폐기하고 `malformed`로만 기록한다. base·중복 key·전체 출력 16KiB 위반은 기존처럼
전체 결과를 거절한다. 직접 주입한 관측도 같은 sanitizer를 거치고, 로그는 고정 어휘·숫자·
8자리 upper hex만 출력한다. helper 메시지·stack·경로·계정/SID·문서 내용은 전송하지 않는다.

앱이 일반 helper의 종료 결과를 수집했을 때만 앱 로그로 전달된다. 기존 관찰 timeout·앱 종료
뒤의 영구 보관/재수집은 범위 밖이다. 판독은 [관리자 설치 실패 진단](../guides/PPT_READER.md#관리자-설치-실패-진단),
실제 시험 범위와 미검증 환경은 [helper 진단 결과](../DIAGNOSTICS.md#ppt-관리자-helper-진단-전달-2026-10-26)를 따른다.

## 9. 개발 설계 검증 상태

설계 에이전트 작성 후 별도 검증 에이전트가 실제 코드와 대조했다. PPT의 강제 종료 없는
시간초과 판정, 설치 실패/종료 미확인 상태의 우선순위, UAC 대기와 앱 재실행 사이 준비 예약
수명의 세 지적을 수정했고 독립 재검증에서 새 중대·높음·보통 지적은 없었다. 당시 결과에 따라 구현에 착수했다.

이 결과는 문서·코드 계약과 링크·코드 블록·diff 검증이며 Windows 설치나 실제 Office를
실행한 검증이 아니다. §8-4의 일반 권한 최초 예약 생성·ACL·다른 관리자/세션·중단 복구
prototype gate가 통과되기 전에는 자동 설치를 배포하지 않는다.


## 10. 운영 구현 검증 범위

운영 DLL 외피와 시험 DLL이 같은 추출 코어를 사용하며 별도 식별자로 빌드된다.
`ppt_payload.decode_slide`의 순수 검증을 시험·운영이 공유한다. 시험 decoder는 오류 위치를
진단하기 위해 오류 포함 응답을 반환할 수 있고, 운영 호출은 `strict=True`로 위치·건수 관계까지
검사하고 오류 건수가 있으면 부분 결과를 거부한다.

수집기는 파일마다 COM 결과를 확정하고, 임베딩 후 LanceDB `add` 직전에 확정 결과와 현재
취소·원본 변경을 확인한다. PPT 시간 초과는 강제 종료 없이 별도로 집계하며 COM 호출이
영원히 멈춘 경우를 해제하는 기능으로 표시하지 않는다.

Windows 최초 자동 설치와 요청 디렉터리 ACL·UAC·다른 사용자/세션·Office 경합은 이 환경에서
검증하지 않았다. 기본 빌드는 disabled이며, candidate/released는 개발 빌드에서만 선택한다.
읽기 전용 점검이 Machine 안전을 확인하면 auto는 기존 COM을 사용할 수 있다.
설치·예약·저널이 불확실하면 모든 모드에서 PPT를 준비 대기로 처리한다.
최종 released 산출물을 고정한 뒤 Windows 승인 시험 전체를 그 파일로 수행하고 같은 해시의 파일만 배포한다.
candidate 시험 결과를 다른 released 파일의 승인으로 사용하지 않는다. 재빌드·payload 교체 시 다시 검증한다.


### 코드 리뷰와 검사 결과 (2026-10-13)

상위 에이전트의 코드 리뷰에서 실제 DB 저장 직전 안전 검사, 닫기 RPC 오류 후 캐시 폐기,
부분 읽기·본문 비노출, 시작 게이트의 초기화 실패 알림·재시도 기록, Office 점유/종료 대기
제어 예외의 원형 유지, 설치 예약·ACL·중단 복구·빌드 명령을 점검하고 발견 사항을 수정했다.

- 전체 `pytest knowmate/tests -v`: **1259 passed, 5 skipped, 8 failed**.
- 실패 중 메일 종료 1건·QLocalServer 3건은 구현 전에도 같은 환경에서 재현했다.
- 나머지 4건은 수정하지 않은 Word 소유 확인 테스트의 `worker.join(timeout=1)` 제한을
  전체 실행 부하에서 넘긴 결과다. 동일 테스트의 모든 32개 조건을 별도로 재실행해 **32 passed**를
  확인했다. 전체 검사가 완전히 통과했다고 표시하지 않는다.
- Office 점유 제어·PPT 읽기·본문 비노출·probe·config·phase4 관련 최종 검사: **215 passed, 3 skipped**.
- 마지막 재시도 초기화 실패 수정 후 Qt 준비/bridge 검사: **87 passed**. 전체 결과는 해당 작은
  UI 오류 반환 보완 이전에 수집한 수치이며, 보완은 이 별도 검사로 확인했다.
- C# 시험/운영 추출·연결·setup 컴파일 및 순수 하네스는 Mono에서 통과했다.
  Windows 네이티브 COM·실제 Office·UAC·ACL·레지스트리 rollback은 실행하지 않았다.

현재의 운영 등록·읽기 검증 명령과 자동 설치 출하 전 확인 항목은
[운영 PPT Reader 개발 검증](../guides/PPT_READER.md)에 있다.


## 9. PowerPoint 자동종료 후보

자동종료는 `candidate` 빌드에서만 시험한다. 기본·소스·`released`에서는 자동종료를
활성화하지 않는다. 설치의 released 모드와 정상종료 승인 여부는 별개다.
실제 Windows·Office의 직접 실행 연결 및 취소 이벤트 검증 전에는 정상종료 기능을 배포하지 않는다.

읽기용 소유 등록은 종료 권한이 아니다. 신뢰한 Office 실행파일을 `/EMBEDDING`으로 직접
실행한 프로세스의 handle·PID·생성 identity와 COM 호스트 증명이 모두 일치할 때만 종료를
요청한다. 기존 Dispatch-only, 실행 인계, identity 불명, 다른 호스트는 추측해서 종료하지 않는다.
실행 결과 불명의 기록은 COM을 담지 않는 프로세스 전체 상태로 유지해 새 워커의 중복 실행도 막는다.

문서 처리 완료·사이클 종료·기존 주기 재기동에서 호스트 세대마다 최대 한 번 요청한다.
동일 호스트의 호환 추가 기능이 주 STA에서 문서·보호된 보기·슬라이드 쇼가 모두 비어 있는지
확인하고, 종료 중 문서 닫기 취소 이벤트로 사용자 문서를 보호한다. 처음부터 PowerPoint
경고를 표시하며, 사용자 문서 Close/Save/Saved 변경이나 알림 억제로 종료하지 않는다.

Quit 반환·연결 단절은 실제 종료 증거가 아니다. COM 참조를 놓은 뒤 동일 프로세스의 실제
종료를 관찰한다. 미확인은 보류하고, 사용자가 문서를 열었으면 종료 권한을 폐기해 외부 점유로
취급한다. PPT `terminable=False` 및 read/quit watchdog·poison·앱 종료의 강제종료 금지는 유지한다.
poison 이후에는 Count·Quit을 포함한 새 RPC를 요청하지 않는다.

설치 읽기 잠금 안에서 생성·호환 서비스 확인·정상종료 요청을 수행한다. 읽기 COM 객체는 생성
QThread의 MTA에서만 사용하며 UI·타이머·OS 종료 helper로 넘기지 않는다. 동기 RPC가 멈췄다고
타임아웃만으로 취소됐다고 간주하지 않는다. 앱의 제한된 종료는 PowerPoint 강제종료를 허용하지 않는다.

Reader 1.0.5.14 후보는 호스트 STA에서 빈 컬렉션 확인과 `CommandBars.ExecuteMso("FileExit")`를
연속 실행한다. 확인 후 외부 클라이언트에 Quit을 맡기던 경로는 사용하지 않는다.
이전 실험 메서드 `TryQuitIfIdleWithCaller`·`ConfirmCallerQuit`은 `not_supported`로 차단한다.
문서 열림·새 문서·창 활성화를 관측하면 해당 종료 시도의 포기를 유지하며, 실제 종료 명령 전에
감지한 경우에는 명령을 실행하지 않는다. 사용자 열기를 막거나 UI 입력에 잠금을 걸지 않는다.

종료 명령 반환은 실제 닫힘 완료가 아니므로, 정상 요청 뒤에는 기존에 취득한 이벤트 보호와 native
참조를 호스트가 보유한다. 이후 닫기 이벤트에서 취소를 설정한 경우에만 `CloseCancelled`를 기록하고
보호 상태를 내린다. 다음 수동 닫기는 막지 않는다. `QueryQuitOutcome`은 확인된 취소를 `protected`로
보고하며 종료 요청만 반환된 경우와 구분한다. 보호 취득·정리는 기존 STA에서만 수행한다.
확인된 취소 뒤의 상태 조회 또는 호스트 연결 해제에서 기존 취득분을 한 번 정리한다.
poison·정리 실패는 결과 불명으로 두고 새 RPC나 Quit 재시도로 보정하지 않는다.

Python은 같은 수집 QThread에서 한 번만 요청하고, 짧은 상태 조회 뒤 COM 참조를 내려놓는다.
실제 종료 판단은 계속 원 handle·PID·생성 identity 관측으로 한다. 외부 Application.Quit·타이머·
강제종료와 새 패키지 의존성은 추가하지 않는다. 사용자 문서 보호·일반 배포 승인은 실제 검증 대상이다.

기존 dual 인터페이스는 유지하고 새 기본 인터페이스로 lifecycle을 노출한다. 요구 버전은
reader/Events/setup 모두 1.0.5.14, protocol 3이며 추출 JSON schema 1·청크·인덱스 형식은 바꾸지 않는다.
사전 검증과 사용자 문서 보호 시험은 [운영 검증 절차](../guides/PPT_READER.md#powerpoint-자동종료-후보-검증)를 따른다.


선택적 이벤트 DLL의 로딩·패키지·복구 계약은 [PPT 이벤트 어댑터](PPT_EVENT_ADAPTER.md)를 따른다.
