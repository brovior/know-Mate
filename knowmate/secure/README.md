# secure/ 패키지 — 환경·보안 의존 코드 격리 구역

이 디렉토리에는 Windows 환경에 의존하는 코드(COM, DPAPI)를 격리해 놓는다.
다른 모듈은 이 디렉토리 밖에서 `win32com`, `win32crypt`를 **직접 import하지 않는다**.

---

## 1. COM 싱글톤 패턴 (`com_reader.py`)

### 왜 싱글톤을 쓰는가

구형 바이너리 포맷(`.doc` / `.xls` / `.ppt`)은 python-docx, openpyxl 같은
순수 파이썬 라이브러리로 파싱할 수 없다. Win32 COM을 통해 Word / Excel /
PowerPoint 애플리케이션 프로세스를 기동하고 그 API로 텍스트를 추출한다.

COM 애플리케이션을 파일 1개마다 `Dispatch → Quit`하면:
- **기동 비용**: Office 프로세스 하나 뜨는 데 1~3초 소요 → 수백 개 파일이면
  수 분이 낭비된다.
- **세션 누수 위험**: `Quit()` 실패 시 좀비 프로세스가 쌓여 메모리 누수.

싱글톤으로 **프로세스를 재사용**하면 최초 기동 1회만 비용을 치르고,
이후 파일은 `Open → 텍스트 추출 → Close`만 반복한다.

### 예외 처리 원칙

예외 발생 시 `_instance = None`으로 초기화해 다음 호출에 재생성한다.
`Quit()`를 매번 호출하는 방식은 절대 사용하지 않는다.

### 지원 확장자

| 확장자 | COM ProgID | 리더 클래스 |
|--------|-----------|------------|
| `.doc` | Word.Application | `WordComReader` |
| `.xls` | Excel.Application | `ExcelComReader` |
| `.ppt` | PowerPoint.Application | `PowerPointComReader` |

---

## 2. DPAPI + AES-256-GCM (`crypto.py`)

### 왜 키를 파일에 평문 저장하면 안 되는가

벡터DB(`%APPDATA%/AegisDesk/index`)는 개인 PC의 파일 시스템에 저장된다.
AES 키를 같은 위치에 평문으로 두면 DB 파일과 키를 함께 복사해 복호화할 수 있어
암호화 의미가 없어진다.

**Windows DPAPI(CryptProtectData / CryptUnprotectData)**는 현재 로그온한
Windows 사용자 자격증명에 키를 묶는다. 다른 사용자나 다른 PC에서는
복호화할 수 없다. 이 방식으로 AES-256 키를 `km.key` 파일에 저장한다.

### 암호화 포맷

```
base64( nonce(12B) | ciphertext | tag(16B) )
```

- `nonce`: 매 암호화마다 랜덤 생성 → 같은 평문이라도 다른 암호문
- `tag`: AES-GCM의 인증 태그 → 위변조 감지

### 복호화 원칙 (CLAUDE.md 5장 4번)

- 복호화는 검색 결과로 **선택된 청크**에 한해 메모리에서만 수행한다.
- 복호화된 평문을 파일·로그에 기록하지 않는다.
- `retriever.py`는 반환 직전에 복호화하고 즉시 상위로 넘긴다.

---

## 3. 사외 개발 환경 — fake 모드

`config.yaml`의 `extractor: fake`로 설정하면:

- `get_extractor("fake")` → `FakeReader` 반환 (COM import 없음)
- `get_crypto_manager(cfg)` → `FakeCryptoManager` 반환 (DPAPI import 없음)
- `FakeCryptoManager.encrypt(text)` → 평문 그대로 반환
- `FakeCryptoManager.decrypt(text)` → 입력 그대로 반환

fake 모드에서는 Windows 없이도 전체 파이프라인과 pytest가 동작한다.

---

## 4. 사내 PC 수동 검증 체크리스트

Phase 4 구현 후 회사 PC에서 아래 항목을 순서대로 확인한다.

### 전제 조건

- Python 3.11 가상환경 활성화
- `pip install pywin32 cryptography` 완료
- `config.yaml`의 `extractor: auto` (또는 `plain`)

### 체크리스트

1. **config 변경**
   ```yaml
   extractor: auto
   ```
   `config.yaml`을 위와 같이 수정한다.

2. **.doc 파일 배치**
   `watch_folders`에 등록된 폴더에 `.doc` 파일 1개를 복사한다.

3. **앱 실행 및 인덱싱**
   앱을 실행하고 사이드바 → [지금 재인덱싱] 클릭.
   진행 바가 완료될 때까지 기다린다.

4. **km.key 파일 확인**
   ```
   %APPDATA%\AegisDesk\km.key
   ```
   위 경로에 파일이 생성됐는지 확인한다.
   (파일 내용은 DPAPI 암호문이므로 사람이 읽을 수 없다)

5. **LanceDB text 컬럼 확인 (암호문 여부)**
   Python 대화형 셸에서:
   ```python
   import lancedb, os
   db = lancedb.connect(os.path.join(os.environ["APPDATA"], "AegisDesk", "index"))
   tbl = db.open_table("chunks")
   df = tbl.to_arrow().to_pandas()
   print(df["text"].iloc[0][:80])  # base64 암호문이어야 함 (평문 아님)
   ```

6. **채팅 검색 확인**
   채팅창에 `.doc` 파일 내용과 관련된 질문 입력.
   출처 카드에 해당 `.doc` 파일이 등장하는지 확인한다.

7. **앱 재시작 후 키 지속성 확인**
   앱을 종료 후 재시작. 같은 질문을 입력해 동일한 답변이 나오는지 확인한다.
   (DPAPI로 키를 재로드해 복호화가 성공해야 함)

8. **COM 싱글톤 확인**
   `.doc` 파일 10개를 인덱싱하면서 작업 관리자에서
   `WINWORD.EXE` 프로세스가 1개만 유지되는지 확인한다.

### 실패 시 확인 포인트

- `CryptoUnavailableError` → `pip install pywin32` 재설치
- `ComUnavailableError` → 같은 원인, pywin32 설치 확인
- `km.key` 파일은 있는데 복호화 실패 → 다른 사용자 계정으로 접근한 경우.
  원래 생성한 사용자 계정으로 로그인해야 한다.
