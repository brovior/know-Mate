"""구형 바이너리 포맷(doc/xls/ppt) COM 싱글톤 파서 (CLAUDE.md 6-6).

win32com 없는 환경에서 import 시 ComUnavailableError를 발생시킨다.
fake 모드에서는 이 모듈을 import하지 않아야 한다.

COM STA 주의: COM 객체는 생성한 스레드에서만 사용 가능하다.
_ThreadLocalComApps를 통해 스레드별로 독립적인 COM 앱 인스턴스를 관리한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any, TYPE_CHECKING

from knowmate.secure import com_stage
from knowmate.secure.ppt_diagnostics import PptReadDiagnostics
from knowmate.secure.text_util import format_table

if TYPE_CHECKING:
    from knowmate.secure.office_guard import OfficeCleanupResult, OwnedOfficeProcess
    from knowmate.secure.ppt_operation import PptOperation

logger = logging.getLogger(__name__)

_MSO_GROUP = 6  # msoGroup — 그룹 도형 Type 값


class ComUnavailableError(RuntimeError):
    """win32com.client를 사용할 수 없는 환경에서 발생한다."""


# Office가 이미 죽었거나 RPC 연결이 끊겼음을 뜻하는 HRESULT만 여기에 둔다.
# 파일별 암호/손상 오류나 busy/retry 오류를 여기에 넣으면 정상 인스턴스까지
# 불필요하게 죽여 사용자 작업과 재사용성을 해칠 수 있다.
_FATAL_TRANSPORT_HRESULTS = frozenset({
    0x800706BA,  # RPC_S_SERVER_UNAVAILABLE
    0x800706BE,  # RPC_S_CALL_FAILED
    0x800706BF,  # RPC_S_CALL_FAILED_DNE
    0x80010006,  # RPC_E_CONNECTION_TERMINATED
    0x80010007,  # RPC_E_SERVER_DIED
    0x80010012,  # RPC_E_SERVER_DIED_DNE
    0x80010108,  # RPC_E_DISCONNECTED
    0x800401FD,  # CO_E_OBJNOTCONNECTED
})


def hresult_of(exc: BaseException) -> int | None:
    """예외의 HRESULT를 unsigned 32-bit 정수로 반환한다(없으면 None)."""
    value = getattr(exc, "hresult", None)
    if not isinstance(value, int):
        args = getattr(exc, "args", ())
        value = args[0] if args and isinstance(args[0], int) else None
    return (value & 0xFFFFFFFF) if isinstance(value, int) else None


def is_fatal_transport_hresult(hresult: int | None) -> bool:
    """HRESULT가 재사용 불가 Office RPC 전송 단절인지 판정한다."""
    return isinstance(hresult, int) and (hresult & 0xFFFFFFFF) in _FATAL_TRANSPORT_HRESULTS


class OfficeComPoisonError(RuntimeError):
    """Office RPC 연결이 끊겨 해당 COM 인스턴스를 즉시 폐기해야 할 때 발생한다."""

    def __init__(self, exe_name: str, hresult: int | None = None) -> None:
        self.exe_name = exe_name.upper()
        self.app_exe = self.exe_name
        self.hresult = (hresult & 0xFFFFFFFF) if isinstance(hresult, int) else None
        detail = f" (HRESULT=0x{self.hresult:08X})" if self.hresult is not None else ""
        super().__init__(f"{self.exe_name} COM RPC 연결이 끊어졌습니다{detail}")


def _poison_if_fatal(exc: BaseException, exe_name: str) -> None:
    """fatal transport HRESULT면 원본을 보존한 poison 예외를 발생시킨다."""
    if exe_name == "POWERPNT.EXE":
        from knowmate.secure.ppt_payload import fatal_hresult
        hresult = fatal_hresult(exc)
    else:
        hresult = hresult_of(exc)
    if is_fatal_transport_hresult(hresult):
        raise OfficeComPoisonError(exe_name, hresult) from exc


def _require_win32com():
    """win32com.client를 import하고 반환한다. 없으면 ComUnavailableError."""
    try:
        import win32com.client  # type: ignore
        return win32com.client
    except ImportError as exc:
        raise ComUnavailableError(
            "win32com.client를 import할 수 없습니다. "
            "Windows 환경에서 pywin32를 설치하거나 fake/plain 모드를 사용하세요."
        ) from exc


def _ensure_com_initialized() -> bool:
    """현재 스레드에 COM을 MTA로 초기화한다.

    워커 스레드(메시지 펌프 없음)에서 Office STA 서버를 호출하려면
    MTA가 필요하다. STA로 초기화하면 펌프 부재로 Open()이 무한 대기한다.
    """
    try:
        import pythoncom  # type: ignore
        # COINIT_MULTITHREADED — 메시지 펌프 불필요, COM이 RPC로 마샬링
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        return True
    except Exception:
        # 이미 다른 모드로 초기화됨(RPC_E_CHANGED_MODE) 등 → 그대로 진행
        return False


# 스레드별 COM 앱 인스턴스를 저장한다 (STA 요구사항 준수)
_tls = threading.local()


@dataclass
class _PendingOwnershipCleanup:
    """생성한 워커에서만 접근하는 미검증 COM 앱과 문서 참조."""

    app: Any
    document: Any = None


def _retain_unverified_app(exe: str, app: Any, document: Any = None) -> None:
    """소유를 확인 못한 COM 참조와 EXE 차단을 생성 워커에 보존한다."""
    from knowmate.secure.office_guard import begin_unverified_cleanup

    candidates = getattr(_tls, "ownership_cleanup", None)
    if candidates is None:
        candidates = _tls.ownership_cleanup = {}
    if exe not in candidates:
        begin_unverified_cleanup(exe)
    candidates[exe] = _PendingOwnershipCleanup(app, document)


def release_unverified_com_refs() -> None:
    """워커 종료 전에 COM 참조를 해제하되 미확인 EXE 차단 상태는 보존한다."""
    candidates = getattr(_tls, "ownership_cleanup", {})
    exes = tuple(candidates)
    candidates.clear()
    for exe in exes:
        logger.info(
            "[com] 미검증 COM 참조 해제: exe=%s quit_requested=False reason=unverified_ownership cleanup_gate_action=unchanged",
            exe,
        )


# COM 상수 (모달 다이얼로그 억제용)
_WD_ALERTS_NONE = 0          # wdAlertsNone
_MSO_SEC_FORCE_DISABLE = 3   # msoAutomationSecurityForceDisable (매크로 강제 비활성)
_XL_ALERTS_OFF = False
_XL_REPAIR_FILE = 1          # xlRepairFile — 손상 파일을 복구 모드로 연다(복구 확인창 대신)
_XL_UPDATE_LINKS_NEVER = 0   # 외부 링크를 갱신하지 않음(네트워크 대기·갱신 확인창 방지)

# 한 번의 COM 왕복으로 읽을 최대 행 수. 셀 단위로 읽으면 셀마다 프로세스 간 마샬링이
# 일어나 1000행×20열이면 왕복이 2만 번인데, Range 단위로 읽으면 블록당 1번이다.
# 시트 전체를 한 번에 올리지 않고 블록으로 나누는 건 대형 시트의 순간 메모리 때문.
# config `chunking.xlsx_block_rows`로 조정 가능하며, 이 값은 그 설정이 없거나
# 비정상(0·음수)일 때의 폴백이다.
_DEFAULT_XL_BLOCK_ROWS = 1000

# 암호 보호 문서용 더미 암호. 빈 문자열이나 미지정이면 Office가 **암호 입력창**을 띄우고,
# 백그라운드라 아무도 답할 수 없어 그대로 멈춘다(워치독 강제 종료 → 세이프모드 루프의
# 또 다른 진입점). 틀린 암호를 미리 주면 프롬프트 없이 즉시 실패하고, 보호되지 않은
# 문서에서는 이 인자가 무시된다.
_DUMMY_PASSWORD = "\x00aegisdesk-no-prompt"


def _com_missing():
    """지정하지 않을 COM 선택 인자용 sentinel(DISP_E_PARAMNOTFOUND).

    위치 인자로만 호출하므로(late binding에서 이름 인자는 신뢰할 수 없음) 중간의
    "관심 없는" 인자를 건너뛰려면 이 값이 필요하다. pythoncom import 실패 시
    None으로 폴백한다(비Windows — 어차피 이 경로는 실행되지 않는다).
    """
    try:
        import pythoncom  # type: ignore
        return pythoncom.Missing
    except ImportError:
        return None


def _normalize_range_values(values, n_rows: int, n_cols: int) -> tuple:
    """`Range.Value`의 반환값을 항상 (행, 열) 2차원 튜플로 정규화한다.

    pywin32는 범위 모양에 따라 다른 형태를 돌려주는 함정이 있다:
    - 1×1 범위 → **2차원 튜플이 아니라 스칼라 하나**
    - 그 외 → 튜플의 튜플(행 단위)

    1×N·N×1도 방어적으로 처리한다(드라이버·Office 버전에 따라 1차원으로 올 수 있어,
    그때 행/열 방향을 범위 모양(n_rows/n_cols)으로 복원한다). 여기서 잘못 펴면
    셀 값이 엉뚱한 행에 붙어 인덱스 내용이 조용히 오염되므로 명시적으로 다룬다.
    """
    if not isinstance(values, (tuple, list)):
        return ((values,),)  # 1×1 스칼라
    if not values:
        return ()
    if isinstance(values[0], (tuple, list)):
        return tuple(values)  # 이미 2차원
    # 1차원으로 온 경우 — 요청한 범위 모양으로 행/열 방향을 판단
    if n_rows == 1:
        return (tuple(values),)          # 1행 N열
    if n_cols == 1:
        return tuple((v,) for v in values)  # N행 1열
    # 모양을 알 수 없는 예외적 형태 — 한 행으로 취급(값 유실보다 낫다)
    return (tuple(values),)


def _close_quietly(timer: com_stage.StageTimer, obj: Any, method_name: str, *args) -> Exception | None:
    """obj의 닫기 메서드를 CLOSE 단계로 호출하고 실패 예외를 반환한다.

    호출자는 일반 닫기 오류는 기존 원본 예외를 유지한 채 무시할 수 있고, fatal
    RPC 단절만 Office poison으로 승격할 수 있다. 항상 `finally`에서 호출돼 오픈에
    성공한 문서는 일반 읽기 오류가 나도 닫기를 시도한다.
    """
    if obj is None:
        return None
    try:
        with timer.stage(com_stage.STAGE_CLOSE):
            getattr(obj, method_name)(*args)
    except Exception as exc:
        logger.debug("[com] 닫기 실패: error_type=%s HRESULT=%s", type(exc).__name__, hresult_of(exc))
        return exc
    return None


class _OfficeQuitWatchdog:
    """Bound one Office Quit call and clean only its captured owned processes."""

    def __init__(
        self, office_guard: ModuleType, exe: str,
        owned: dict[int, OwnedOfficeProcess], timeout_sec: float,
    ) -> None:
        self._guard = office_guard
        self._exe = exe
        self._owned = dict(owned)
        self._timeout = timeout_sec
        self._lock = threading.Lock()
        self._generation = 0
        self._active = False
        self._fired = False
        self._inflight = False
        self._completed = False
        self._result = None
        self._timer = None

    def arm(self) -> None:
        """Start a daemon timer for this app's Quit call."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._active = True
            timer = threading.Timer(self._timeout, lambda: self._fire(generation))
            timer.daemon = True
            self._timer = timer
        timer.start()

    def _fire(self, generation: int) -> None:
        """Claim this generation, then do process work outside the watchdog lock."""
        with self._lock:
            if not self._active or generation != self._generation:
                return
            self._active = False
            self._fired = True
            self._inflight = True
            self._guard.begin_shutdown_cleanup(self._exe)
        result = None
        try:
            logger.warning("[com] Office Quit 시간 초과 발화: exe=%s PID=%s", self._exe, sorted(self._owned))
            result = self._guard.cleanup_owned_processes(self._owned)
            self._guard._log_cleanup_result(self._exe, result, "Quit timeout")
        except Exception as exc:
            logger.error("[com] Quit timeout cleanup 예외: exe=%s error_type=%s", self._exe, type(exc).__name__)
        finally:
            self._guard.finish_shutdown_cleanup(self._exe)
            with self._lock:
                self._result = result
                self._inflight = False
                self._completed = True

    def disarm(self) -> tuple[bool, bool, OfficeCleanupResult | None]:
        """Cancel without joining and report whether timeout cleanup completed."""
        with self._lock:
            self._active = False
            self._generation += 1
            timer = self._timer
            self._timer = None
            fired, completed, result = self._fired, self._completed, self._result
        if timer is not None:
            timer.cancel()
        return fired, completed, result


