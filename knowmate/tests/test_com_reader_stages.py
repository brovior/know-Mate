"""ExcelComReader의 단계 구분·확실한 닫기·범위 단위 일괄 읽기 테스트.

두 가지 핵심 회귀 방어:
1. 이전에는 정상 경로에서만 `wb.Close(False)`가 호출돼, 셀 읽기 중 예외가 나면
   워크북이 열린 채로 남았다. 이제는 `finally`에서 항상 닫기를 시도한다.
2. 이전에는 셀마다 COM 왕복(`cell.Value`)을 했다. 이제 `Range.Value`로 블록당
   왕복 1회만 한다 — 페이크 시트가 개별 셀 접근을 아예 제공하지 않으므로,
   셀 단위로 되돌아가면 테스트가 즉시 깨진다.

실제 win32com/COM 없이 `_get_excel_app`을 가짜 객체로 monkeypatch해 사외(Linux)
환경에서도 전부 통과한다.
"""
import logging

import pytest

from knowmate.secure import com_reader


class _FakeCellRef:
    """`sheet.Cells(r, c)`가 돌려주는 좌표 표식(실제 COM 셀 객체 대역)."""

    def __init__(self, row: int, col: int) -> None:
        self.row = row
        self.col = col


class _FakeRange:
    def __init__(self, values):
        self.Value = values


class _FakeUsedRange:
    def __init__(self, first_row: int, first_col: int, n_rows: int, n_cols: int) -> None:
        self.Row = first_row
        self.Column = first_col
        self.Rows = type("_C", (), {"Count": n_rows})()
        self.Columns = type("_C", (), {"Count": n_cols})()


class _FakeSheet:
    """UsedRange + Range(Cells, Cells).Value만 제공한다.

    **개별 셀 값 접근(`cell.Value`) 경로를 의도적으로 제공하지 않는다** — 셀 단위
    COM 왕복으로 회귀하면 AttributeError로 즉시 드러나게 하기 위함.
    """

    def __init__(self, name: str, rows: list[list], first_row: int = 1, first_col: int = 1) -> None:
        self.Name = name
        self._rows = rows
        self._first_row = first_row
        self._first_col = first_col
        n_rows = len(rows)
        n_cols = max((len(r) for r in rows), default=1) if rows else 1
        self.UsedRange = _FakeUsedRange(first_row, first_col, n_rows, n_cols)
        self.range_calls: list[tuple[int, int, int, int]] = []  # 요청된 (r1,c1,r2,c2)

    def Cells(self, row: int, col: int) -> _FakeCellRef:
        return _FakeCellRef(row, col)

    def Range(self, top_left: _FakeCellRef, bottom_right: _FakeCellRef) -> _FakeRange:
        self.range_calls.append((top_left.row, top_left.col, bottom_right.row, bottom_right.col))
        # UsedRange 오프셋을 빼서 내부 rows 인덱스로 변환
        r_start = top_left.row - self._first_row
        r_end = bottom_right.row - self._first_row
        block = [tuple(r) for r in self._rows[r_start:r_end + 1]]
        if len(block) == 1 and len(block[0]) == 1:
            return _FakeRange(block[0][0])  # pywin32의 1×1 스칼라 반환 재현
        return _FakeRange(tuple(block))


class _FakeWorkbook:
    def __init__(self, sheets):
        self.Sheets = sheets
        self.close_calls: list[bool] = []

    def Close(self, save_changes):
        self.close_calls.append(save_changes)


class _FakeWorkbooksCollection:
    def __init__(self, open_fn):
        self._open_fn = open_fn

    def Open(self, *args, **kwargs):
        return self._open_fn(*args, **kwargs)


class _FakeExcelApp:
    def __init__(self, open_fn):
        self.Workbooks = _FakeWorkbooksCollection(open_fn)


class _RaisingSheet:
    """Range 읽기에서 예외를 던지는 시트 — 셀 읽기 단계 실패 시뮬레이션."""

    Name = "Sheet1"

    def __init__(self):
        self.UsedRange = _FakeUsedRange(1, 1, 3, 3)

    def Cells(self, row, col):
        return _FakeCellRef(row, col)

    def Range(self, top_left, bottom_right):
        raise RuntimeError("셀 읽기 실패(시뮬레이션)")


def _app_with(sheets):
    wb = _FakeWorkbook(sheets)
    return wb, _FakeExcelApp(lambda *a, **kw: wb)


@pytest.fixture(autouse=True)
def _clear_com_stage():
    from knowmate.secure import com_stage
    com_stage.clear()
    yield
    com_stage.clear()


