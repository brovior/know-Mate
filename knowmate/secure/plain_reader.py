"""확장자 기반 실제 파일 파싱 TextExtractor."""
from pathlib import Path


class PlainReader:
    def extract(self, path: str) -> str:
        """확장자에 따라 적합한 파서로 파일을 읽어 텍스트를 반환한다."""
        ext = Path(path).suffix.lower()
        if ext == ".docx":
            return self._read_docx(path)
        if ext == ".xlsx":
            return self._read_xlsx(path)
        if ext == ".pptx":
            return self._read_pptx(path)
        if ext == ".pdf":
            return self._read_pdf(path)
        if ext in {".txt", ".md", ".log"}:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        raise ValueError(f"지원하지 않는 파일 형식: {ext!r} ({path})")

    def _read_docx(self, path: str) -> str:
        """python-docx로 docx 파일을 읽어 문단 텍스트를 반환한다."""
        import docx  # type: ignore

        doc = docx.Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    def _read_xlsx(self, path: str) -> str:
        """openpyxl로 xlsx 파일을 읽어 셀값을 탭 구분 텍스트로 반환한다."""
        import openpyxl  # type: ignore

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        lines: list[str] = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                row_text = "\t".join(
                    str(cell) if cell is not None else "" for cell in row
                )
                if row_text.strip():
                    lines.append(row_text)
        wb.close()
        return "\n".join(lines)

    def _read_pptx(self, path: str) -> str:
        """python-pptx로 pptx 파일을 읽어 슬라이드별 텍스트를 반환한다."""
        from pptx import Presentation  # type: ignore

        prs = Presentation(path)
        slides: list[str] = []
        for slide in prs.slides:
            texts = [
                shape.text
                for shape in slide.shapes
                if hasattr(shape, "text") and shape.text.strip()
            ]
            if texts:
                slides.append("\n".join(texts))
        return "\n\n".join(slides)

    def _read_pdf(self, path: str) -> str:
        """PyMuPDF(fitz)로 pdf 파일을 읽어 페이지별 텍스트를 반환한다."""
        import fitz  # type: ignore

        pages: list[str] = []
        with fitz.open(path) as doc:
            for page in doc:
                text = page.get_text()
                if text.strip():
                    pages.append(text)
        return "\n\n".join(pages)