def _dispatch_and_own(win32com, prog_id: str, exe_name: str):
    """앱별 방식으로 COM을 생성하고 검증된 프로세스 PID만 소유 등록한다.

    Word·Excel은 DispatchEx로 기존 ROT 객체 재접속을 피하고, MultiUse인
    PowerPoint는 Dispatch를 유지한다. 생성 전 PID baseline과 앱 HWND의 PID,
    실행 파일명이 모두 일치할 때만 AegisDesk 소유로 등록한다. PowerPoint는 최신
    OS 목록에서 외부 앱을 먼저 차단하고, 이전 소유 기록이 같은 경우 재접속한다.
    검증할 수 없으면
    사용자 Office일 가능성을 배제할 수 없으므로 안전하게 처리를 연기한다.

    Dispatch 직전에 Resiliency 표식을 지운다 — 이전 사이클에서 워치독이 강제
    종료한 흔적이 남아 있으면 이번 기동 때 "안전 모드로 시작할까요?" 프롬프트가
    뜨는데, 그 프롬프트는 Dispatch가 반환하기도 전에 떠서 DisplayAlerts 같은 앱
    수준 설정으로는 억제할 수 없다(강제 종료 ↔ 세이프모드 무한 루프의 고리).
    """
    from knowmate.secure.office_guard import (
        OfficeOwnershipProbeError, OfficeOwnershipCleanupError,
        OfficeCleanupPendingError, office_pids_live, register_owned_app,
        ensure_office_available, unverified_cleanup_pending,
        prepare_powerpoint_dispatch,
    )
    from knowmate.secure.office_resiliency import clear_resiliency_markers

    ensure_office_available(exe_name)
    if exe_name not in unverified_cleanup_pending():
        getattr(_tls, "ownership_cleanup", {}).pop(exe_name, None)
    clear_resiliency_markers(exe_name)
    expected_owned = None
    if exe_name == "POWERPNT.EXE":
        before, expected_owned = prepare_powerpoint_dispatch()
    else:
        before = office_pids_live(exe_name)
    dispatch = (
        win32com.Dispatch if exe_name == "POWERPNT.EXE"
        else getattr(win32com, "DispatchEx", win32com.Dispatch)
    )
    try:
        app = dispatch(prog_id)
    except Exception as exc:
        _poison_if_fatal(exc, exe_name)
        raise
    # PowerPoint는 MultiUse라 Dispatch가 기존 사용자 프로세스를 반환할 수 있다.
    # Windows에서 새 PID 또는 같은 기존 소유 기록과 HWND/exe를 확인하지 못하면 추측하지 않고
    # 이 자동화 요청 자체를 연기한다. 비Windows fake 객체는 office_guard의
    # platform fallback으로 허용돼 기존 단위 테스트를 유지한다.
    owned = False
    for attempt in range(3):
        try:
            if expected_owned is not None:
                owned = register_owned_app(exe_name, before, app, expected_owned)
            else:
                owned = register_owned_app(exe_name, before, app)
        except OfficeOwnershipCleanupError as exc:
            if exc.registered is None:
                _retain_unverified_app(exe_name, app, exc.document)
            logger.warning(
                "[com] 소유 확인용 문서 정리 실패; 등록 재시도 중단: exe=%s probe_error=%s close_error=%s",
                exe_name, type(exc.probe_error).__name__, type(exc.close_error).__name__,
            )
            for error in (exc.probe_error, exc.close_error, exc.restore_error):
                while error is not None:
                    if exe_name == "POWERPNT.EXE" and is_fatal_transport_hresult(hresult_of(error)):
                        _retain_unverified_app(exe_name, app, exc.document)
                    _poison_if_fatal(error, exe_name)
                    error = error.__cause__
            raise OfficeCleanupPendingError(
                exe_name, f"{exe_name} 소유 확인용 문서 종료 확인 대기 중",
            ) from exc
        except OfficeOwnershipProbeError as exc:
            original = exc.__cause__ if isinstance(exc.__cause__, BaseException) else exc
            error = exc
            while error is not None:
                if is_fatal_transport_hresult(hresult_of(error)):
                    _retain_unverified_app(exe_name, app, exc.document)
                _poison_if_fatal(error, exe_name)
                error = error.__cause__
            logger.warning(
                "[com] 소유권 확인 오류: exe=%s attempt=%d error_type=%s HRESULT=%s",
                exe_name, attempt + 1, type(original).__name__, hresult_of(original),
            )
        except Exception as exc:
            _retain_unverified_app(exe_name, app)
            _poison_if_fatal(exc, exe_name)
            raise
        if owned:
            break
        if attempt < 2:
            time.sleep(0.1)
    if not owned:
        _retain_unverified_app(exe_name, app)
        logger.warning("[com] 소유 등록 재시도 소진; 미검증 앱 정리 대기: exe=%s", exe_name)
        raise OfficeCleanupPendingError(exe_name, f"{exe_name} 소유권 확인 실패로 정리 대기 중")
    return app


