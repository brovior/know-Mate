# ARCHITECTURE.md — Aegis Desk 구조·디렉토리

> CLAUDE.md에서 분리한 구조 레퍼런스. 설계 *결정*의 근거는 `docs/DESIGN.md`, 여기는 "무엇이 어디에 있는가".

---

## 런타임 구조

```
PyQt6 + QWebEngineView
  ├─ HTML/JS UI (index.html / app.js / styles.css)
  │    └─ QWebChannel 브리지 (bridge.py)
  ├─ AgentRegistry
  │    ├─ knowledge_agent  ← RAG 지식검색 (완료)
  │    └─ mes_agent        ← stub ("준비 중")
  ├─ RAG 파이프라인
  │    ├─ Indexer       → chunker → embedding → LanceDB chunks 테이블
  │    ├─ EmailIndexer  → chunker → embedding → LanceDB emails 테이블 (Knox 메일)
  │    └─ Retriever → 벡터검색(chunks+emails 병합) → 권한필터 → 샌드위치배열 → LLM
  └─ CollectorWorker (QThread)
       ├─ Scanner(scandir) → 생산자 스레드 → 큐 → 소비자(추출·임베딩·저장)  ← 스캔·인덱싱 파이프라인
       │                     TextExtractor → Indexer, CleanupManager
       └─ MailScanner(scandir) → parse_mail_file(.mysingle/.eml) → EmailIndexer (orphan 정리 없음)
```

- **UI 셸 상주**: 닫기(X) 시 시스템 트레이로 숨김(설정으로 종료 전환 가능). 유휴 시 자동 인덱싱(설정으로 on/off·주기 조정).
- **설정 패널**(⚙): 연결(LLM/임베딩 주소)·검색(엄격도·문서수)·인덱싱(유휴·메일·파일크기·정리삭제)·동작(닫기·로그레벨) + 연결 테스트. `bridge.getSettings/saveSettings/testConnection/openConfigFile`.
- **파일 로깅**: `%APPDATA%/AegisDesk/logs/aegisdesk.log` (Rotating 5MB×3) + 전역 excepthook.
- **데이터 폴더**: `%APPDATA%/AegisDesk` (구 KnowMate 폴더는 최초 실행 시 자동 이전). config.yaml·index·logs·km.key·index_state.json 모두 여기 위치.

---

## 디렉토리 구조

```
AegisDesk.spec · build.bat      # PyInstaller 포터블 빌드 (onedir) · 사내 원클릭 빌드
.gitattributes                  # *.bat=CRLF 강제 (cmd가 LF 배치를 오파싱)
knowmate/
 ├─ app/          main.py · bridge.py · threads.py · ui/
 │                lifecycle.py(종료 계약) · single_instance.py · selftest.py(빌드 자체점검)
 ├─ agents/       base.py · registry.py · knowledge_agent.py · mes_agent.py
 ├─ rag/          indexer.py · email_indexer.py · retriever.py · embedding.py · chunker.py
 │                date_filter.py(한국어 기간 파서)
 ├─ collector/    scanner.py · mail_scanner.py · cleanup.py · state.py · scheduler.py
 │                failure_state.py(실패 분류·백오프) · com_watchdog.py(행오버 감시)
 │                purge_meta.py · idle_util.py
 ├─ secure/       base.py · plain_reader.py · com_reader.py · fake_reader.py
 │                mysingle_reader.py · crypto.py · signature.py · text_util.py
 │                com_stage.py(단계 계측, 순수 파이썬) · office_guard.py(프로세스 소유권)
 │                office_resiliency.py(세이프모드 표식 정리)
 ├─ llm/          client.py
 ├─ version.py    # __version__ (릴리스마다 갱신)
 ├─ config.py     # config.yaml 로더 (번들 템플릿 → %APPDATA% 시드, watch_folders만 초기화)
 ├─ config.yaml   # 배포 기본값 템플릿 (설정 추가 시 여기에만). 실사용본은 %APPDATA%/AegisDesk/config.yaml
 └─ tests/        test_phase1~4.py · 기능별 test_*.py · fixtures/sample.mysingle
scripts/          diag_search.py(검색 0건 진단) · diag_embed_latency.py(임베딩 구간 분해)
                  inspect_index.py · test_shared_db.py(5b 사전검증)
```

> `secure/com_stage.py`는 COM/win32를 import하지 않는 **순수 파이썬**이라 `collector/com_watchdog.py`가 직접 import해도 원칙3을 어기지 않는다. 이 예외의 근거는 해당 파일 docstring에 있다.

---

## 문서 지도

### 작업 규칙 · 구조

| 문서 | 내용 |
|---|---|
| `CLAUDE.md` | 공통 개발 계약(불변식·코드 변경 규칙) — 정본 |
| `AGENTS.md` | Codex와 개발 에이전트용 진입 지침 — `CLAUDE.md`로 위임 |
| `docs/ARCHITECTURE.md` | 이 문서 — 런타임 구조·디렉토리·문서 지도 |
| `docs/WORKFLOW.md` | 모델 사용 정책·수정노트 작성 규칙 |
| `docs/ENVIRONMENT.md` | 환경·패키지·버전 고정·배포·네트워크 드라이브 |
| `docs/ROADMAP.md` | 구현 단계·미착수 과제·5b 결론 |

### 설계

| 문서 | 내용 |
|---|---|
| `docs/DESIGN.md` | **설계 결정 상세 (정본)** — 스키마·파서·워치독·실패 백오프 등 |
| `docs/EMAIL_DESIGN.md` | 메일 인덱싱 (Knox `.mysingle` · `.eml`) |
| `docs/ISSUE_B_query_async.md` | 질의 비동기화 검토 기록 |
| `knowmate/secure/README.md` | Windows·보안 의존 코드 운영 및 사내 검증 안내 |

### 화면 · 배포

| 문서 | 내용 |
|---|---|
| `UI_SPEC.md` (루트) | 화면 사양 |
| `knowmate/app/ui/mockup.html` · `mockup_failures.html` | 룩앤필 · [확인 필요한 문서] 화면 |
| `docs/BETA_GUIDE.md` | 테스터 배포 가이드 |
| `docs/UPDATE_NOTES.md` | 베타 수정노트 (요일별) |