class TestExcelComReaderClosing:
    def test_normal_parse_closes_workbook(self, monkeypatch):
        wb, app = _app_with([_FakeSheet("Sheet1", [["a", "b"]])])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        text = com_reader.ExcelComReader().parse("/x.xlsx")

        assert "a\tb" in text
        assert wb.close_calls == [False]

    def test_exception_during_cell_read_still_closes_workbook(self, monkeypatch):
        """핵심 회귀 방어: 셀 읽기 중 예외가 나도 finally에서 Close(False)가 호출된다."""
        wb, app = _app_with([_RaisingSheet()])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        with pytest.raises(RuntimeError):
            com_reader.ExcelComReader().parse("/x.xlsx")

        assert wb.close_calls == [False]

    def test_exception_during_open_does_not_attempt_close(self, monkeypatch):
        """Open 자체가 실패하면 wb가 없으므로 닫기를 시도하지 않는다(None.Close() 방지)."""
        def _boom(*a, **kw):
            raise RuntimeError("Open 실패(시뮬레이션)")

        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: _FakeExcelApp(_boom))

        with pytest.raises(RuntimeError):
            com_reader.ExcelComReader().parse("/x.xlsx")


class TestExcelComReaderStages:
    def test_stage_order_recorded(self, monkeypatch, caplog):
        _wb, app = _app_with([_FakeSheet("Sheet1", [["v"]])])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        with caplog.at_level(logging.DEBUG, logger="knowmate.secure.com_stage"):
            com_reader.ExcelComReader().parse("/x.xlsx")

        summary_lines = [r.message for r in caplog.records if "단계별 소요" in r.message]
        assert len(summary_lines) == 1
        for stage in ("dispatch=", "open=", "sheets=", "cell_read=", "close="):
            assert stage in summary_lines[0]

    def test_com_stage_cleared_after_parse(self, monkeypatch):
        from knowmate.secure import com_stage
        _wb, app = _app_with([_FakeSheet("Sheet1", [["v"]])])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        com_reader.ExcelComReader().parse("/x.xlsx")

        stage, path, _elapsed = com_stage.snapshot()
        assert stage is None and path is None

    def test_failed_stage_reported_in_summary_log(self, monkeypatch, caplog):
        _wb, app = _app_with([_RaisingSheet()])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        with caplog.at_level(logging.DEBUG, logger="knowmate.secure.com_stage"):
            with pytest.raises(RuntimeError):
                com_reader.ExcelComReader().parse("/x.xlsx")

        summary = [r.message for r in caplog.records if "단계별 소요" in r.message][0]
        assert "실패단계=cell_read" in summary

    def test_log_does_not_contain_cell_values(self, monkeypatch, caplog):
        """CLAUDE.md 원칙7: 로그에 셀 내용·문서 내용을 남기면 안 된다."""
        secret = "극비_연봉_1억2000만원"
        _wb, app = _app_with([_FakeSheet("Sheet1", [[secret]])])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        with caplog.at_level(logging.DEBUG):
            com_reader.ExcelComReader().parse("/x.xlsx")

        for record in caplog.records:
            assert secret not in record.message


class TestBulkRangeRead:
    """셀 단위 COM 왕복 → 범위 단위 일괄 읽기."""

    def test_single_range_call_for_small_sheet(self, monkeypatch):
        """3×3 시트는 왕복 1회로 끝나야 한다(이전엔 셀당 1회 = 9회)."""
        sheet = _FakeSheet("Sheet1", [["a", "b", "c"], ["d", "e", "f"], ["g", "h", "i"]])
        _wb, app = _app_with([sheet])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        text = com_reader.ExcelComReader().parse("/x.xlsx")

        assert len(sheet.range_calls) == 1
        assert sheet.range_calls[0] == (1, 1, 3, 3)
        assert "a\tb\tc" in text and "g\th\ti" in text

    def test_splits_into_blocks_of_configured_size(self, monkeypatch):
        """block_rows=2, 5행 → (1~2), (3~4), (5~5) 세 번으로 나뉜다."""
        rows = [[f"r{i}c1", f"r{i}c2"] for i in range(1, 6)]
        sheet = _FakeSheet("Sheet1", rows)
        _wb, app = _app_with([sheet])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        text = com_reader.ExcelComReader(block_rows=2).parse("/x.xlsx")

        assert sheet.range_calls == [(1, 1, 2, 2), (3, 1, 4, 2), (5, 1, 5, 2)]
        # 분할해도 모든 행이 순서대로 다 나와야 한다
        body = [ln for ln in text.splitlines() if not ln.startswith("=== 시트:")]
        assert body == [f"r{i}c1\tr{i}c2" for i in range(1, 6)]

    def test_respects_used_range_offset(self, monkeypatch):
        """UsedRange가 A1이 아니라 C5부터 시작해도 올바른 범위를 요청한다."""
        sheet = _FakeSheet("Sheet1", [["x", "y"], ["z", "w"]], first_row=5, first_col=3)
        _wb, app = _app_with([sheet])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        text = com_reader.ExcelComReader().parse("/x.xlsx")

        assert sheet.range_calls == [(5, 3, 6, 4)]
        assert "x\ty" in text and "z\tw" in text

    def test_single_cell_scalar_is_normalized(self, monkeypatch):
        """pywin32는 1×1 범위에서 2차원 튜플이 아니라 스칼라를 돌려준다(대표적 함정)."""
        sheet = _FakeSheet("Sheet1", [["only"]])
        _wb, app = _app_with([sheet])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        text = com_reader.ExcelComReader().parse("/x.xlsx")

        assert "only" in text

    def test_output_format_matches_plain_reader_convention(self, monkeypatch):
        """탭 구분·빈 행 스킵·시트 헤더가 openpyxl/xlrd 경로와 동일해야 한다 —
        세 경로가 같은 인덱스에 들어가므로 포맷이 갈리면 검색 품질이 경로에 따라
        달라진다."""
        sheet = _FakeSheet("매출", [["a", "b"], [None, None], ["c", None]])
        _wb, app = _app_with([sheet])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        text = com_reader.ExcelComReader().parse("/x.xlsx")

        assert text.splitlines() == ["=== 시트: 매출 ===", "a\tb", "c\t"]

    def test_empty_sheet_produces_no_header(self, monkeypatch):
        """내용이 전부 빈 시트는 헤더조차 넣지 않는다(기존 동작 유지)."""
        sheet = _FakeSheet("빈시트", [[None, None]])
        _wb, app = _app_with([sheet])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        assert com_reader.ExcelComReader().parse("/x.xlsx") == ""

    def test_multiple_sheets_each_get_header(self, monkeypatch):
        s1 = _FakeSheet("S1", [["a"]])
        s2 = _FakeSheet("S2", [["b"]])
        _wb, app = _app_with([s1, s2])
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        text = com_reader.ExcelComReader().parse("/x.xlsx")

        assert text.splitlines() == ["=== 시트: S1 ===", "a", "=== 시트: S2 ===", "b"]