def _configure_app(app: Any, exe_name: str, assignments: tuple[tuple[Any, str, Any], ...]) -> None:
    """초기 COM 설정을 적용하며 fatal RPC 오류만 호출자에게 전달한다."""
    for obj, attr, value in assignments:
        try:
            setattr(obj, attr, value)
        except Exception as exc:
            _poison_if_fatal(exc, exe_name)


def _configure_word_options(app: Any) -> None:
    """Word Options 접근의 일반 호환성 오류는 무시하고 poison만 전달한다."""
    try:
        _configure_app(app, "WINWORD.EXE", ((app.Options, "ConfirmConversions", False),))
    except Exception as exc:
        _poison_if_fatal(exc, "WINWORD.EXE")


def _get_word_app():
    """현재 스레드의 Word.Application COM 인스턴스를 반환한다."""
    from knowmate.secure.office_guard import ensure_office_available
    ensure_office_available("WINWORD.EXE")
    if not getattr(_tls, "word", None):
        _ensure_com_initialized()
        win32com = _require_win32com()
        app = _dispatch_and_own(win32com, "Word.Application", "WINWORD.EXE")
        # 모달 다이얼로그/매크로 경고/변환 확인창 억제
        _configure_app(app, "WINWORD.EXE", (
            (app, "Visible", False), (app, "DisplayAlerts", _WD_ALERTS_NONE),
            (app, "AutomationSecurity", _MSO_SEC_FORCE_DISABLE),
        ))
        _configure_word_options(app)
        _tls.word = app
    return _tls.word


