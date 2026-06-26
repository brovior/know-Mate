# CLAUDE.md — KnowMate
> Phase 1~4 완료, Phase 5 진행 중 (Knox 메일 인덱싱 구현) | 2026-06-26

## 프로젝트 개요

**개인 PC용 사내 지식 AI 비서 데스크톱 앱.** PyQt6 + QWebEngineView 셸 위에서 여러 에이전트가 구동되는 멀티 에이전트 구조. Phase 1~4 완료(RAG 지식검색), Phase 5 진행 중(Knox 메일 인덱싱 구현 완료, 공용 DB·PyInstaller 배포 예정).

핵심 시나리오: "작년 A설비 알람 폭주 때 처리 절차 찾아줘" → 로컬 문서·메일 의미 검색 → 요약 답변 + 출처 제시.

---

## 아키텍처

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
       ├─ Scanner      → TextExtractor → Indexer, CleanupManager
       └─ MailScanner  → MysingleReader → EmailIndexer (orphan 정리 없음)
```

---

## 디렉토리 구조

```
knowmate/
 ├─ app/          main.py · bridge.py · threads.py · ui/
 ├─ agents/       base.py · registry.py · knowledge_agent.py · mes_agent.py
 ├─ rag/          indexer.py · email_indexer.py · retriever.py · embedding.py · chunker.py
 ├─ collector/    scanner.py · mail_scanner.py · cleanup.py · state.py · scheduler.py
 ├─ secure/       base.py · plain_reader.py · com_reader.py · fake_reader.py
 │                mysingle_reader.py · crypto.py · signature.py · text_util.py
 ├─ llm/          client.py
 ├─ config.py     # config.yaml 싱글톤 로더
 ├─ config.yaml   # 런타임 설정 전체 (설정 추가 시 여기에만)
 └─ tests/        test_phase1~4.py · test_mysingle.py · fixtures/sample.mysingle
```

참고 문서: `app/ui/UI_SPEC.md` (화면 사양) · `app/ui/mockup.html` (룩앤필) · `docs/DESIGN.md` (설계 결정 상세) · `docs/EMAIL_DESIGN.md` (메일 인덱싱 설계)

---

## 핵심 원칙 (위반 금지)

1. **에이전트 인터페이스**: `handle(query: str, context: dict) -> list[Block]` 하나로만 통신. UI에 에이전트별 분기 로직 금지.

2. **임베딩 모델·차원은 `rag/embedding.py` 상수로 고정** (`VECTOR_DIM = 1024`). 모델 변경 시 전체 재인덱싱 필수.

3. **보안 의존 코드는 `secure/` 밖으로 나가지 않는다.** COM/DPAPI를 다른 모듈에서 직접 import 금지.

4. **mock 전환은 config 한 줄.** `extractor: fake | plain | auto`. fake 모드로 사외 전체 테스트 통과해야 함.

5. **벡터DB 원문(`text` 컬럼)은 반드시 AES-256-GCM 암호화 저장.** 복호화 평문을 파일·로그에 남기지 않는다.

6. **공용 벡터DB에 개인 PC는 절대 쓰기하지 않는다.**

7. **로그에 문서·메일 본문 출력 금지.** 경로·건수·소요시간만.

8. **수집기는 QThread 워커에서만 실행.** multiprocessing 금지 (LanceDB 파일 락 충돌).

9. **scopes 빈 배열이면 전체 검색 fallback 금지.** JS 1차 + knowledge_agent 2차 차단.

10. **LanceDB API**: `optimize()` 사용 (`compact_files()` deprecated 금지). DataFrame 변환은 `table.to_arrow().to_pandas()` (`table.to_pandas()` 직접 금지).

---

## 구현 단계

| Phase | 내용 | 상태 |
|---|---|---|
| 1 | UI 셸 + 에이전트 골격 | ✅ 완료 |
| 2 | RAG 파이프라인 (chunker/embedding/indexer/retriever) | ✅ 완료 |
| 3 | 수집기 (증분스캔 + orphan정리 + 스케줄러) | ✅ 완료 |
| 4 | 보안 모듈 (COM 싱글톤 + AES-GCM + DPAPI) | ✅ 완료 |
| 5a | Knox `.mysingle` 메일 인덱싱 | ✅ 완료 |
| 5b | 공용 벡터DB 읽기 연동 | 🔲 예정 |
| 5c | PyInstaller 배포 | 🔲 예정 |

> Outlook PST 인덱싱은 COM 보안 정책 선결 검증 후 5b 이후에 착수. 상세는 `docs/EMAIL_DESIGN.md` §8 참조.

---

## 환경

- **OS**: Windows 10/11 · **Python**: 3.11
- **임베딩 운영 모드**: 반드시 `mode: api`. `local` 모드는 폐쇄망에서 모델 다운로드 불가로 무한 대기 발생.
- **패키지**: PyQt6, PyQt6-WebEngine, lancedb, pyarrow, pywin32, cryptography, PyMuPDF, python-docx, openpyxl, python-pptx, pytest

---

## 코딩 규칙

- 함수는 단일 책임. 파일 300줄 초과 시 분리 제안.
- 모든 public 함수에 타입 힌트 + 한 줄 docstring.
- 예외는 삼키지 않는다. 수집기는 파일 1건 실패가 사이클 전체를 멈추지 않도록 건별 try/except.
- 설정값 하드코딩 금지 — 전부 config.yaml.
- UI 작업 시 `UI_SPEC.md` · `mockup.html` 먼저 읽고, 스펙과 다른 판단 필요 시 먼저 묻는다.
- 로그 레벨: DEBUG(흐름 추적) / INFO(정상 결과) / WARNING(복구 가능) / ERROR(즉시 확인).
