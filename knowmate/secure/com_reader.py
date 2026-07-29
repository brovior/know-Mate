"""구형 바이너리 포맷(doc/xls/ppt) COM 싱글톤 파서 (CLAUDE.md 6-6).

win32com 없는 환경에서 import 시 ComUnavailableError를 발생시킨다.
fake 모드에서는 이 모듈을 import하지 않아야 한다.

COM STA 주의: COM 객체는 생성한 스레드에서만 사용 가능하다.
_ThreadLocalComApps를 통해 스레드별로 독립적인 COM 앱 인스턴스를 관리한다.
"""
import logging
import threading
from pathlib import Path

from knowmate.secure import com_stage
from knowmate.secure.text_util import format_table

logger = logging.getLogger(__name__)

_MSO_GROUP = 6  # msoGroup — 그룹 도형 Type 값


class ComUnavailableError(RuntimeError):
    """win32com.client를 사용할 수 없는 환경에서 발생한다."""


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


def _close_quietly(timer: com_stage.StageTimer, obj, method_name: str, *args) -> None:
    """obj가 None이 아니면 method_name(*args)를 CLOSE 단계로 계측하며 호출한다.

    닫기 자체의 예외는 로그만 남기고 삼킨다 — 이미 있는 원본 예외(예: 셀 순회 중
    실패)를 덮어쓰지 않고, 닫기 실패로 사이클 전체를 막지 않기 위함이다. 항상
    `finally`에서 호출돼 **오픈에 성공한 문서는 예외가 나도 반드시 닫히도록** 한다
    (이전에는 정상 경로에서만 Close가 호출돼, 셀 읽기 중 예외가 나면 워크북이
    열린 채 남았다).
    """
    if obj is None:
        return
    try:
        with timer.stage(com_stage.STAGE_CLOSE):
            getattr(obj, method_name)(*args)
    except Exception as exc:
        logger.debug("[com] 닫기 실패(무시): %s", exc)


def _dispatch_and_own(win32com, prog_id: str, exe_name: str):
    """COM 앱을 Dispatch하고, 그로 인해 새로 뜬 프로세스 PID를 우리 소유로 등록한다.

    Dispatch 전후의 해당 exe PID를 비교해 '우리가 띄운' 인스턴스를 식별한다.
    이렇게 등록해 두면 office_guard가 우리 자신을 점유로 오판하지 않는다
    (자기 감지 스킵 방지). 사용자가 이미 열어둔 인스턴스에 붙은 경우엔 새
    프로세스가 없어 아무것도 등록되지 않는다(그 경로는 가드가 먼저 차단).

    Dispatch 직전에 Resiliency 표식을 지운다 — 이전 사이클에서 워치독이 강제
    종료한 흔적이 남아 있으면 이번 기동 때 "안전 모드로 시작할까요?" 프롬프트가
    뜨는데, 그 프롬프트는 Dispatch가 반환하기도 전에 떠서 DisplayAlerts 같은 앱
    수준 설정으로는 억제할 수 없다(강제 종료 ↔ 세이프모드 무한 루프의 고리).
    """
    from knowmate.secure.office_guard import office_pids_live, register_owned_pids
    from knowmate.secure.office_resiliency import clear_resiliency_markers

    clear_resiliency_markers(exe_name)
    before = office_pids_live(exe_name)
    app = win32com.Dispatch(prog_id)
    register_owned_pids(office_pids_live(exe_name) - before)
    return app


def _get_word_app():
    """현재 스레드의 Word.Application COM 인스턴스를 반환한다."""
    if not getattr(_tls, "word", None):
        _ensure_com_initialized()
        win32com = _require_win32com()
        app = _dispatch_and_own(win32com, "Word.Application", "WINWORD.EXE")
        # 모달 다이얼로그/매크로 경고/변환 확인창 억제
        try:
            app.Visible = False
            app.DisplayAlerts = _WD_ALERTS_NONE
            app.AutomationSecurity = _MSO_SEC_FORCE_DISABLE
            app.Options.ConfirmConversions = False
        except Exception:
            pass
        _tls.word = app
    return _tls.word


