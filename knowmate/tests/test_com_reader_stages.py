"""ExcelComReader의 단계 구분·확실한 닫기·로그 안전성 테스트.

핵심 회귀 방어: 이전에는 정상 경로에서만 `wb.Close(False)`가 호출돼, 셀 순회 중
예외가 나면 워크북이 열린 채로 남았다. 이제는 `finally`에서 항상 닫기를 시도한다.

실제 win32com/COM 없이 `_get_excel_app`을 가짜 객체로 monkeypatch해 사외(Linux)
환경에서도 전부 통과한다.
"""
import logging

import pytest

from knowmate.secure import com_reader


class _FakeCell:
    def __init__(self, value):
        self.Value = value


class _FakeRow:
    def __init__(self, values):
        self.Cells = [_FakeCell(v) for v in values]


class _FakeUsedRange:
    def __init__(self, rows):
        self.Rows = [_FakeRow(r) for r in rows]


class _FakeSheet:
    def __init__(self, name, rows):
        self.Name = name
        self.UsedRange = _FakeUsedRange(rows)


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


class _RaisingCells:
    """반복 시 예외를 던지는 셀 컬렉션 대역 — 셀 순회 중 실패를 시뮬레이션."""

    def __iter__(self):
        raise RuntimeError("셀 읽기 실패(시뮬레이션)")


class _RaisingRow:
    Cells = _RaisingCells()


class _RaisingUsedRange:
    Rows = [_RaisingRow()]


class _RaisingSheet:
    Name = "Sheet1"
    UsedRange = _RaisingUsedRange()


@pytest.fixture(autouse=True)
def _clear_com_stage():
    from knowmate.secure import com_stage
    com_stage.clear()
    yield
    com_stage.clear()


class TestExcelComReaderStagesAndClosing:
    def test_normal_parse_closes_workbook(self, monkeypatch):
        wb = _FakeWorkbook([_FakeSheet("Sheet1", [["a", "b"]])])
        app = _FakeExcelApp(lambda *a, **kw: wb)
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        text = com_reader.ExcelComReader().parse("/x.xlsx")

        assert "a\tb" in text
        assert wb.close_calls == [False]  # 정상 경로에서도 여전히 닫힘

    def test_exception_during_cell_read_still_closes_workbook(self, monkeypatch):
        """핵심 회귀 방어: 셀 순회 중 예외가 나도 finally에서 반드시 Close(False)가
        호출돼야 한다(이전에는 정상 경로에서만 호출돼 워크북이 열린 채 남았다)."""
        wb = _FakeWorkbook([_RaisingSheet()])
        app = _FakeExcelApp(lambda *a, **kw: wb)
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        with pytest.raises(RuntimeError):
            com_reader.ExcelComReader().parse("/x.xlsx")

        assert wb.close_calls == [False]

    def test_exception_during_open_does_not_attempt_close(self, monkeypatch):
        """Open 자체가 실패하면 wb가 없으므로 닫기를 시도하지 않는다(None.Close() 방지)."""
        def _boom(*a, **kw):
            raise RuntimeError("Open 실패(시뮬레이션)")

        app = _FakeExcelApp(_boom)
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        with pytest.raises(RuntimeError):
            com_reader.ExcelComReader().parse("/x.xlsx")
        # _FakeWorkbook이 아예 생성되지 않았으므로 close_calls를 확인할 대상이 없다 —
        # 예외 없이 여기 도달하면 None.Close() 같은 2차 예외가 안 났다는 뜻.

    def test_stage_order_is_dispatch_open_sheets_cell_read(self, monkeypatch, caplog):
        wb = _FakeWorkbook([_FakeSheet("Sheet1", [["v"]])])
        app = _FakeExcelApp(lambda *a, **kw: wb)
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        with caplog.at_level(logging.DEBUG, logger="knowmate.secure.com_stage"):
            com_reader.ExcelComReader().parse("/x.xlsx")

        summary_lines = [r.message for r in caplog.records if "단계별 소요" in r.message]
        assert len(summary_lines) == 1
        summary = summary_lines[0]
        for stage in ("dispatch=", "open=", "sheets=", "cell_read=", "close="):
            assert stage in summary

    def test_com_stage_cleared_after_parse(self, monkeypatch):
        """파싱이 끝나면(성공이든 실패든) 모듈 레벨 단계 상태가 비어야 한다 —
        안 비우면 다음 파일 처리 전까지 워치독이 "이전 파일이 멈춰 있다"는
        오정보를 볼 수 있다."""
        from knowmate.secure import com_stage
        wb = _FakeWorkbook([_FakeSheet("Sheet1", [["v"]])])
        app = _FakeExcelApp(lambda *a, **kw: wb)
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        com_reader.ExcelComReader().parse("/x.xlsx")

        stage, path, _elapsed = com_stage.snapshot()
        assert stage is None and path is None

    def test_log_does_not_contain_cell_values(self, monkeypatch, caplog):
        """CLAUDE.md 원칙7: 로그에 셀 내용·문서 내용을 남기면 안 된다."""
        secret = "극비_연봉_1억2000만원"
        wb = _FakeWorkbook([_FakeSheet("Sheet1", [[secret]])])
        app = _FakeExcelApp(lambda *a, **kw: wb)
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        with caplog.at_level(logging.DEBUG):
            com_reader.ExcelComReader().parse("/x.xlsx")

        for record in caplog.records:
            assert secret not in record.message

    def test_failed_stage_reported_in_summary_log(self, monkeypatch, caplog):
        wb = _FakeWorkbook([_RaisingSheet()])
        app = _FakeExcelApp(lambda *a, **kw: wb)
        monkeypatch.setattr(com_reader, "_get_excel_app", lambda: app)

        with caplog.at_level(logging.DEBUG, logger="knowmate.secure.com_stage"):
            with pytest.raises(RuntimeError):
                com_reader.ExcelComReader().parse("/x.xlsx")

        summary_lines = [r.message for r in caplog.records if "단계별 소요" in r.message]
        assert len(summary_lines) == 1
        assert "실패단계=cell_read" in summary_lines[0]