def _get_excel_app():
    """현재 스레드의 Excel.Application COM 인스턴스를 반환한다."""
    from knowmate.secure.office_guard import ensure_office_available
    ensure_office_available("EXCEL.EXE")
    if not getattr(_tls, "excel", None):
        _ensure_com_initialized()
        win32com = _require_win32com()
        app = _dispatch_and_own(win32com, "Excel.Application", "EXCEL.EXE")
        _configure_app(app, "EXCEL.EXE", (
            (app, "Visible", False), (app, "DisplayAlerts", _XL_ALERTS_OFF),
            (app, "AutomationSecurity", _MSO_SEC_FORCE_DISABLE),
            (app, "AskToUpdateLinks", False),
        ))
        _tls.excel = app
    return _tls.excel


def _get_ppt_app():
    """현재 스레드의 PowerPoint.Application COM 인스턴스를 반환한다."""
    if not getattr(_tls, "ppt", None):
        _ensure_com_initialized()
        win32com = _require_win32com()
        app = _dispatch_and_own(win32com, "PowerPoint.Application", "POWERPNT.EXE")
        _configure_app(app, "POWERPNT.EXE", (
            (app, "DisplayAlerts", 1),  # ppAlertsNone 계열 (버전별 차이)
            (app, "AutomationSecurity", _MSO_SEC_FORCE_DISABLE),
        ))
        _tls.ppt = app
    return _tls.ppt


class WordComReader:
    """doc 파일을 COM(Word)으로 파싱하는 리더. 스레드별 싱글톤을 사용한다."""

    def parse(self, path: str) -> str:
        """doc 파일을 열어 본문 텍스트를 반환한다.

        단계(dispatch/open/read/close)별 소요시간을 계측해 워치독·소비자 로그와
        연계한다 — 어느 단계에서 멈췄는지 로그 한 줄로 알 수 있어야 한다는 요구
        (COM 처리 안정화). 문서 오픈에 성공했다면 그 뒤 어느 단계에서 예외가
        나도 `finally`에서 반드시 `Close`를 시도한다.
        """
        timer = com_stage.StageTimer(path)
        doc = None
        poison: OfficeComPoisonError | None = None
        primary_error: Exception | None = None
        try:
            with timer.stage(com_stage.STAGE_DISPATCH):
                word = _get_word_app()
            # 모든 모달 프롬프트를 사전 차단한다 — 백그라운드라 아무도 답할 수 없어
            # 프롬프트 하나가 그대로 행오버가 되고, 워치독 강제 종료 → 세이프모드 표식
            # → 다음 기동 때 또 프롬프트로 이어지는 루프의 시작점이 된다.
            # 이름 인자 대신 위치 인자로 넘긴다(late binding에서 이름 인자는 신뢰 불가).
            _m = _com_missing()
            with timer.stage(com_stage.STAGE_OPEN):
                doc = word.Documents.Open(
                    str(Path(path).resolve()),
                    False,            # ConfirmConversions — 변환 확인창 억제
                    True,             # ReadOnly
                    False,            # AddToRecentFiles — 사용자 최근 문서 목록 오염 방지
                    _DUMMY_PASSWORD,  # PasswordDocument — 암호 입력창 대신 즉시 실패
                    _DUMMY_PASSWORD,  # PasswordTemplate
                    False,            # Revert
                    _DUMMY_PASSWORD,  # WritePasswordDocument
                    _DUMMY_PASSWORD,  # WritePasswordTemplate
                    _m,               # Format
                    _m,               # Encoding
                    False,            # Visible
                    True,             # OpenAndRepair — 손상 문서를 복구 확인창 없이 연다
                    _m,               # DocumentDirection
                    True,             # NoEncodingDialog — 인코딩 선택창 억제(구형 .doc 단골 블로커)
                )
                if doc is None:
                    raise OfficeComPoisonError("WINWORD.EXE")
            with timer.stage(com_stage.STAGE_READ):
                text = doc.Content.Text
            return text
        except OfficeComPoisonError as exc:
            poison = exc
            _tls.word = None
            raise
        except Exception as exc:
            primary_error = exc
            try:
                _poison_if_fatal(exc, "WINWORD.EXE")
            except OfficeComPoisonError as poison_exc:
                poison = poison_exc
                _tls.word = None
                raise
            raise
        finally:
            # RPC 단절 뒤 Close도 다시 RPC에 매달릴 수 있어 scheduler의 즉시 복구로
            # 넘긴다. 일반 문서 오류는 기존처럼 Close를 유지한다.
            try:
                close_error = None if poison is not None else _close_quietly(timer, doc, "Close", False)
                if close_error is not None:
                    try:
                        _poison_if_fatal(close_error, "WINWORD.EXE")
                    except OfficeComPoisonError as close_poison:
                        _tls.word = None
                        if primary_error is not None:
                            raise close_poison from primary_error
                        raise
            finally:
                com_stage.clear()
                timer.log_summary()


