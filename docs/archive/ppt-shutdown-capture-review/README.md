# PPT 종료 진단 수집 계획 — 검증 결과

**2026-10-20, 최종 계획 r2의 미해결 Critical/High/Medium/Low는 각각 0건이다.**
새 컨텍스트 검증자 2명이 총 3회 검토했다. 계획 검토 완료이며 구현·시험·live·배포 승인은 아니다.

- [최종 개발 계획](../../design/PPT_SHUTDOWN_CAPTURE_PLAN.md)
- [최종 계획 고정 사본](plan-r2.txt)
- 최종 SHA-256: `aca9def288c72d6ac2f3304055f140a6c7570d011cfc26753eafc1313405d656`
- 기준 소스: `brovior/know-Mate` main `b75c98511cea3ddb7d842c7204dd11b4f9767ea3`의 관련 부분 스냅샷.

## 실제 검증 기록

| 검증자·회차 | 전체 대상 | Critical / High / Medium / Low | 결과 |
|---|---|---|---|
| A / 1차 | [r1](plan-r1.txt) | 0 / 0 / 2 / 0 | [중첩 callback 누락 의미·참조 정리 시점 지적](reviewer-a-round1.md) |
| A / 2차 | [r2](plan-r2.txt) | 0 / 0 / 0 / 0 | [전체 r2 재검토, 두 지적 해소](reviewer-a-round2.md) |
| B / 새 검증자 최종 | [r2](plan-r2.txt) | 0 / 0 / 0 / 0 | [선행 검토에 참여하지 않은 새 검증자의 전체 독립 대조](reviewer-b-final-r2.md) |

수용·보완·재검증 기준은 [지적 처리 기록](dispositions-r2.md)에 있다. 심각도 하향이나 비공개 범위 축소로 닫은 지적은 없다.
txt 사본은 검토 당시 문서의 byte-identical 보존본이다. 그 안의 상대 링크는 원래 `docs/design/` 위치 기준이며 현재 지시 문서가 아니다.

## 쉽게 보는 최종 계획

PPT 안에서는 어떤 요청·이벤트·정리가 일어났는지 기록하고, 별도 프로그램은 실제 프로세스 종료와 로그 보존을 담당한다.
핵심 객체 확인을 부가 조회보다 먼저 하고, 종료 요청 완료·수집 완료·Windows 실제 종료를 따로 판정한다.
정상 종료 callback과 우리 코드의 취소 설정을 보존하며, COM 참조를 정리한 뒤 실제로 확보한 관측 시간도 남긴다.
새 PPT 하나에 종료 요청은 최대 1회이며 거절·unknown 뒤 추가 COM 요청과 자동 재시도는 하지 않는다.

## 현재 하지 않은 일

제품 코드 변경, 새 fake/전체 회귀, Windows native transport·ACL·ABI·WCT, 빌드·package/pin 생성,
설치/등록/UAC, 실제 Windows·Office 합성·사용자 문서 보호·오버헤드 및 사내 DRM·배포 시험은 수행하지 않았다.
계획의 이 시험들은 후속 구현 gate로 남으며, 이 문서의 0건 판정은 실행 안전성 보장이 아니다.
현재 workspace는 완전 checkout이 아니다. 후속 구현 전 전체 저장소·위임 지침·현재 작업 상태를 확인한다.

작성한 계획·리뷰 문서는 이 workspace에 보존했으며 커밋·push·PR은 만들지 않았다.
