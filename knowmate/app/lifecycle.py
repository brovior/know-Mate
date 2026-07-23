"""앱 종료 시 워커 정리 로직 (PyQt6 비의존 — 사외 단위 테스트 가능).

MainWindow._shutdown이 직접 하던 워커 종료 에스컬레이션을 분리했다. 워커가
COM Open 등에 블로킹돼 취소 플래그를 못 보면 정상 종료가 안 돼 프로세스가
잔존(트레이 [종료]를 눌러도 안 죽고 자원을 계속 물어 PC가 버벅임)한다. 그래서
정상 종료 → 실패 시 스레드 강제 종료 → 그래도 실패 시 프로세스 하드 종료로
단계적으로 강제해, 종료가 반드시 프로세스를 끝내도록 보장한다.

worker는 QThread를 덕타이핑(cancel/isRunning/wait/terminate)하고, 최종 하드
종료는 hard_exit로 주입해 테스트에서 프로세스를 실제로 죽이지 않고 검증한다.
"""
import logging
import os

logger = logging.getLogger(__name__)

# 취소 플래그 확인 후 정상 종료를 기다리는 시간(현재 처리 중인 파일 완료 여유)
_WAIT_GRACEFUL_MS = 8000
# terminate() 후 스레드가 실제로 사라지길 기다리는 시간
_WAIT_AFTER_TERMINATE_MS = 3000


def _default_hard_exit(code: int = 0) -> None:
    """로그를 flush하고 프로세스를 즉시 종료한다(마지막 수단)."""
    logging.shutdown()
    os._exit(code)


def stop_worker(worker, hard_exit=_default_hard_exit) -> None:
    """워커를 정상 종료 → 실패 시 강제 종료 → 그래도 실패 시 프로세스 하드 종료.

    worker: cancel()/isRunning()/wait(ms)->bool/terminate() 를 갖는 객체(또는 None).
    hard_exit: 최종 프로세스 종료 콜백(기본 os._exit 래퍼, 테스트 주입용).
    """
    if worker is None or not worker.isRunning():
        return

    worker.cancel()
    # 현재 처리 중인 파일 완료 후 정상 종료 대기
    if worker.wait(_WAIT_GRACEFUL_MS):
        return

    # COM Open 등에 블로킹돼 취소 플래그를 못 본 상태 → 스레드 강제 종료
    logger.warning("워커가 제때 종료되지 않음(COM 블로킹 추정) — 스레드 강제 종료")
    worker.terminate()
    if worker.wait(_WAIT_AFTER_TERMINATE_MS):
        return

    # 강제 종료 후에도 잔존 → 프로세스 하드 종료(사용자가 의도한 종료이므로 0)
    logger.error("워커 스레드가 강제 종료 후에도 잔존 — 프로세스 하드 종료(os._exit)")
    hard_exit(0)