class ExcelComReader:
    """xls 파일을 COM(Excel)으로 파싱하는 리더. 스레드별 싱글톤을 사용한다."""

    def __init__(self, block_rows: int | None = None) -> None:
        """block_rows: 한 번의 COM 왕복으로 읽을 행 수(config `chunking.xlsx_block_rows`).

        None·0·음수 같은 비정상 값은 조용히 기본값으로 폴백한다 — config는 사용자가
        직접 편집할 수 있어, 잘못된 값 하나로 인덱싱 전체가 죽으면 안 된다(fail-safe).
        """
        self._block_rows = (
            _DEFAULT_XL_BLOCK_ROWS
            if not isinstance(block_rows, int) or isinstance(block_rows, bool) or block_rows < 1
            else block_rows
        )

    def _read_sheet_lines(self, sheet) -> list[str]:
        """시트 하나를 **범위 단위**로 읽어 탭 구분 텍스트 줄 리스트를 반환한다.

        이전에는 셀마다 `cell.Value`로 COM 왕복을 했는데(1000행×20열이면 2만 번),
        `Range.Value`는 지정한 사각 범위를 왕복 1번으로 가져온다. 시트가 커도 순간
        메모리가 튀지 않도록 `self._block_rows` 행씩 나눠 읽는다.

        출력 포맷은 `plain_reader`의 openpyxl·xlrd 경로와 **동일해야** 한다
        (탭 구분, 빈 행 스킵) — 세 경로가 같은 인덱스에 들어가므로 포맷이 갈리면
        검색 품질이 경로에 따라 달라진다.
        """
        used = sheet.UsedRange
        # UsedRange는 A1에서 시작한다는 보장이 없다(예: C5부터 데이터가 있는 시트).
        first_row = int(used.Row)
        first_col = int(used.Column)
        n_rows = int(used.Rows.Count)
        n_cols = int(used.Columns.Count)

        sheet_lines: list[str] = []
        for start in range(0, n_rows, self._block_rows):
            block_rows = min(self._block_rows, n_rows - start)
            r1 = first_row + start
            r2 = r1 + block_rows - 1
            c1 = first_col
            c2 = first_col + n_cols - 1
            values = sheet.Range(sheet.Cells(r1, c1), sheet.Cells(r2, c2)).Value
            for row_values in _normalize_range_values(values, block_rows, n_cols):
                row_text = "\t".join(str(v) if v is not None else "" for v in row_values)
                if row_text.strip():
                    sheet_lines.append(row_text)
        return sheet_lines

    def parse(self, path: str) -> str:
        """xls 파일을 열어 시트 전체를 탭 구분 텍스트로 반환한다.

        단계(dispatch/open/sheets/cell_read/close)별 소요시간을 계측한다 — DRM
        문서 등에서 어느 단계가 hang의 원인인지(Open 자체인지, 셀 읽기인지)를
        로그 한 줄로 구분할 수 있어야 한다는 요구(COM 처리 안정화). 시트 목록
        조회(sheets)와 셀 읽기(cell_read)를 별도 단계로 나누기 위해, `wb.Sheets`를
        먼저 리스트로 materialize한 뒤 셀 읽기를 시작한다. 셀 읽기 자체는
        `_read_sheet_lines`가 범위 단위(블록)로 처리한다.
        """
        timer = com_stage.StageTimer(path)
        wb = None
        poison: OfficeComPoisonError | None = None
        primary_error: Exception | None = None
        try:
            with timer.stage(com_stage.STAGE_DISPATCH):
                excel = _get_excel_app()
            # Word와 같은 이유로 모든 모달 프롬프트를 사전 차단한다(위 주석 참조).
            # 특히 CorruptLoad=xlRepairFile은 "파일이 손상됐습니다. 복구할까요?" 확인창을
            # 없애는데, 이 확인창은 앱 수준 DisplayAlerts=False로도 억제되지 않는다.
            _m = _com_missing()
            with timer.stage(com_stage.STAGE_OPEN):
                wb = excel.Workbooks.Open(
                    str(Path(path).resolve()),
                    _XL_UPDATE_LINKS_NEVER,  # UpdateLinks
                    True,                    # ReadOnly
                    _m,                      # Format
                    _DUMMY_PASSWORD,         # Password — 암호 입력창 대신 즉시 실패
                    _DUMMY_PASSWORD,         # WriteResPassword
                    True,                    # IgnoreReadOnlyRecommended — 읽기전용 권장 창 억제
                    _m,                      # Origin
                    _m,                      # Delimiter
                    _m,                      # Editable
                    False,                   # Notify — 잠긴 파일을 대기하지 않고 즉시 실패
                    _m,                      # Converter
                    False,                   # AddToMru — 사용자 최근 문서 목록 오염 방지
                    _m,                      # Local
                    _XL_REPAIR_FILE,         # CorruptLoad — 손상 파일을 복구 확인창 없이 연다
                )
                if wb is None:
                    raise OfficeComPoisonError("EXCEL.EXE")
            with timer.stage(com_stage.STAGE_SHEETS):
                sheets = list(wb.Sheets)
            lines: list[str] = []
            with timer.stage(com_stage.STAGE_CELL_READ):
                for sheet in sheets:
                    sheet_lines = self._read_sheet_lines(sheet)
                    if sheet_lines:
                        lines.append(f"=== 시트: {sheet.Name} ===")
                        lines.extend(sheet_lines)
            return "\n".join(lines)
        except OfficeComPoisonError as exc:
            poison = exc
            _tls.excel = None
            raise
        except Exception as exc:
            primary_error = exc
            try:
                _poison_if_fatal(exc, "EXCEL.EXE")
            except OfficeComPoisonError as poison_exc:
                poison = poison_exc
                _tls.excel = None
                raise
            raise
        finally:
            try:
                close_error = None if poison is not None else _close_quietly(timer, wb, "Close", False)
                if close_error is not None:
                    try:
                        _poison_if_fatal(close_error, "EXCEL.EXE")
                    except OfficeComPoisonError as close_poison:
                        _tls.excel = None
                        if primary_error is not None:
                            raise close_poison from primary_error
                        raise
            finally:
                com_stage.clear()
                timer.log_summary()


