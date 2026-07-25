"""구형 바이너리 포맷(doc/xls/ppt) COM 싱글톤 파서 (CLAUDE.md 6-6).

win32com 없는 환경에서 import 시 ComUnavailableError를 발생시킨다.
fake 모드에서는 이 모듈을 import하지 않아야 한다.

COM STA 주의: COM 객체는 생성한 스레드에서만 사용 가능하다.
_ThreadLocalComApps를 통해 스레드별로 독립적인 COM 앱 인스턴스를 관리한다.
"""
import logging
import threading
from pathlib import Path

from knowmate.secure.text_util import format_table

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
        """doc 파일을 열어 본문 텍스트를 반환한다."""
        try:
            word = _get_word_app()
            # 모든 모달 프롬프트를 사전 차단한다 — 백그라운드라 아무도 답할 수 없어
            # 프롬프트 하나가 그대로 행오버가 되고, 워치독 강제 종료 → 세이프모드 표식
            # → 다음 기동 때 또 프롬프트로 이어지는 루프의 시작점이 된다.
            # 이름 인자 대신 위치 인자로 넘긴다(late binding에서 이름 인자는 신뢰 불가).
            _m = _com_missing()
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
            text = doc.Content.Text
            doc.Close(False)
            return text
        except Exception:
            _tls.word = None  # 예외 시 이 스레드의 인스턴스 리셋
            raise


class ExcelComReader:
    """xls 파일을 COM(Excel)으로 파싱하는 리더. 스레드별 싱글톤을 사용한다."""

    def parse(self, path: str) -> str:
        """xls 파일을 열어 시트 전체를 탭 구분 텍스트로 반환한다."""
        try:
            excel = _get_excel_app()
            # Word와 같은 이유로 모든 모달 프롬프트를 사전 차단한다(위 주석 참조).
            # 특히 CorruptLoad=xlRepairFile은 "파일이 손상됐습니다. 복구할까요?" 확인창을
            # 없애는데, 이 확인창은 앱 수준 DisplayAlerts=False로도 억제되지 않는다.
            _m = _com_missing()
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
            lines: list[str] = []
            for sheet in wb.Sheets:
                sheet_lines: list[str] = []
                used = sheet.UsedRange
                for row in used.Rows:
                    cells = [str(cell.Value) if cell.Value is not None else "" for cell in row.Cells]
                    row_text = "\t".join(cells)
                    if row_text.strip():
                        sheet_lines.append(row_text)
                if sheet_lines:
                    lines.append(f"=== 시트: {sheet.Name} ===")
                    lines.extend(sheet_lines)
            wb.Close(False)
            return "\n".join(lines)
        except Exception:
            _tls.excel = None
            raise


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
        """ppt 파일을 열어 슬라이드 텍스트를 반환한다 (표·그룹 도형 포함)."""
        try:
            ppt = _get_ppt_app()
            prs = ppt.Presentations.Open(str(Path(path).resolve()), ReadOnly=True, WithWindow=False)
            slides: list[str] = []
            for slide in prs.Slides:
                texts: list[str] = []
                for shape in slide.Shapes:
                    texts.extend(_ppt_shape_texts(shape))
                texts = [t for t in texts if t.strip()]
                if texts:
                    slides.append("\n".join(texts))
            prs.Close()
            return "\n\n".join(slides)
        except Exception:
            _tls.ppt = None
            raise


_word_reader = WordComReader()
_excel_reader = ExcelComReader()
_ppt_reader = PowerPointComReader()


def quit_com_apps() -> None:
    """현재 스레드의 COM 앱들을 Quit하고 thread-local을 비운다.

    COM 객체는 생성한 스레드에서만 Quit할 수 있으므로(STA),
    반드시 COM 앱을 생성한 워커 스레드 내부에서 호출해야 한다.
    누수된 WINWORD/EXCEL/POWERPNT 프로세스를 정리한다.

    Quit이 실패해 남은 '우리 소유' 프로세스는 강제 종료해 좀비가 다음
    사이클의 가드를 오작동시키지 않게 한다(자기 감지 스킵 방지).
    """
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
        from knowmate.secure.office_guard import (
            clear_owned_pids,
            terminate_owned_office_processes,
        )
        owned = clear_owned_pids()
        terminate_owned_office_processes(owned)
    except Exception as exc:
        logging.getLogger(__name__).debug("COM 소유 프로세스 정리 실패(무시): %s", exc)


class ComReader:
    """확장자를 보고 Word/Excel/PowerPoint COM 리더로 라우팅하는 TextExtractor 구현체."""

    def extract(self, path: str) -> str:
        """확장자에 따라 적합한 COM 리더로 파일을 파싱해 텍스트를 반환한다.

        OLE2 오라벨 파일(.docx/.xlsx/.pptx인데 실제 구형 바이너리)도 같은 앱으로
        라우팅한다. COM 앱은 확장자와 무관하게 실제 포맷을 열기 때문이다.
        """
        ext = Path(path).suffix.lower()
        if ext in (".doc", ".docx"):
            return _word_reader.parse(path)
        if ext in (".xls", ".xlsx"):
            return _excel_reader.parse(path)
        if ext in (".ppt", ".pptx"):
            return _ppt_reader.parse(path)
        raise ValueError(f"ComReader가 지원하지 않는 확장자: {ext!r} ({path})")
