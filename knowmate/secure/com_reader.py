"""구형 바이너리 포맷(doc/xls/ppt) COM 싱글톤 파서 (CLAUDE.md 6-6).

win32com 없는 환경에서 import 시 ComUnavailableError를 발생시킨다.
fake 모드에서는 이 모듈을 import하지 않아야 한다.
"""
from pathlib import Path

# COM을 사용할 수 없는 환경임을 나타내는 예외
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


class WordComReader:
    """doc 파일을 COM(Word) 싱글톤으로 파싱하는 리더.

    매번 Quit() 하지 않고 인스턴스를 재사용해 기동 비용을 절감한다.
    """

    _instance = None  # Word.Application COM 싱글톤

    def get_app(self):
        """Word COM 애플리케이션 싱글톤을 반환한다. 없으면 생성한다."""
        if self._instance is None:
            win32com = _require_win32com()
            self._instance = win32com.Dispatch("Word.Application")
        return self._instance

    def parse(self, path: str) -> str:
        """doc 파일을 열어 본문 텍스트를 반환한다.

        예외 발생 시 인스턴스를 초기화해 다음 호출에 재생성하도록 한다.
        """
        word = self.get_app()
        try:
            doc = word.Documents.Open(str(Path(path).resolve()))
            text = doc.Content.Text
            doc.Close(False)
            return text
        except Exception:
            WordComReader._instance = None
            raise


class ExcelComReader:
    """xls 파일을 COM(Excel) 싱글톤으로 파싱하는 리더.

    매번 Quit() 하지 않고 인스턴스를 재사용한다.
    """

    _instance = None  # Excel.Application COM 싱글톤

    def get_app(self):
        """Excel COM 애플리케이션 싱글톤을 반환한다. 없으면 생성한다."""
        if self._instance is None:
            win32com = _require_win32com()
            self._instance = win32com.Dispatch("Excel.Application")
            self._instance.Visible = False
            self._instance.DisplayAlerts = False
        return self._instance

    def parse(self, path: str) -> str:
        """xls 파일을 열어 시트 전체를 탭 구분 텍스트로 반환한다.

        예외 발생 시 인스턴스를 초기화해 다음 호출에 재생성하도록 한다.
        """
        excel = self.get_app()
        try:
            wb = excel.Workbooks.Open(str(Path(path).resolve()))
            lines: list[str] = []
            for sheet in wb.Sheets:
                used = sheet.UsedRange
                for row in used.Rows:
                    cells = [str(cell.Value) if cell.Value is not None else "" for cell in row.Cells]
                    row_text = "\t".join(cells)
                    if row_text.strip():
                        lines.append(row_text)
            wb.Close(False)
            return "\n".join(lines)
        except Exception:
            ExcelComReader._instance = None
            raise


class PowerPointComReader:
    """ppt 파일을 COM(PowerPoint) 싱글톤으로 파싱하는 리더.

    매번 Quit() 하지 않고 인스턴스를 재사용한다.
    """

    _instance = None  # PowerPoint.Application COM 싱글톤

    def get_app(self):
        """PowerPoint COM 애플리케이션 싱글톤을 반환한다. 없으면 생성한다."""
        if self._instance is None:
            win32com = _require_win32com()
            self._instance = win32com.Dispatch("PowerPoint.Application")
        return self._instance

    def parse(self, path: str) -> str:
        """ppt 파일을 열어 슬라이드 텍스트를 반환한다.

        예외 발생 시 인스턴스를 초기화해 다음 호출에 재생성하도록 한다.
        """
        ppt = self.get_app()
        try:
            prs = ppt.Presentations.Open(str(Path(path).resolve()), ReadOnly=True, WithWindow=False)
            slides: list[str] = []
            for slide in prs.Slides:
                texts: list[str] = []
                for shape in slide.Shapes:
                    if shape.HasTextFrame:
                        t = shape.TextFrame.TextRange.Text.strip()
                        if t:
                            texts.append(t)
                if texts:
                    slides.append("\n".join(texts))
            prs.Close()
            return "\n\n".join(slides)
        except Exception:
            PowerPointComReader._instance = None
            raise


# COM 리더 싱글톤 인스턴스 (모듈 레벨 공유)
_word_reader = WordComReader()
_excel_reader = ExcelComReader()
_ppt_reader = PowerPointComReader()


class ComReader:
    """확장자를 보고 Word/Excel/PowerPoint COM 리더로 라우팅하는 TextExtractor 구현체."""

    def extract(self, path: str) -> str:
        """확장자에 따라 적합한 COM 리더로 파일을 파싱해 텍스트를 반환한다."""
        ext = Path(path).suffix.lower()
        if ext == ".doc":
            return _word_reader.parse(path)
        if ext == ".xls":
            return _excel_reader.parse(path)
        if ext == ".ppt":
            return _ppt_reader.parse(path)
        raise ValueError(f"ComReader가 지원하지 않는 확장자: {ext!r} ({path})")