def _ppt_shape_texts(
    shape: Any, diagnostics: PptReadDiagnostics | None = None,
    slide: int = 0, position: tuple[int, ...] = (),
) -> list[str]:
    """그룹·표·텍스트 추출 순서를 유지하며 기존 COM 읽기를 계측한다."""
    def read(operation, action, row=0, column=0):
        if diagnostics is None:
            return action()
        return diagnostics.read(operation, action, slide, position, row, column)

    started = time.monotonic()
    kind = "other"
    shape_type = -1
    rows = columns = 0
    if diagnostics is not None:
        diagnostics.shape_started(slide, position)
    try:
        try:
            shape_type = read("type", lambda: shape.Type)
            if shape_type == _MSO_GROUP:
                kind = "group"
                if diagnostics is not None:
                    diagnostics.count(slide, "groups")
                children = read("group_items", lambda: shape.GroupItems)
                items = (
                    enumerate(children, 1) if diagnostics is None
                    else diagnostics.items(children, "group", slide, position)
                )
                out: list[str] = []
                for index, child in items:
                    out.extend(_ppt_shape_texts(child, diagnostics, slide, position + (index,)))
                return out
        except Exception as exc:
            _poison_if_fatal(exc, "POWERPNT.EXE")

        try:
            if read("has_table", lambda: shape.HasTable):
                kind = "table"
                if diagnostics is not None:
                    diagnostics.count(slide, "tables")
                table = read("table", lambda: shape.Table)
                rows = read("table_rows", lambda: table.Rows.Count)
                columns = read("table_columns", lambda: table.Columns.Count) if rows else 0
                grid: list[list[str]] = []
                for row in range(1, rows + 1):
                    cells: list[str] = []
                    for column in range(1, columns + 1):
                        if diagnostics is not None:
                            diagnostics.count(slide, "cells")
                        cells.append(read(
                            "cell_text", lambda: table.Cell(row, column).Shape.TextFrame.TextRange.Text,
                            row, column,
                        ))
                    grid.append(cells)
                table_text = format_table(grid)
                return [table_text] if table_text else []
        except Exception as exc:
            _poison_if_fatal(exc, "POWERPNT.EXE")

        try:
            if read("has_text_frame", lambda: shape.HasTextFrame):
                kind = "text"
                if diagnostics is not None:
                    diagnostics.count(slide, "text_frames")
                text = read("text", lambda: shape.TextFrame.TextRange.Text).strip()
                if text:
                    return [text]
        except Exception as exc:
            _poison_if_fatal(exc, "POWERPNT.EXE")
        return []
    finally:
        if diagnostics is not None:
            diagnostics.shape_finished(
                slide, position, kind, started, rows, columns,
                shape_type if isinstance(shape_type, int) else -1,
            )


