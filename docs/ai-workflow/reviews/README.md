# reviews/ — GPT 독립 리뷰 기록

- 파일명: `REVIEW-<YYYYMMDD>-<대상 slug>.md` (예: `REVIEW-20260801-architecture.md`).
- 생성: 채널 A(`gpt_review.py`) 또는 채널 B(GitHub Action)가 자동 생성. Codex CLI 리뷰는 같은
  형식으로 수동 정리해 커밋.
- **리뷰 파일은 생성 후 GPT 산출 부분을 고치지 않는다.** Claude의 판단은 하단 "처리 기록" 표에만
  추가한다(리뷰 원문과 판단의 분리 — 감사 가능성).
- 처리 기록의 각 행이 종결(수용→반영 커밋 / 기각→사유)되어야 해당 설계를 Accepted로 올릴 수 있다.
  Blocker/Major 미종결 상태의 Accepted 승격은 워크플로 위반이다.

## 처리 기록 형식 (Claude가 리뷰 파일 하단에 추가)

```markdown
---

## 처리 기록 (Claude, YYYY-MM-DD)

| ID | 판단 | 사유 / 반영 커밋 |
|---|---|---|
| B-1 | 수용 | <근거 요약> → 반영: <커밋 해시 or PR> |
| M-1 | 기각 | <기각 사유 — 근거 명시. "싫어서"는 사유가 아니다> |
| m-1 | 보류 | <후속 이슈/ADR 링크> |

**종결 판정**: Blocker 0 / Major 0 미종결 → 대상 문서 상태 Reviewed→Accepted 승격 가능.
```
