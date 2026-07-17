# Aegis Desk

사내 문서를 AI로 검색하는 개인 PC용 데스크톱 지식 비서입니다.

> **Aegis Desk (이지스 데스크)** — 그리스 신화의 방패 '이지스'에서 영감을 받아, 강력한 암호화로 원문을 안전하게 보호하면서 스마트하게 답변하는 철벽 비서. (구 KnowMate)

"작년 A설비 알람 폭주 때 처리 절차 찾아줘"처럼 자연어로 질문하면, 인덱싱된 사내 문서에서 관련 내용을 찾아 요약 답변과 출처를 제시합니다.

---

## 주요 기능

- **자연어 문서 검색**: bge-m3 임베딩 기반 의미 검색 (벡터 유사도 + 파일명 키워드 보강)
- **다양한 파일 형식 지원**: docx, xlsx, pptx, pdf, txt, doc, xls, ppt
- **메일 인덱싱**: Knox `.mysingle` + 표준 `.eml` (발신인·수신인·날짜 메타데이터 포함 검색)
- **증분 인덱싱**: 변경된 파일만 재인덱싱 (mtime + size + 인덱스 버전 비교)
- **검색 범위 선택**: 내 PC 문서 / 공유 폴더 / 내 메일함 체크박스 전환
- **상대 날짜 인식**: "어제/저번주/25주차" 등 (LLM 프롬프트에 현재 날짜 주입)
- **출처 표시**: 답변에 사용된 문서·메일 파일명, 경로/발신인, 일치율 제공
- **대화 스레드**: 이전 질문 목록 저장 및 복원
- **설정 패널(⚙)**: 서버 주소·검색 엄격도·인덱싱 옵션·닫기 동작을 UI에서 변경 + 연결 테스트
- **시스템 트레이 상주**: 창을 닫아도 백그라운드에서 유휴 자동 인덱싱 지속
- **암호화 저장**: 벡터DB 내 원문을 AES-256-GCM으로 암호화 (키는 Windows DPAPI 보호)

---

## 아키텍처

```
PyQt6 + QWebEngineView
  └─ HTML/JS UI (채팅 인터페이스)
       ↕ QWebChannel (JS ↔ Python 브리지)
  └─ AgentRegistry
       └─ KnowledgeAgent
            ├─ Retriever → LanceDB (로컬/공용 벡터DB)
            └─ LLMClient → 사내 LLM API
  └─ CollectorWorker (QThread)
       └─ Scanner → TextExtractor → Indexer → LanceDB
```

---

## 환경 요구사항

- **OS**: Windows 10 / 11
- **Python**: 3.11
- **네트워크**: 사내망 (임베딩 API, LLM API 접근 필요)

---

## 설치 및 실행

### 소스 실행 (개발)

```bash
# 1. 가상환경 생성
py -3.11 -m venv .venv
.venv\Scripts\activate

# 2. 패키지 설치 (사내 PyPI 미러 사용)
pip install -r requirements.txt

# 3. config.yaml 설정 (배포 템플릿)
# knowmate/config.yaml의 embedding.base_url, llm.base_url에 사내 서버 IP 입력
# 최초 실행 시 이 템플릿이 %APPDATA%/AegisDesk/config.yaml로 시드됨 (watch_folders만 초기화).
# 이후 설정은 %APPDATA% 사본을 읽고 쓰며, 앱의 설정 패널(⚙) 또는 파일 직접 편집으로 변경.

# 4. 실행
python knowmate\app\main.py    # 또는 run.bat
```

### 포터블 빌드 (배포)

```bash
# pyinstaller 설치 후, config 템플릿의 base_url을 실제 사내 IP로 채운 뒤
build.bat
# → dist\AegisDesk\ 생성 (AegisDesk.exe + _internal 폴더).
# 이 폴더를 통째로 zip으로 압축해 테스터에게 배포 (exe 단독 배포 불가).
# 배포 가이드: docs/BETA_GUIDE.md
```

---

## 설정 (config.yaml)

대부분의 설정은 앱의 **설정 패널(⚙)**에서 변경할 수 있으며, 나머지는 `%APPDATA%/AegisDesk/config.yaml` 직접 편집으로 바꿉니다.

| 키 | 기본값 | 설명 | 설정 UI |
|---|---|---|---|
| `log_level` | `INFO` | DEBUG / INFO / WARNING / ERROR | ✅ |
| `extractor` | `auto` | fake / plain / auto (확장자 자동 선택) | |
| `embedding.mode` | `api` | fake / local / **api** (운영은 반드시 api) | |
| `embedding.base_url` | - | 사내 임베딩 서버 IP | ✅ |
| `embedding.model`(코드 상수) | `bge-m3` | 고정값(읽기전용). 변경 시 전체 재인덱싱 | ✅(표시) |
| `chunking.chunk_size` | `400` | 청크 크기 (한국어 기준 자) | |
| `chunking.max_file_size_mb` | `30` | 파일 크기 상한 (초과 시 인덱싱 제외) | ✅ |
| `chunking.xlsx_max_rows_per_sheet` | `2000` | xlsx 시트 행 수 상한 | |
| `chunking.max_chunks_per_file` | `500` | 파일당 최대 청크 수 | |
| `search.score_threshold` | `0.3` | 유사도 임계값(검색 엄격도, 0~0.7) | ✅ |
| `search.top_k_max` | `10` | 답변 참고 문서 수 (3~20) | ✅ |
| `collector.watch_folders` | `[]` | 인덱싱할 폴더 목록 (앱의 [폴더 관리]에서 지정) | |
| `collector.idle_enabled` | `true` | 유휴 시 자동 인덱싱 on/off | ✅ |
| `collector.idle_seconds` | `60` | 유휴 감지 후 자동 인덱싱 간격(초) | ✅(분 단위) |
| `cleanup.dry_run` | `true` | true이면 제거된 폴더의 인덱스를 실제 삭제하지 않고 로그만 | ✅(자동삭제 토글) |
| `cleanup.max_delete_ratio` | `0.3` | 대량 삭제 차단 임계(전체의 30% 초과 삭제 시 차단) | |
| `mail.enabled` | `true` | 메일 파일 자동 인덱싱 on/off | ✅ |
| `mail.extensions` | `[.mysingle, .eml]` | 인덱싱할 메일 확장자 | |
| `mail.max_mails_per_scan` | `500` | 스캔당 처리 상한 | |
| `llm.mode` | `api` | fake / claude / openrouter / **api** | |
| `llm.base_url` | - | 사내 LLM 서버 IP | ✅ |
| `llm.model` | `qwen3-27b` | 사용할 모델명 | ✅ |
| `ui.close_action` | `tray` | 닫기(X) 동작: tray(트레이 숨김) / quit(종료) | ✅ |