class PowerPointComReader:
    """Read a PPT once, using a compatible add-in or a safe single COM fallback."""

    def __init__(self, mode: str | None = None, policy: str = "ready",
                 operation: PptOperation | None = None) -> None:
        """Receive mode, preparation policy and file-scoped checks from the caller."""
        from knowmate.config import ppt_reader_mode
        self.mode = ppt_reader_mode({} if mode is None else {"ppt_reader_mode": mode})
        self.policy = policy
        self.operation = operation

    def _check(self, stage: str = "read") -> None:
        if self.operation is not None:
            from knowmate.secure.ppt_operation import PptOperationError
            try:
                self.operation.check()
            except PptOperationError as exc:
                exc.stage = com_stage.snapshot()[0] or stage
                raise

    def _read_com(self, prs: Any, path: str, timer: com_stage.StageTimer) -> str:
        """Preserve production formatting and reject previously swallowed shape failures."""
        from knowmate.secure.ppt_addin_reader import PptPartialRead
        slides: list[str] = []
        count = 0
        slowest = 0.0
        with PptReadDiagnostics(path) as diagnostics:
            self._check()
            collection = diagnostics.read("slides", lambda: prs.Slides, 0)
            for index, slide in diagnostics.items(collection, "slides", 0):
                self._check()
                started = time.monotonic()
                texts = []
                try:
                    shapes = diagnostics.read("shapes", lambda: slide.Shapes, index)
                    for position, shape in diagnostics.items(shapes, "shapes", index):
                        self._check()
                        texts.extend(_ppt_shape_texts(shape, diagnostics, index, (position,)))
                        self._check()
                finally:
                    slowest = max(slowest, time.monotonic() - started)
                    diagnostics.slide_finished(index)
                self._check()
                count += 1
                if any(t.strip() for t in texts):
                    slides.append("\n".join(t for t in texts if t.strip()))
            if diagnostics.error_count:
                raise PptPartialRead("partial_com_read")
        timer.note("슬라이드", count)
        timer.note("최장슬라이드", slowest)
        return "\n\n".join(slides)

    def parse(self, path: str) -> str:
        """Hold the machine read lock through dispatch, extraction and confirmed close."""
        from knowmate.secure.ppt_addin_reader import PptAddinUnavailable, select_service, read_slides, REQUIRED_VERSION, PROTOCOL
        from knowmate.secure.ppt_operation import PptOperationError, PptPreparationPending
        from knowmate.secure.ppt_installation import MachineReadGuard
        from knowmate.secure.office_guard import OfficeBusyError, OfficeCleanupPendingError
        if self.policy == "ppt_pending":
            raise PptPreparationPending("preparation_pending")
        timer = com_stage.StageTimer(path)
        prs = None
        poison = False
        read_ok = False
        close_ok = False
        chosen = "com"
        fallback = "none"
        try:
            with MachineReadGuard():
                try:
                    self._check("dispatch")
                    with timer.stage(com_stage.STAGE_DISPATCH):
                        ppt = _get_ppt_app()
                    self._check("dispatch")
                    service = None
                    if self.mode != "com" and self.policy != "com_only":
                        try:
                            service = select_service(ppt, _tls)
                        except PptAddinUnavailable as exc:
                            fallback = str(exc)
                            if self.mode == "addin":
                                raise PptPreparationPending(fallback) from None
                    elif self.mode == "addin":
                        raise PptPreparationPending("service_not_prepared")
                    self._check("dispatch")
                    with timer.stage(com_stage.STAGE_OPEN):
                        prs = ppt.Presentations.Open(str(Path(path).resolve()), ReadOnly=True, WithWindow=False)
                        if prs is None:
                            raise OfficeComPoisonError("POWERPNT.EXE")
                    self._check("open")
                    with timer.stage(com_stage.STAGE_READ):
                        if service is not None:
                            chosen = "addin"
                            try:
                                text = read_slides(prs, path, service, self._check,
                                                   note_count=lambda count: timer.note("슬라이드", count))
                            except (OfficeComPoisonError, PptOperationError, PptPreparationPending,
                                    OfficeBusyError, OfficeCleanupPendingError):
                                raise
                            except Exception as exc:
                                _poison_if_fatal(exc, "POWERPNT.EXE")
                                self._check()
                                if self.mode == "addin":
                                    raise
                                fallback = "addin_read_failed"
                                chosen = "com"
                                # A new result buffer; the add-in's partial text is never retained.
                                text = self._read_com(prs, path, timer)
                        else:
                            text = self._read_com(prs, path, timer)
                    self._check()
                    read_ok = True
                except OfficeComPoisonError:
                    poison = True
                    _tls.ppt = None
                    _tls.ppt_service = None
                    raise
                except Exception as exc:
                    try:
                        _poison_if_fatal(exc, "POWERPNT.EXE")
                    except OfficeComPoisonError:
                        poison = True
                        _tls.ppt = None
                        _tls.ppt_service = None
                        raise
                    from knowmate.secure.ppt_payload import PptPayloadError
                    if isinstance(exc, (PptOperationError, PptPreparationPending, PptPayloadError,
                                        OfficeBusyError, OfficeCleanupPendingError)):
                        raise
                    # COM descriptions can include document text. Preserve only
                    # the numeric code for collector classification and logs.
                    safe = PptPayloadError("ppt_read_failed")
                    safe.hresult = hresult_of(exc)
                    raise safe from None
                finally:
                    # Fatal transport failures must not initiate another blocking RPC Close.
                    error = None if poison else _close_quietly(timer, prs, "Close")
                    close_ok = prs is not None and not poison and error is None
                    if self.operation is not None:
                        self.operation.finish(close_ok, read_ok)
                    if error is not None:
                        try:
                            _poison_if_fatal(error, "POWERPNT.EXE")
                        except OfficeComPoisonError:
                            _tls.ppt = None
                            _tls.ppt_service = None
                            raise
                        raise PptOperationError("close_unconfirmed") from None
                    if read_ok:
                        self._check("close")
                        if self.operation is not None:
                            self.operation.validate_commit()
                logger.info("[ppt] 운영 읽기: method=%s fallback=%s version=%s protocol=%d characters=%d <%s>",
                            chosen, fallback, REQUIRED_VERSION if chosen == "addin" else "none", PROTOCOL if chosen == "addin" else 0,
                            len(text), path)
                return text
        finally:
            com_stage.clear()
            timer.log_summary()




