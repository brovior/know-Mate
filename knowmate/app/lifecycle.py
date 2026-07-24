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


def check_and_remark_dirty_shutdown(marker_path=None) -> bool:
    """앱 시작 시 **1회만** 호출한다. 이전 실행의 표식이 남아있으면(=지난 실행이
    정상 종료되지 못했으면) True를 반환하고, 이번 실행을 위한 새 표식을 남긴다.

    설계 리뷰 10차 M-1(강제 종료 감지) → 11차 B-1로 구현 방식을 수정: 표식을
    **hard-exit 직전이 아니라 시작 시점에 미리** 써 두고 **정상 종료(quit) 확정
    시에만** `clear_dirty_shutdown()`으로 지운다. 이러면 hard-exit 경로는 어떤
    파일 I/O도 거치지 않고 os._exit()만 호출해도(9차 B-1로 확립한 "하드 종료는
    무조건·즉시" 불변식 유지) 다음 시작 때 표식이 그대로 남아있는 것만으로 강제
    종료였음을 알 수 있다. (이전 설계는 hard-exit 직전에 동기 파일 쓰기를 했는데,
    파일시스템·백신·네트워크 드라이브에서 그 쓰기 자체가 블록되면 최후 안전망인
    하드 종료가 멈추는 모순이 있었다 — 리뷰11 B-1.)

    marker_path: 표식 파일 경로(테스트 주입용, 기본은 %APPDATA%/AegisDesk/dirty_shutdown.flag).
    표식 기록 자체의 실패(디스크 오류 등)는 무시한다 — 감지 기능 저하일 뿐 앱 동작을
    막아서는 안 된다.
    """
    try:
        if marker_path is None:
            from knowmate.config import get_data_dir
            marker_path = get_data_dir() / _DIRTY_MARKER_NAME
        was_dirty = marker_path.exists()
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("1", encoding="utf-8")
        return was_dirty
    except OSError as exc:
        logger.debug("강제 종료 표식 확인/기록 실패(무시): %s", exc)
        return False


_CLEAR_DIRTY_JOIN_TIMEOUT_SEC = 1.0


