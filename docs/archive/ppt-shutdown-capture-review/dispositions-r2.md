# r1 → r2 지적 처리

작성자: /root. 대상: PPT_SHUTDOWN_CAPTURE_PLAN r2. 제품 코드 변경 없음.

| ID | 판단 | r2 반영 | 재검증 요청 |
|---|---|---|---|
| A-R1-01 (Medium) | 수용 | §5에서 정상 owner-STA callback 중첩을 정상 record로 보존하고 append 자체 재진입과 구별. 누락 counter의 하한/비영/조회 실패를 evidence_loss·unknown에 전파. §6 summary와 §9 fake/native 기준 동기화 | 전체 r2 대조 및 이벤트/append/coverage 계약의 누락 확인 |
| A-R1-02 (Medium) | 수용 | §7에 요청 반환→wrapper·인수·closure·예외 보유 해제→균형 CoUninitialize→caller OS exit 순서 명시. 30초 보유/강제 GC 제외, post_cleanup_observed_ms와 미확보 사유·180초 상한 유지. §9 정리 멈춤/늦은 완료 기준 추가 | 전체 r2 대조 및 내부/외부 경로·정리 후 실제 관측 범위 확인 |

두 항목 모두 severity 변경·범위 축소 없이 반영했다. 실제 fake/native/Windows 시험은 후속 구현 gate이며 이번 문서 보완의 통과 증거로 세지 않는다.
