# ADR-0002: purge의 전체 테이블 로드를 projection + 조건부 스킵으로 교체

| 상태 | 날짜 | 결정자 | 리뷰 |
|---|---|---|---|
| Accepted | 2026-07-24 | Claude (Chief Architect) | reviews/REVIEW-20260724-adr-0001-explicit-quit-for-tray-app-adr-0002-purge-projectio-*.md (1~14차, Blocker/Major 전건 처리·구현 반영, 리뷰11 M-1 축소채택·리뷰12 marker 비동기화/단일인스턴스 순서 정정·리뷰13 marker 해제 위치 재이동·리뷰14 unsupported capability_sig 재설계/marker join 상한 추가 근거 명시) |

## 맥락 (Context)
- `_purge_removed_folders`(watch_folders에서 제거된 폴더의 청크를 DB에서 삭제)는 매 인덱싱
  사이클 실행되며, 판단에 `file_path` 목록만 필요함에도
  `table.to_arrow().to_pandas()`로 chunks 테이블 **전체**를 로드한다 — 1024차원 float32
  벡터(청크당 4KB)와 AES 암호화 원문 포함.
- 유휴 자동 인덱싱이 기본 60초 간격으로 반복되므로, 변경 파일 0건인 유휴 방치 중에도 매분
  수십 MB(청크 1만 기준 벡터만 40MB)를 할당/해제한다. CPython/pyarrow는 해제 힙을 OS에
  즉시 반환하지 않아 RSS가 최고점에 눌러앉는다 — 베타에서 exe 메모리 70MB 도달 관측.
  인덱스가 수만 청크로 커지면 사이클당 수백 MB로 확대되는 시한폭탄이다.
- CLAUDE.md 원칙 10은 "DataFrame 변환은 `table.to_arrow().to_pandas()`"를 규정하는데, 이는
  `table.to_pandas()` 직접 호출 금지가 취지이며 컬럼 projection을 금지하는 것이 아니다.
- CleanupManager(파일 단위 orphan 정리)는 state dict 기반이라 이 문제와 무관하다.

## 결정 (Decision)
① purge의 DB 조회를 `file_path` 단일 컬럼 projection으로 교체한다(벡터·원문 미로드).
② 스킵 조건: "**op_sig 불변 && 처리 0건 && 마지막 성공 purge 후 24h 미경과**"일 때만 purge를
생략한다. op_sig는 사이클 시작 시 고정한 불변 스냅샷(normcase/normpath·중복 제거·정렬)에
`dry_run`·`max_delete_ratio`를 더해 SHA-256으로 계산한다(프로세스 간 안정·설정 변경 시 재실행).
③ 메타 갱신·판정 규칙: 억제 판정을 성공 스킵보다 **먼저** 수행한다(차단 → 백오프 → 성공 스킵 →
실행 순). 성공 완료 시에만 `reconciled_sig`·`last_purge_ts`를 갱신하고 실패·차단 표식을 해제한다.
일시적 예외는 `failed_sig`+`next_retry_ts`(기본 30분 백오프)를 기록하며 **동시에
`reconciled_sig`를 해제**한다 — 백오프 만료 후 이전 성공 메타가 성공 스킵을 성립시켜 재시도를
24h까지 막는 결함 방지. 대량삭제 차단은 `blocked_sig`로 기록해 동일 op_sig에 대해 자동 재시도하지
않는다(구성·차단율 변경 시에만 재실행 — 이 미복구는 R-0002 FR-3의 명문화된 예외). 성공·실패·차단 상태 모두 sidecar 저장 성공 여부와 무관하게 **프로세스 내 메모리에 즉시
반영**한다 — 저장 실패 시 현 프로세스는 상태를 유지(성공=정상 스킵 지속, 실패·차단=억제·알림
1회 유지)하고, 재시작 후에만 보수적으로 재실행된다(멱등이라 안전). ④ 메타는 `index_state.json`이 아닌 **sidecar 파일**
(`index_state.meta.json`, tmp→replace 원자 교체)에 보관해 기존 state 스키마·소비자를 건드리지
않는다. 시각 필드 검증은 필드별로 다르다: `last_purge_ts`는 **모든 미래값 무효**(스킵 불가, 오차허용
없음), `next_retry_ts`는 `now < 값 ≤ now+설정백오프`의 미래값이 **정상**이고 그 밖만 손상
취급(억제 해제). 메타 부재·타입/범위 이상은 부재와 동일 취급(스킵 없이 실행). purge 성공 후 메타
저장 실패의 재실행은 삭제(file_path 기준)·optimize의 멱등성으로 안전. ⑤ op_sig는 스키마 버전을 포함한 canonical JSON(sort_keys·고정
separator·UTF-8)의 SHA-256이며, 경로는 서명·소속판정 공용 정규화 함수 1개의 결과만 사용.
⑥ projection 조회는 **`table.search().select(["file_path"]).to_arrow()`**로 확정(Arrow 직접
순회, pandas 변환 생략). lancedb 0.34.0에서 실측 검증됨(결과 스키마에 `file_path`만 실림, 전체
로드 대비 약 6배 빠름, 숨은 기본 limit 없음 — 상세는 architecture.md § 핵심 결정과 트레이드오프).
검토했던 `table.to_lance().to_table(columns=[...])`는 별도 `pylance` 설치가 필요해 기각. 미지원
lancedb 버전에서는 `.select()` 호출이 `AttributeError`로 실패하는데, 이는 재시도로 복구되지 않는
**영구 장애**(배포 의존성 비호환)이므로 일시적 DB I/O 실패와 구분한다 — `"unsupported"`로 분류돼
장기 억제되고(30분 백오프 반복 없음) 1회 UI 알림으로 업데이트를 안내한다(리뷰11 M-2로 "failed"와
분리; 호환 전체-로드 모드를 두지 않는 원칙은 유지 — 조용한 폴백 금지). 억제 해제 판정은 대량삭제
차단(`op_sig` 기준)과 달리 **`compute_capability_sig()`(lancedb 버전 지문)** 기준이다(리뷰14 M-1) —
unsupported는 watch_folders 구성이 아니라 실행 환경의 문제이므로, op_sig로 억제하면 사용자가
안내대로 앱을 업데이트해도 폴더 구성이 그대로면 억제가 절대 풀리지 않는 결함이 있었다.