def _get_excel_app():
    """현재 스레드의 Excel.Application COM 인스턴스를 반환한다."""
    if not getattr(_tls, "excel", None):
        _ensure_com_initialized()
        win32com = _require_win32com()
        app = _dispatch_and_own(win32com, "Excel.Application", "EXCEL.EXE")
        try:
            app.Visible = False
            app.DisplayAlerts = _XL_ALERTS_OFF
            app.AutomationSecurity = _MSO_SEC_FORCE_DISABLE
            app.AskToUpdateLinks = False
        except Exception:
            pass
        _tls.excel = app
    return _tls.excel


def _get_ppt_app():
    """현재 스레드의 PowerPoint.Application COM 인스턴스를 반환한다."""
    if not getattr(_tls, "ppt", None):
        _ensure_com_initialized()
        win32com = _require_win32com()
        app = _dispatch_and_own(win32com, "PowerPoint.Application", "POWERPNT.EXE")
        try:
            app.DisplayAlerts = 1  # ppAlertsNone 계열 (버전별 차이 → try)
            app.AutomationSecurity = _MSO_SEC_FORCE_DISABLE
        except Exception:
            pass
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
            with timer.stage(com_stage.STAGE_READ):
                text = doc.Content.Text
            return text
        except Exception:
            _tls.word = None  # 예외 시 이 스레드의 인스턴스 리셋
            raise
        finally:
            _close_quietly(timer, doc, "Close", False)
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
        except Exception:
            _tls.excel = None
            raise
        finally:
            _close_quietly(timer, wb, "Close", False)
            com_stage.clear()
            timer.log_summary()


def _ppt_shape_texts(shape) -> list[str]:
    """PowerPoint 도형 하나에서 텍스트를 추출한다. 그룹은 재귀, 표는 셀을 펼친다.

    COM 속성 접근은 도형 타입별로 예외가 날 수 있어 각 분기를 try로 감싼다.
    """
    # 그룹 도형(조직도) → 내부 도형 재귀
    try:
        if shape.Type == _MSO_GROUP:
            out: list[str] = []
            for child in shape.GroupItems:
                out.extend(_ppt_shape_texts(child))
            return out
    except Exception:
        pass

    # 표 도형 → 셀(1-indexed) 순회 후 ' | ' 텍스트화
    try:
        if shape.HasTable:
            table = shape.Table
            rows: list[list[str]] = []
            for r in range(1, table.Rows.Count + 1):
                rows.append(
                    [
                        table.Cell(r, c).Shape.TextFrame.TextRange.Text
                        for c in range(1, table.Columns.Count + 1)
                    ]
                )
            table_text = format_table(rows)
            return [table_text] if table_text else []
    except Exception:
        pass

    # 일반 텍스트 프레임
    try:
        if shape.HasTextFrame:
            t = shape.TextFrame.TextRange.Text.strip()
            if t:
                return [t]
    except Exception:
        pass

    return []


class PowerPointComReader:
    """ppt 파일을 COM(PowerPoint)으로 파싱하는 리더. 스레드별 싱글톤을 사용한다."""

    def parse(self, path: str) -> str:
        """ppt 파일을 열어 슬라이드 텍스트를 반환한다 (표·그룹 도형 포함).

        단계(dispatch/open/read/close)별 소요시간을 계측한다(COM 처리 안정화).
        """
        timer = com_stage.StageTimer(path)
        prs = None
        try:
            with timer.stage(com_stage.STAGE_DISPATCH):
                ppt = _get_ppt_app()
            with timer.stage(com_stage.STAGE_OPEN):
                prs = ppt.Presentations.Open(str(Path(path).resolve()), ReadOnly=True, WithWindow=False)
            with timer.stage(com_stage.STAGE_READ):
                slides: list[str] = []
                for slide in prs.Slides:
                    texts: list[str] = []
                    for shape in slide.Shapes:
                        texts.extend(_ppt_shape_texts(shape))
                    texts = [t for t in texts if t.strip()]
                    if texts:
                        slides.append("\n".join(texts))
            return "\n\n".join(slides)
        except Exception:
            _tls.ppt = None
            raise
        finally:
            _close_quietly(timer, prs, "Close")
            com_stage.clear()
            timer.log_summary()