def clear_dirty_shutdown(marker_path=None) -> None:
    """`app.exec()`가 정상 반환한 뒤(main()의 정상 반환 경로)에만 호출해 표식을 지운다.
    quit() 요청 시점이 아니라 이벤트 루프가 실제로 반환을 완료한 뒤에 호출해야 한다 —
    quit()은 종료를 "요청"할 뿐 완료를 보장하지 않으므로, 요청 시점에 지우면 그 사이
    크래시가 나도 다음 시작 때 강제 종료를 탐지하지 못한다(설계 리뷰 13차 M-1).

    hard-exit 경로에서는 절대 호출하지 않는다 — 그래야 표식이 "정상 종료 못 함"의
    증거로 남는다. 삭제 자체는 별도 daemon 스레드에서 수행하고 **최대
    `_CLEAR_DIRTY_JOIN_TIMEOUT_SEC`초만 대기**한다(best-effort, 설계 리뷰 14차 M-2) —
    `%APPDATA%`가 네트워크로 리다이렉트된 로밍 프로필이거나 백신이 파일 삭제를
    지연시키면 `unlink()`가 오래 걸리거나 멈출 수 있는데, 무기한 daemon 스레드에만
    맡기면 이 함수 호출 직후 인터프리터가 곧바로 종료돼(main()의 마지막 단계) 스레드가
    실행되기도 전에 잘릴 수 있다(리뷰13 B-1 수정 이후 리뷰14 M-2가 지적). 반대로
    무기한 동기 대기는 종료 자체를 지연시킬 수 있으므로, 짧은 상한을 두고 그 안에
    끝나지 않으면 종료를 계속한다 — 표식이 남으면 다음 시작 때 오탐(강제 종료 아니었는데
    경고) 위험은 있지만, 이는 진단 정보의 정확도 저하일 뿐이라 종료 지연보다 우선순위가
    낮다.
    """
    def _do_clear(path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("강제 종료 표식 삭제 실패(무시): %s", exc)

    if marker_path is None:
        try:
            from knowmate.config import get_data_dir
            marker_path = get_data_dir() / _DIRTY_MARKER_NAME
        except OSError as exc:
            logger.debug("강제 종료 표식 경로 확인 실패(무시): %s", exc)
            return

    import threading
    t = threading.Thread(target=_do_clear, args=(marker_path,), daemon=True, name="clear-dirty-marker")
    t.start()
    t.join(_CLEAR_DIRTY_JOIN_TIMEOUT_SEC)


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


def stop_worker(worker, hard_exit=_default_hard_exit) -> bool:
    """워커를 정상 종료 → 실패 시 강제 종료 → 그래도 실패 시 프로세스 하드 종료.

    worker: cancel()/isRunning()/wait(ms)->bool/terminate() 를 갖는 객체(또는 None).
    hard_exit: 최종 프로세스 종료 콜백(기본 os._exit 래퍼, 테스트 주입용).

    반환값: `terminate()`가 이번 호출에서 사용됐는지(bool). 호출부(`finalize_shutdown`)가
    이 값을 `force_hard_exit`로 넘겨야 한다(설계 리뷰 15차 B-1) — `QThread.terminate()`는
    스레드를 임의 지점에서 강제 중단하므로, 그 결과 `isRunning()`이 False가 되더라도
    "정상 종료"로 취급할 수 없다. 강제 중단된 스레드가 로깅 핸들러 락·LanceDB 파일 락 등을
    쥔 채 사라졌을 수 있고(9차 B-1이 같은 이유로 하드 종료 경로에서 `logging.shutdown()`을
    제거한 근거와 동일), `terminate()` 이후 `quit()`으로 넘어가면 그 락이 정상 인터프리터
    종료를 블록하거나, `app.exec()`가 어쨌든 반환돼 dirty-shutdown marker가 지워지는
    false negative(실제로는 강제 중단이 있었는데 다음 시작에서 정상 종료로 오인)가 생긴다.
    따라서 `terminate()`가 한 번이라도 쓰이면 그 이후는 항상 하드 종료로 수렴시킨다.

    하드 종료 경로는 어떤 파일 I/O·정리 코드도 거치지 않고 hard_exit만 호출한다
    (설계 리뷰 9·11차 B-1 — "하드 종료는 무조건·즉시" 불변식). 강제 종료 감지는
    이 함수가 아니라 check_and_remark_dirty_shutdown/clear_dirty_shutdown이
    시작·정상종료 시점에 담당한다.
    """
    if worker is None or not worker.isRunning():
        return False

    worker.cancel()
    # 현재 처리 중인 파일 완료 후 정상 종료 대기
    if worker.wait(_WAIT_GRACEFUL_MS):
        return False

    # COM Open 등에 블로킹돼 취소 플래그를 못 본 상태 → 스레드 강제 종료
    logger.warning("워커가 제때 종료되지 않음(COM 블로킹 추정) — 스레드 강제 종료")
    worker.terminate()
    if worker.wait(_WAIT_AFTER_TERMINATE_MS):
        # terminate()가 스레드를 멈추는 데는 성공했지만, 임의 지점에서 강제 중단된 것이라
        # 락을 보유한 채 죽었을 가능성이 있어 "정상 종료"로 취급하지 않는다(리뷰15 B-1) —
        # 호출부가 이 반환값을 finalize_shutdown(force_hard_exit=True)로 넘겨야 한다.
        logger.error("워커 스레드가 강제 종료됨(terminate) — 락 보유 가능성으로 하드 종료 강제")
        return True

    # 강제 종료 후에도 잔존 → 프로세스 하드 종료(사용자가 의도한 종료이므로 0)
    logger.error("워커 스레드가 강제 종료 후에도 잔존 — 프로세스 하드 종료(os._exit)")
    hard_exit(0)
    return True


def finalize_shutdown(worker, quit_fn, hard_exit=_default_hard_exit, force_hard_exit: bool = False) -> None:
    """종료 최종 판정 — quit_fn(워커 비실행 확인) 또는 hard_exit(실행 중·판정 불가·강제중단됨) 중 정확히 하나.

    _shutdown()의 앞 단계(scheduler.stop/tray.hide/stop_worker)가 예외로 이탈하더라도 항상
    도달해야 하는 마지막 안전망(설계 A-0001). stop_worker()가 정상 반환했다면 워커는 이미
    멈춘 상태이지만, stop_worker() 자체가 예외를 던졌거나 isRunning() 조회 자체가 실패하면
    "판정 불가" 상태이므로 quit()만으로는 QThread가 잔존할 수 있어 보수적으로 hard_exit한다.

    force_hard_exit: `stop_worker()`가 `terminate()`를 사용했으면(반환값 True) 호출부가
    이 값을 그대로 넘겨야 한다(설계 리뷰 15차 B-1). `terminate()`는 스레드를 임의 지점에서
    강제 중단하므로 이후 `isRunning()`이 False가 되어도 "정상 종료"가 아니다 — 락을 쥔 채
    죽었을 수 있어 `quit()`으로 넘어가면 인터프리터 종료가 블록되거나, `app.exec()`가
    반환돼 dirty-shutdown marker가 false negative로 지워질 수 있다. 이 플래그가 True면
    `isRunning()` 값과 무관하게 항상 `hard_exit()`한다.

    강제 종료 표식(clear_dirty_shutdown) 해제는 **여기서 하지 않는다**(설계 리뷰 13차 M-1).
    `quit_fn()`(QApplication.quit)은 이벤트 루프 종료를 "요청"할 뿐 프로세스의 정상 종료
    완료를 보장하지 않는다 — 여기서 표식을 지운 뒤 이벤트 루프 반환 전에 네이티브
    크래시가 나면 다음 시작 때 강제 종료를 탐지하지 못하는 false negative가 생긴다.
    표식 해제는 `app.exec()`가 정상 반환한 뒤 `main()`의 정상 반환 경로에서만 수행한다
    (hard_exit 경로는 os._exit()로 app.exec()에 절대 반환하지 않으므로, "app.exec() 반환
    = 정상 quit 확정"이 성립한다).

    worker: cancel()/isRunning()/wait(ms)->bool/terminate() 를 갖는 객체(또는 None).
    quit_fn: () -> None, 워커 비실행이 확인됐을 때 호출할 정상 종료 콜백(QApplication.quit 등).
    hard_exit: 최종 프로세스 종료 콜백(기본 os._exit 래퍼, 테스트 주입용). **파일 I/O 없이**
        즉시 실행되어야 한다(리뷰11 B-1).
    """
    if force_hard_exit:
        logger.error("워커가 terminate()로 강제 중단됨 — 락 보유 가능성으로 정상 quit 대신 하드 종료")
        hard_exit(0)
        return

    try:
        is_running = worker is not None and worker.isRunning()
    except Exception as exc:
        logger.warning("워커 실행 상태 조회 실패(판정 불가) — 보수적으로 하드 종료: %s", exc)
        hard_exit(0)
        return

    if is_running:
        logger.error("최종 판정 시점에도 워커가 실행 중 — 프로세스 하드 종료(os._exit)")
        hard_exit(0)
        return

    quit_fn()