class TestBlockRowsConfiguration:
    @pytest.mark.parametrize("bad", [None, 0, -1, -1000, "1000", 1.5, True])
    def test_invalid_values_fall_back_to_default(self, bad):
        """config는 사용자가 직접 편집할 수 있어, 잘못된 값 하나로 인덱싱이 죽으면
        안 된다(fail-safe)."""
        reader = com_reader.ExcelComReader(block_rows=bad)
        assert reader._block_rows == com_reader._DEFAULT_XL_BLOCK_ROWS

    def test_valid_value_is_used(self):
        assert com_reader.ExcelComReader(block_rows=250)._block_rows == 250

    def test_default_is_1000(self):
        assert com_reader._DEFAULT_XL_BLOCK_ROWS == 1000
        assert com_reader.ExcelComReader()._block_rows == 1000


class TestInjectionChain:
    """config → get_extractor → AutoReader → ComReader → ExcelComReader 전달."""

    def test_com_reader_passes_block_rows_to_excel_reader(self):
        assert com_reader.ComReader(xlsx_block_rows=42)._excel._block_rows == 42

    def test_com_reader_default_falls_back(self):
        assert com_reader.ComReader()._excel._block_rows == com_reader._DEFAULT_XL_BLOCK_ROWS

    def test_get_extractor_auto_forwards_block_rows(self):
        from knowmate.secure import get_extractor
        reader = get_extractor("auto", xlsx_block_rows=77)
        assert reader._xlsx_block_rows == 77

    def test_get_extractor_non_auto_modes_ignore_block_rows(self):
        """fake/plain은 COM을 타지 않으므로 인자를 받아도 무시하고 정상 동작한다."""
        from knowmate.secure import get_extractor
        assert get_extractor("fake", xlsx_block_rows=77) is not None
        assert get_extractor("plain", xlsx_block_rows=77) is not None


class TestNormalizeRangeValues:
    """pywin32 Range.Value 반환 형태 정규화 — 여기서 잘못 펴면 셀 값이 엉뚱한 행에
    붙어 인덱스 내용이 조용히 오염된다."""

    def test_scalar_becomes_1x1(self):
        assert com_reader._normalize_range_values("v", 1, 1) == (("v",),)

    def test_already_2d_passes_through(self):
        values = (("a", "b"), ("c", "d"))
        assert com_reader._normalize_range_values(values, 2, 2) == values

    def test_flat_single_row_restored(self):
        assert com_reader._normalize_range_values(("a", "b", "c"), 1, 3) == (("a", "b", "c"),)

    def test_flat_single_column_restored(self):
        assert com_reader._normalize_range_values(("a", "b"), 2, 1) == (("a",), ("b",))

    def test_empty_returns_empty(self):
        assert com_reader._normalize_range_values((), 0, 0) == ()

    def test_none_scalar_becomes_1x1(self):
        assert com_reader._normalize_range_values(None, 1, 1) == ((None,),)