def quit_com_apps(grace_sec: float = 5.0, wait_fn=None) -> None:
    """현재 스레드의 COM 앱들을 Quit하고 thread-local을 비운다.

    COM 객체는 생성한 스레드에서만 Quit할 수 있으므로(STA),
    반드시 COM 앱을 생성한 워커 스레드 내부에서 호출해야 한다.
    누수된 WINWORD/EXCEL/POWERPNT 프로세스를 정리한다.

    `app.Quit()`은 종료를 요청할 뿐 즉시 반환한다 — 실제 종료(임시파일·애드인
    정리 등)까지는 수 초 걸릴 수 있어, 반환 직후 바로 프로세스를 조회하면
    스스로 꺼지는 중인 것까지 매번 강제 종료로 오판했다(레이스). `grace_sec`
    만큼 실제 종료를 기다린 뒤, 그래도 남은 '우리 소유' 프로세스만 강제
    종료한다 — 좀비가 다음 사이클의 가드를 오작동시키지 않게 한다(자기 감지
    스킵 방지). 강제 종료는 Office에 세이프모드 유발 표식을 남기므로, 유예를
    주는 것만으로 이 표식 생성 자체를 줄일 수 있다.

    Quit() 전 `gc.collect()`를 1회 호출한다 — 파이썬 쪽에서 COM 래퍼 참조를
    깜빡 놓지 않고 있어(순환 참조 등) Office가 "아직 누가 쓰고 있다"고 보고
    스스로 종료하지 못하는 경우를 미리 제거한다. 이 라인이 있어도 여전히
    유예를 다 쓰고 강제 종료가 반복된다면, 원인은 레이스가 아니라 어딘가
    COM 참조를 명시적으로 놓지 않는 코드가 있다는 신호다 — `대기 Ns` 로그로
    운영 중 구분한다.

    grace_sec=0이면 대기 없이 기존 동작(즉시 강제종료)과 동일하다(비상 스위치).
    wait_fn: 테스트 주입용(기본 `office_guard.wait_for_owned_exit`).
    """
    import gc
    gc.collect()

    for attr in ("word", "excel", "ppt"):
        app = getattr(_tls, attr, None)
        if app is None:
            continue
        try:
            app.Quit()
        except Exception:
            pass
        setattr(_tls, attr, None)

    # 우리가 띄운 인스턴스 중 Quit 후에도 남아있으면 강제 종료 + 소유 목록 비움
    try:
        from knowmate.secure import office_guard
        owned = office_guard.clear_owned_pids()
        if not owned:
            return
        if wait_fn is None:
            wait_fn = office_guard.wait_for_owned_exit
        if grace_sec > 0:
            still_alive, elapsed = wait_fn(owned, grace_sec)
        else:
            still_alive, elapsed = owned, 0.0
        exited = len(owned) - len(still_alive)
        if exited:
            logger.info("[com] Office 정상 종료 확인: %d개 (대기 %.1f초)", exited, elapsed)
        if still_alive:
            logger.warning(
                "[com] 유예 %.0f초 초과 — 강제 종료: %s (대기 %.1f초)",
                grace_sec, sorted(still_alive), elapsed,
            )
            office_guard.terminate_owned_office_processes(still_alive)
    except Exception as exc:
        logger.debug("COM 소유 프로세스 정리 실패(무시): %s", exc)


class ComReader:
    """확장자를 보고 Word/Excel/PowerPoint COM 리더로 라우팅하는 TextExtractor 구현체."""

    def __init__(self, xlsx_block_rows: int | None = None) -> None:
        """xlsx_block_rows: Excel 범위 읽기 블록 크기(config `chunking.xlsx_block_rows`).

        리더 3개를 인스턴스 속성으로 보유한다(이전에는 모듈 레벨 싱글톤). 리더 객체는
        무상태라 인스턴스화 비용이 없고, 진짜 재사용 대상인 COM 앱은 `_tls`에 따로
        보관되므로 싱글톤을 없애도 Office 프로세스 재사용에는 영향이 없다.
        """
        self._word = WordComReader()
        self._excel = ExcelComReader(block_rows=xlsx_block_rows)
        self._ppt = PowerPointComReader()

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