> `embedding.mode: local`은 폐쇄망에서 Hugging Face 모델 다운로드를 시도하므로 사용하지 마세요.
> 서버 IP가 바뀌면 설정 패널에서 직접 수정하거나, `%APPDATA%/AegisDesk/config.yaml`을 삭제 후 재실행하면 번들 템플릿에서 재시드됩니다.

---

## 지원 파일 형식

| 확장자 | 파서 | 비고 |
|---|---|---|
| docx, xlsx, pptx, pdf, txt | python-docx / openpyxl / python-pptx / PyMuPDF | 직접 파싱 |
| doc, xls, ppt | win32com (COM) | 구형 바이너리, Windows 전용 (Office 설치 필요) |
| .mysingle, .eml | 표준 `email` 라이브러리 (RFC822+MIME) | 메일. Knox 백업 + 표준 이메일 |

- xlsx 파일에서 `docProps/custom.xml` 손상으로 인한 로드 실패는 자동 복구합니다.
- pptx의 SmartArt·OLE 등 인식 불가 도형이 있어도 파일 전체가 실패하지 않고 추출 가능한 텍스트만 인덱싱합니다.
- Outlook `.msg`/PST는 미지원 (전용 파서·COM 필요, 차후 과제).

---

## 대용량 파일 처리

대용량 데이터 파일이 인덱싱을 수시간 점유하는 문제를 3단으로 방어합니다:

1. **파일 크기 상한** (`max_file_size_mb: 30`): 파싱 전에 제외
2. **xlsx 시트 행 수 상한** (`xlsx_max_rows_per_sheet: 2000`): 초과 시 메타 청크 1개로 대체
3. **파일당 최대 청크 수** (`max_chunks_per_file: 500`): 초과분 절단

---

## 테스트

```bash
# fake 모드로 사외 환경에서도 전체 테스트 실행 가능
pytest knowmate/tests/ -v
```

Phase별 테스트 파일:
- `test_phase1.py` — UI 셸 + 에이전트 골격
- `test_phase2.py` — RAG 파이프라인 (chunker / embedding / indexer / retriever)
- `test_phase3.py` — 수집기 + orphan 정리
- `test_phase4.py` — 보안 모듈 (COM / crypto)

---

## 디버깅

로그 파일은 `%APPDATA%/AegisDesk/logs/aegisdesk.log`에 남습니다 (콘솔에도 동시 출력, Rotating 5MB×3). 로그 레벨은 설정 패널 또는 config에서 변경합니다.

```yaml
log_level: DEBUG   # 흐름 상세 추적
```

- **인덱싱 완료 신호**: `수집기 사이클 완료: N.N초` (이 줄이 떠야 UI의 "인덱싱 중"이 풀림)
- **단계별 소요시간**(INFO): 파일별 `extract=Xs`, `embed=Xs save=Xs` — 병목(추출/임베딩/저장) 확인용
- **특정 파일에서 멈출 때**(DEBUG): `[단계1] 추출 시작 → [단계2] 완료 → [단계4] 임베딩·저장 시작 → [단계5] 완료` 로 블로킹 지점 확인 (주로 COM 추출 또는 임베딩 API 대기)

---

## 구현 현황

| Phase | 내용 | 상태 |
|---|---|---|
| Phase 1 | UI 셸 + 에이전트 골격 | ✅ 완료 |
| Phase 2 | RAG 파이프라인 | ✅ 완료 |
| Phase 3 | 수집기 (증분 스캔 + orphan 정리) | ✅ 완료 |
| Phase 4 | 보안 모듈 (COM + AES-GCM + DPAPI) | ✅ 완료 |
| Phase 5a | 메일 인덱싱 (.mysingle + .eml) | ✅ 완료 |
| Phase 5c | 포터블 빌드 + 설정 패널 + 트레이 상주 + 파일 로깅 | ✅ 완료 |
| 베타 | 소수 테스터 배포 | 🔄 진행 |
| Phase 5b | 공용 벡터DB (로컬 캐시 복사 방식) | 🔲 예정 |

---

## 보안

- 벡터DB 내 원문(`text` 컬럼)은 AES-256-GCM으로 암호화 저장
- 암호화 키는 Windows DPAPI로 보호 (해당 PC, 해당 사용자만 복호화 가능)
- 로그에 문서·메일 본문을 출력하지 않음 (경로·건수·소요시간만)
- 미처리 예외는 로그에 기록 (전역 excepthook — 콘솔 없는 빌드에서도 원인 추적)
- 공용 벡터DB(5b, 예정)는 개인 PC에서 쓰기 금지 — 마스터만 인덱싱·배포, 사용자는 로컬 캐시로 복사 후 읽기
