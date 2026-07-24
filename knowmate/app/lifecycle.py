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


_DIRTY_MARKER_NAME = "dirty_shutdown.flag"


def _default_mark_dirty_shutdown() -> None:
    """강제 종료 직전에 표식 파일을 남긴다(설계 리뷰 10차 M-1).

    LanceDB add/delete/optimize가 진행 중일 수 있는 상태에서 워커 스레드를
    강제 종료하거나 프로세스를 하드 종료하면, 그 쓰기의 커밋 원자성은 배포
    lancedb 버전에 따라 미확정이다(추측성 자동 손상 감지·복구는 검증 불가능한
    상태에서 넣는 게 더 위험하다고 판단해 미구현 — docs/DESIGN.md § 종료 확실화
    참조). 대신 "강제 종료가 있었다"는 사실 자체는 저비용으로 기록해, 다음 실행
    시작 시 사용자에게 재인덱싱을 권장할 근거로 삼는다(check_and_clear_dirty_shutdown).
    표식 기록 자체의 실패(디스크 오류 등)는 종료를 막지 않도록 무시한다.
    """
    try:
        from knowmate.config import get_data_dir
        marker = get_data_dir() / _DIRTY_MARKER_NAME
        marker.write_text("1", encoding="utf-8")
    except OSError as exc:
        logger.debug("강제 종료 표식 기록 실패(무시): %s", exc)


def check_and_clear_dirty_shutdown(marker_path=None) -> bool:
    """이전 실행이 강제 종료됐는지 확인하고 표식을 지운다(read-then-clear — 다음
    시작 시 1회만 보고). 반환: 이전 실행에 강제 종료 표식이 있었으면 True.

    marker_path: 표식 파일 경로(테스트 주입용, 기본은 %APPDATA%/AegisDesk/dirty_shutdown.flag).
    """
    try:
        if marker_path is None:
            from knowmate.config import get_data_dir
            marker_path = get_data_dir() / _DIRTY_MARKER_NAME
        existed = marker_path.exists()
        if existed:
            marker_path.unlink(missing_ok=True)
        return existed
    except OSError as exc:
        logger.debug("강제 종료 표식 확인 실패(무시): %s", exc)
        return False


def _default_hard_exit(code: int = 0) -> None:
    """프로세스를 무조건·즉시 종료한다(마지막 수단 — 어떤 정리 코드도 먼저 실행하지 않음).

    이전에는 os._exit() 전에 logging.shutdown()으로 로그를 flush했지만, 이는 하드
    종료의 "무조건 종료" 불변식을 깰 수 있다: QThread.terminate()는 스레드를 임의
    지점에서 강제 중단하는데, 그 지점이 하필 logging 핸들러 락(RLock)을 쥔 채
    logger.info() 등을 호출하던 중이라면 그 락은 영원히 풀리지 않는다. 그러면 메인
    스레드의 logging.shutdown()이 그 락을 기다리며 영구 대기해, 최후 안전망이어야 할
    hard_exit 자체가 멈추는 모순이 생긴다(설계 리뷰 9차 B-1). 하드 종료 경로에서는
    로그 flush를 포기하고 os._exit()만 호출한다 — 로그 유실은 감수하되(정상/graceful
    종료 경로에서는 여전히 flush됨) "반드시 종료된다"는 최상위 불변식을 지킨다.
    """
    os._exit(code)


def stop_worker(worker, hard_exit=_default_hard_exit, mark_dirty=_default_mark_dirty_shutdown) -> None:
    """워커를 정상 종료 → 실패 시 강제 종료 → 그래도 실패 시 프로세스 하드 종료.

    worker: cancel()/isRunning()/wait(ms)->bool/terminate() 를 갖는 객체(또는 None).
    hard_exit: 최종 프로세스 종료 콜백(기본 os._exit 래퍼, 테스트 주입용).
    mark_dirty: 하드 종료 직전에 호출할 강제 종료 표식 콜백(테스트 주입용, 설계 리뷰 10차 M-1).
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
    mark_dirty()
    hard_exit(0)


def finalize_shutdown(
    worker, quit_fn, hard_exit=_default_hard_exit, mark_dirty=_default_mark_dirty_shutdown
) -> None:
    """종료 최종 판정 — quit_fn(워커 비실행 확인) 또는 hard_exit(실행 중·판정 불가) 중 정확히 하나.

    _shutdown()의 앞 단계(scheduler.stop/tray.hide/stop_worker)가 예외로 이탈하더라도 항상
    도달해야 하는 마지막 안전망(설계 A-0001). stop_worker()가 정상 반환했다면 워커는 이미
    멈춘 상태이지만, stop_worker() 자체가 예외를 던졌거나 isRunning() 조회 자체가 실패하면
    "판정 불가" 상태이므로 quit()만으로는 QThread가 잔존할 수 있어 보수적으로 hard_exit한다.

    worker: cancel()/isRunning()/wait(ms)->bool/terminate() 를 갖는 객체(또는 None).
    quit_fn: () -> None, 워커 비실행이 확인됐을 때 호출할 정상 종료 콜백(QApplication.quit 등).
    hard_exit: 최종 프로세스 종료 콜백(기본 os._exit 래퍼, 테스트 주입용).
    mark_dirty: 하드 종료 직전에 호출할 강제 종료 표식 콜백(테스트 주입용, 설계 리뷰 10차 M-1).
    """
    try:
        is_running = worker is not None and worker.isRunning()
    except Exception as exc:
        logger.warning("워커 실행 상태 조회 실패(판정 불가) — 보수적으로 하드 종료: %s", exc)
        mark_dirty()
        hard_exit(0)
        return

    if is_running:
        logger.error("최종 판정 시점에도 워커가 실행 중 — 프로세스 하드 종료(os._exit)")
        mark_dirty()
        hard_exit(0)
        return

    quit_fn()