def quit_com_apps(
    grace_sec: float = 5.0, wait_fn=None, quit_timeout_sec: float | None = None,
) -> OfficeCleanupResult:
    """현재 스레드의 Word/Excel 앱은 Quit하고 모든 COM 참조를 비운다.

    COM 객체는 생성한 스레드에서만 Quit할 수 있으므로(STA),
    반드시 COM 앱을 생성한 워커 스레드 내부에서 호출해야 한다.
    PowerPoint는 MultiUse라 사용자 창일 가능성을 배제할 수 없어 Quit·강제 종료하지
    않고 참조 해제와 gc에 맡긴다. 검증 소유 Word/Excel 프로세스만 정리한다.

    Quit 자체가 멈추면 별도 daemon 타이머가 캡처한 Word/Excel 프로세스만 정리한다.
    반환 후에는 grace_sec 동안 자연 종료를 기다린다(0이면 유예 생략). 타이머가
    이미 정리를 시작한 대상에는 워커가 중복 종료를 요청하지 않는다. 실제 종료를
    확인하지 못한 소유 기록은 남겨 다음 COM 진입에서 제한적으로 재정리한다.
    Quit 전후 GC는 순환 참조에 남은 COM 래퍼 해제를 돕는다.

    quit_timeout_sec=None이면 배포 설정의 기본값을 사용한다. 잘못된 제한값도
    같은 기본값으로 복구한다. wait_fn은 자연 종료 대기 함수의 테스트 주입용이다.
    """
    import gc
    from knowmate.secure import office_guard
    from knowmate.config import com_quit_call_timeout_seconds
    quit_timeout_sec = com_quit_call_timeout_seconds(
        {} if quit_timeout_sec is None else {"com_quit_call_timeout_sec": quit_timeout_sec}
    )
    office_guard.prune_unverified_cleanup()
    owned = office_guard.begin_owned_cleanup()
    results: list[OfficeCleanupResult] = []
    timed_out_pids: set[int] = set()
    gc.collect()
    for attr, exe in (("word", "WINWORD.EXE"), ("excel", "EXCEL.EXE"), ("ppt", "POWERPNT.EXE")):
        app = getattr(_tls, attr, None)
        if app is None:
            continue
        watchdog = None
        try:
            if attr != "ppt":
                snapshot = {pid: rec for pid, rec in owned.items() if rec.exe == exe}
                terminable = {pid: record for pid, record in snapshot.items() if record.terminable}
                if terminable:
                    watchdog = _OfficeQuitWatchdog(office_guard, exe, terminable, quit_timeout_sec)
                    watchdog.arm()
                try:
                    app.Quit()
                    logger.info("[com] Office Quit 반환: exe=%s", exe)
                except Exception as exc:
                    logger.warning("[com] Office Quit 예외: exe=%s error_type=%s", exe, type(exc).__name__)
                finally:
                    if watchdog is not None:
                        fired, completed, result = watchdog.disarm()
                        if fired:
                            timed_out_pids.update(terminable)
                            logger.info(
                                "[com] Quit timeout 상태: exe=%s callback_completed=%s",
                                exe, completed,
                            )
                            # cancel()는 이미 실행 중인 콜백을 멈추지 못한다. 콜백이
                            # 기록을 먼저 지워도 in-flight gate가 새 COM 진입을 막으며,
                            # 이번 호출은 정리 완료 전까지 성공을 보고하지 않는다.
                            if completed and result is not None:
                                results.append(result)
                            elif completed:
                                results.append(office_guard.OfficeCleanupResult(failed=frozenset(terminable)))
                            else:
                                results.append(office_guard.OfficeCleanupResult(remaining=frozenset(terminable)))
            else:
                logger.info(
                    "[com] PowerPoint 종료 정책: action=release_reference quit_requested=False reason=multiuse_protection",
                )
        finally:
            # Quit 예외에도 스레드 로컬 COM 참조를 반드시 놓는다.
            setattr(_tls, attr, None)
            if attr == "ppt":
                _tls.ppt_service = None
            app = None
    # PowerPoint 참조만 해제되고 Quit/강제종료되지 않는다.
    gc.collect()
    office_guard.prune_released_nonterminable_processes()
    terminable = {
        pid: record for pid, record in owned.items()
        if record.terminable and pid not in timed_out_pids
    }
    if wait_fn is None:
        wait_fn = office_guard.wait_for_owned_exit
    try:
        still_alive, elapsed = (
            wait_fn(terminable, grace_sec) if terminable and grace_sec > 0
            else (set(terminable), 0.0)
        )
        unresolved = set(still_alive) & set(terminable)
        office_guard.finish_owned_cleanup(terminable, unresolved)
        observed_exited = set(terminable) - unresolved
        if unresolved:
            logger.warning("[com] Office Quit 후 종료 미확인: PID=%s grace=%.1fs", sorted(unresolved), grace_sec)
            result = office_guard.cleanup_owned_processes(
                {pid: terminable[pid] for pid in unresolved},
            )
            office_guard._log_cleanup_result("WORD/EXCEL", result, "grace cleanup")
            cleanup_result = office_guard.OfficeCleanupResult(
                confirmed_exited=frozenset(set(result.confirmed_exited) | observed_exited),
                remaining=result.remaining,
                identity_unknown=result.identity_unknown,
                failed=result.failed,
                forced_exited=result.forced_exited,
            )
        else:
            cleanup_result = office_guard.OfficeCleanupResult(
                confirmed_exited=frozenset(observed_exited),
            )
            if observed_exited:
                logger.info("[com] Office 정상 종료 확인: count=%d wait=%.1fs", len(observed_exited), elapsed)
    except Exception as exc:
        logger.error("[com] Office 종료 정리 실패; 소유 기록 보존: error_type=%s", type(exc).__name__)
        cleanup_result = office_guard.OfficeCleanupResult(
            failed=frozenset(terminable),
        )
    results.append(cleanup_result)
    return office_guard.OfficeCleanupResult(
        confirmed_exited=frozenset().union(*(item.confirmed_exited for item in results)),
        remaining=frozenset().union(*(item.remaining for item in results)),
        identity_unknown=frozenset().union(*(item.identity_unknown for item in results)),
        failed=frozenset().union(*(item.failed for item in results)),
        forced_exited=frozenset().union(*(item.forced_exited for item in results)),
        ownership_pending=office_guard.unverified_cleanup_pending(),
    )


class ComReader:
    """확장자를 보고 Word/Excel/PowerPoint COM 리더로 라우팅하는 TextExtractor 구현체."""

    def __init__(self, xlsx_block_rows: int | None = None, *, ppt_reader_mode: str | None = None,
                 ppt_policy: str = "ready", ppt_operation: PptOperation | None = None) -> None:
        """xlsx_block_rows: Excel 범위 읽기 블록 크기(config `chunking.xlsx_block_rows`).

        리더 3개를 인스턴스 속성으로 보유한다(이전에는 모듈 레벨 싱글톤). 리더 객체는
        무상태라 인스턴스화 비용이 없고, 진짜 재사용 대상인 COM 앱은 `_tls`에 따로
        보관되므로 싱글톤을 없애도 Office 프로세스 재사용에는 영향이 없다.
        """
        self._word = WordComReader()
        self._excel = ExcelComReader(block_rows=xlsx_block_rows)
        self._ppt = PowerPointComReader(ppt_reader_mode, ppt_policy, ppt_operation)

    def extract(self, path: str) -> str:
        """확장자에 따라 적합한 COM 리더로 파일을 파싱해 텍스트를 반환한다.

        OLE2 오라벨 파일(.docx/.xlsx/.pptx인데 실제 구형 바이너리)도 같은 앱으로
        라우팅한다. COM 앱은 확장자와 무관하게 실제 포맷을 열기 때문이다.
        """
        ext = Path(path).suffix.lower()
        if ext in (".doc", ".docx"):
            return self._word.parse(path)
        if ext in (".xls", ".xlsx"):
            return self._excel.parse(path)
        if ext in (".ppt", ".pptx"):
            return self._ppt.parse(path)
        raise ValueError(f"ComReader가 지원하지 않는 확장자: {ext!r} ({path})")
