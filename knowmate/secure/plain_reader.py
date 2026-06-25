"""확장자 기반 실제 파일 파싱 TextExtractor."""
from pathlib import Path

from knowmate.secure.text_util import format_table


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
        """python-docx로 docx를 읽어 문단·표 텍스트를 문서 순서대로 반환한다."""
        import docx  # type: ignore
        from docx.document import Document as _Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = docx.Document(path)
        parts: list[str] = []
        # body 자식을 순서대로 순회 → 문단과 표가 섞인 원래 순서 보존
        for child in doc.element.body.iterchildren():
            if child.tag == qn("w:p"):
                text = Paragraph(child, doc).text
                if text.strip():
                    parts.append(text)
            elif child.tag == qn("w:tbl"):
                table_text = self._format_table(
                    [[cell.text for cell in row.cells] for row in Table(child, doc).rows]
                )
                if table_text:
                    parts.append(table_text)
        return "\n".join(parts)

    @staticmethod
    def _format_table(rows: list[list[str]]) -> str:
        """표 행렬을 ' | ' 텍스트로 변환한다 (공용 format_table에 위임)."""
        return format_table(rows)

    def _read_xlsx(self, path: str) -> str:
        """openpyxl로 xlsx 파일을 읽어 셀값을 탭 구분 텍스트로 반환한다.

        손상된 사용자 정의 속성(docProps/custom.xml)으로 load_workbook이 실패하면
        해당 파트를 제거한 사본으로 재시도해 시트 데이터를 복구한다.
        """
        import openpyxl  # type: ignore

        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:
            # custom.xml 손상 등 메타데이터 문제 → 정리한 사본으로 재시도(복구)
            import logging
            logging.getLogger(__name__).warning(
                "xlsx 로드 실패, custom.xml 제거 후 재시도: %s (%s)", path, exc
            )
            wb = self._load_xlsx_sanitized(path)

        lines: list[str] = []
        try:
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    row_text = "\t".join(
                        str(cell) if cell is not None else "" for cell in row
                    )
                    if row_text.strip():
                        lines.append(row_text)
        finally:
            wb.close()
        return "\n".join(lines)

    @staticmethod
    def _load_xlsx_sanitized(path: str):
        """docProps/custom.xml과 [ContentTypes].xml 내 참조를 제거한 사본으로 워크북을 로드한다.

        custom.xml 파트만 빼면 [ContentTypes].xml에 참조가 남아 openpyxl이 다시 실패하므로
        해당 Override 엔트리도 함께 제거한다.
        """
        import io
        import re
        import zipfile
        import openpyxl  # type: ignore

        CUSTOM_XML = "docProps/custom.xml"

        buf = io.BytesIO()
        with zipfile.ZipFile(path, "r") as zin:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename == CUSTOM_XML:
                        continue
                    data = zin.read(item.filename)
                    if item.filename == "[Content_Types].xml":
                        # custom.xml Override 엔트리 제거
                        data = re.sub(
                            rb'<Override[^/]*/docProps/custom\.xml[^/]*/?>',
                            b"",
                            data,
                        )
                    zout.writestr(item, data)
        buf.seek(0)
        return openpyxl.load_workbook(buf, read_only=True, data_only=True)

    def _read_pptx(self, path: str) -> str:
        """python-pptx로 pptx를 읽어 슬라이드별 텍스트를 반환한다.

        표(has_table)와 그룹 도형(조직도) 내부 도형까지 재귀로 추출한다.
        """
        from pptx import Presentation  # type: ignore

        prs = Presentation(path)
        slides: list[str] = []
        for slide in prs.slides:
            texts: list[str] = []
            for shape in slide.shapes:
                texts.extend(self._iter_shape_texts(shape))
            texts = [t for t in texts if t.strip()]
            if texts:
                slides.append("\n".join(texts))
        return "\n\n".join(slides)

    def _iter_shape_texts(self, shape) -> list[str]:
        """도형 하나에서 텍스트를 추출한다. 그룹은 재귀, 표는 셀을 펼친다."""
        from pptx.enum.shapes import MSO_SHAPE_TYPE  # type: ignore

        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            out: list[str] = []
            for child in shape.shapes:
                out.extend(self._iter_shape_texts(child))
            return out

        # has_table / has_text_frame 는 일부 도형 타입에만 존재 → getattr 로 방어
        if getattr(shape, "has_table", False):
            table_text = self._format_table(
                [[cell.text for cell in row.cells] for row in shape.table.rows]
            )
            return [table_text] if table_text else []

        if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
            return [shape.text_frame.text]

        return []

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