## 검토한 대안 (Alternatives)
| 대안 | 장점 | 단점 | 기각 사유 |
|---|---|---|---|
| projection 없이 스킵만 도입 | 변경 최소 | 구성 변경 사이클·대형 인덱스에서 여전히 전체 로드 | 근본 원인(불필요 컬럼 로드) 잔존 |
| 유휴 주기(60초)를 늘림 | 빈도 감소 | 인덱싱 신선도 저하, 사이클당 비용은 그대로 | 대증요법 — 별도 논의로 분리 |
| purge를 watch_folders "변경 시에만" 실행(0건 조건 없이) | 스킵 최대화 | 외부 요인으로 생긴 state-DB 불일치의 복구 기회가 더 줄어듦 | 처리 건이 있는 사이클엔 실행하는 편이 복구 기회 보존 |
| 주기적 `gc.collect()` 추가 | 도입 쉬움 | 파편화·arena 미반환은 해결 못 함, 근본 원인 무관 | 효과 불확실한 보조책 — 필요 시 후속 |

## 결과 (Consequences)
- 좋아지는 것: 유휴 방치 중(변경 0건) purge의 DB 조회가 0회가 되고, 조회가 필요한 사이클도
  메모리 사용이 인덱스 크기(벡터)와 무관해진다. RSS 눌러앉음의 주요 원인 제거.
- 감수하는 것: 외부 요인으로 DB에만 남은 고아 경로의 복구가 지연될 수 있다 — **구성·파일
  변경이 있는 사이클에서는 즉시** 실행되고, **변경이 전혀 없어도 최대 24시간(강제
  reconciliation 주기, config화)마다** 1회 강제 실행된다(리뷰10 m-1로 architecture.md/A-0002와
  정합화 — 24h 상한이 있어 무기한 방치는 아니다). **단, `"unsupported"`(projection API 비호환)는
  이 24h 예외다**(리뷰14 M-1) — 시간이 아니라 `compute_capability_sig()` 변화(앱/lancedb 업데이트)로만
  재검증하며, 24h가 지나도 환경이 그대로면 재시도하지 않는다(재시도해도 동일하게 실패할
  영구 장애이므로 무의미한 재시도 자체가 낭비). projection API는 lancedb 0.34.0에서 실측
  검증 완료(`table.search().select(["file_path"]).to_arrow()`, 상세는 architecture.md).
- 후속 조치: 스킵 조건 단위 테스트(서명 비교·스파이), projection 결과 컬럼 검증 테스트,
  watch_folder 제거 회귀 테스트 유지, 사내 유휴 1시간 RSS 실측(전후 비교), `max_delete_ratio`
  fail-closed 검증·dirty-shutdown 마커 구현 완료(리뷰10 B-1/M-1).
