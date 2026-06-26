"""구형 바이너리 포맷(doc/xls/ppt) COM 싱글톤 파서 (CLAUDE.md 6-6).

win32com 없는 환경에서 import 시 ComUnavailableError를 발생시킨다.
fake 모드에서는 이 모듈을 import하지 않아야 한다.

COM STA 주의: COM 객체는 생성한 스레드에서만 사용 가능하다.
_ThreadLocalComApps를 통해 스레드별로 독립적인 COM 앱 인스턴스를 관리한다.
"""
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


def _get_word_app():
    """현재 스레드의 Word.Application COM 인스턴스를 반환한다."""
    if not getattr(_tls, "word", None):
        _ensure_com_initialized()
        win32com = _require_win32com()
        app = win32com.Dispatch("Word.Application")
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
        app = win32com.Dispatch("Excel.Application")
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
        app = win32com.Dispatch("PowerPoint.Application")
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
            # ConfirmConversions=False: 변환 확인창 억제
            # ReadOnly=True, AddToRecentFiles=False, Visible=False
            doc = word.Documents.Open(
                str(Path(path).resolve()),
                False,   # ConfirmConversions
                True,    # ReadOnly
                False,   # AddToRecentFiles
                "",      # PasswordDocument
                "",      # PasswordTemplate
                False,   # Revert
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
            wb = excel.Workbooks.Open(str(Path(path).resolve()))
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
