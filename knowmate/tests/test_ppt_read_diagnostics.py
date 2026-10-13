"""가짜 COM으로 PPT 추출 결과·비밀 비노출·진행 위치를 검증한다."""
import logging
import threading
from types import SimpleNamespace

import pytest

from knowmate.secure import com_reader, com_stage, ppt_diagnostics


class _Count:
    def __init__(self, number):
        self.number = number
        self.reads = 0

    @property
    def Count(self):
        self.reads += 1
        return self.number


class _Shape:
    def __init__(self, text=None, table=None, children=None, advance=lambda: None):
        self.Type = 6 if children is not None else 19 if table is not None else 1
        self.HasTable = table is not None
        self.HasTextFrame = text is not None
        self.GroupItems = children
        self.Table = table
        self._text = text
        self._advance = advance

    @property
    def TextFrame(self):
        self._advance()
        return SimpleNamespace(TextRange=SimpleNamespace(Text=self._text))

    @property
    def Name(self):
        pytest.fail("diagnostics must not fetch Shape.Name")


class _Table:
    def __init__(self, grid):
        self._grid = grid
        self.Rows = _Count(len(grid))
        self.Columns = _Count(len(grid[0]) if grid else 0)
        self.visited = []

    def Cell(self, row, column):
        self.visited.append((row, column))
        return SimpleNamespace(Shape=_Shape(text=self._grid[row - 1][column - 1]))


def _install_presentation(monkeypatch, slides):
    closed = []
    presentation = SimpleNamespace(Slides=slides, Close=lambda: closed.append(True))
    app = SimpleNamespace(Presentations=SimpleNamespace(Open=lambda *args, **kwargs: presentation))
    monkeypatch.setattr(com_reader, "_get_ppt_app", lambda: app)
    return closed


def test_parse_preserves_nested_groups_tables_text_and_counts(monkeypatch, caplog):
    table = _Table([["SECRET_HEADER", "SECRET_COLUMN"], ["SECRET_VALUE", "SECRET_CELL"]])
    slides = [
        SimpleNamespace(Shapes=[
            _Shape(children=[_Shape(text="SECRET_GROUP_TEXT"), _Shape(children=[_Shape(table=table)])]),
            _Shape(text="SECRET_TITLE"), _Shape(),
        ]),
        SimpleNamespace(Shapes=[_Shape(text="SECRET_SLIDE_TWO")]),
    ]
    closed = _install_presentation(monkeypatch, slides)
    with caplog.at_level(logging.DEBUG):
        result = com_reader.PowerPointComReader().parse("/synthetic/deck.pptx")
    assert result == (
        "SECRET_GROUP_TEXT\nSECRET_HEADER | SECRET_COLUMN\nSECRET_VALUE | SECRET_CELL\nSECRET_TITLE"
        "\n\nSECRET_SLIDE_TWO"
    )
    assert closed == [True]
    assert table.Rows.reads == table.Columns.reads == 1
    assert table.visited == [(1, 1), (1, 2), (2, 1), (2, 2)]
    assert "slide=1 shapes=6 top_shapes=3 groups=2 tables=1 cells=4 text_frames=2 max_group_depth=2 errors=0" in caplog.text
    assert "slide=2 shapes=1 top_shapes=1 groups=0 tables=0 cells=0 text_frames=1" in caplog.text
    assert "shape=1.2.1 kind=table type=19 depth=2 rows=2 columns=2" in caplog.text
    assert "cell_text:4/" in caplog.text
    assert "SECRET_" not in caplog.text
    assert com_stage.snapshot()[0] is None


def test_slow_shape_time_is_attributed_to_text_without_resetting_read_stage(monkeypatch, caplog):
    clock = SimpleNamespace(value=0.0)
    fake_time = SimpleNamespace(monotonic=lambda: clock.value)
    monkeypatch.setattr(com_reader, "time", fake_time)
    monkeypatch.setattr(ppt_diagnostics, "time", fake_time)
    monkeypatch.setattr(com_stage, "time", fake_time)
    observed = []

    def advance():
        clock.value += 4.0
        observed.append(com_stage.snapshot())

    _install_presentation(monkeypatch, [SimpleNamespace(Shapes=[
        _Shape(text="SECRET_ONE", advance=advance), _Shape(text="SECRET_TWO", advance=advance),
    ])])
    with caplog.at_level(logging.DEBUG):
        assert com_reader.PowerPointComReader().parse("/synthetic/deck.pptx") == "SECRET_ONE\nSECRET_TWO"
    assert [stage for stage, _, _ in observed] == ["read", "read"]
    assert [seconds for _, _, seconds in observed] == [4.0, 8.0]
    assert "text:2/8.00s" in caplog.text
    assert "slide=1 shapes=2" in caplog.text
    assert "seconds=8.00" in caplog.text
    assert "최장슬라이드=8.00s" in caplog.text
    assert "SECRET_" not in caplog.text


def test_empty_table_does_not_add_columns_query():
    table = _Table([])

    class EmptyTable:
        Rows = table.Rows

        @property
        def Columns(self):
            pytest.fail("old empty-table path never queried Columns")

    assert com_reader._ppt_shape_texts(_Shape(table=EmptyTable())) == []
    assert table.Rows.reads == 1


def test_recoverable_shape_error_is_logged_without_exception_body(monkeypatch, caplog):
    class UnreadableShape(_Shape):
        @property
        def TextFrame(self):
            raise RuntimeError("SECRET_EXCEPTION_BODY")

    _install_presentation(monkeypatch, [SimpleNamespace(Shapes=[
        UnreadableShape(text="ignored"), _Shape(text="SECRET_GOOD_TEXT"),
    ])])
    from knowmate.secure.ppt_addin_reader import PptPartialRead
    with caplog.at_level(logging.INFO), pytest.raises(PptPartialRead, match="partial_com_read"):
        com_reader.PowerPointComReader().parse("/synthetic/deck.pptx")
    assert "operation=text row=0 column=0 error_type=RuntimeError" in caplog.text
    assert "errors=1" in caplog.text
    assert "SECRET_" not in caplog.text


def test_fatal_shape_error_preserves_poison_and_skips_close(monkeypatch, caplog):
    class RpcError(Exception):
        hresult = -2147023174

    class DisconnectedShape(_Shape):
        @property
        def TextFrame(self):
            raise RpcError("SECRET_RPC_BODY")

    closed = _install_presentation(monkeypatch, [SimpleNamespace(Shapes=[DisconnectedShape(text="ignored")])])
    with caplog.at_level(logging.DEBUG), pytest.raises(com_reader.OfficeComPoisonError):
        com_reader.PowerPointComReader().parse("/synthetic/deck.pptx")
    assert closed == []
    assert "errors=1" in caplog.text
    assert "실패단계=read" in caplog.text
    assert com_stage.take_last_failed_stage() == "read"
    assert "SECRET_" not in caplog.text
    assert com_stage.snapshot()[0] is None


def test_progress_reports_blocked_cell_and_stops_without_com_on_monitor(monkeypatch, caplog):
    monkeypatch.setattr(ppt_diagnostics, "_PROGRESS_INTERVAL_SEC", 0.01)
    report_seen = threading.Event()
    release = threading.Event()
    owners = []
    errors = []
    diagnostic_instances = []

    class ReportHandler(logging.Handler):
        def emit(self, record):
            if "읽기 진행:" in record.getMessage():
                report_seen.set()

    handler = ReportHandler()
    ppt_diagnostics.logger.addHandler(handler)

    def worker():
        try:
            with ppt_diagnostics.PptReadDiagnostics("/synthetic/deck.pptx") as diagnostics:
                diagnostic_instances.append(diagnostics)

                def cell_read():
                    owners.append(threading.get_ident())
                    assert release.wait(3)
                    return "SECRET_BLOCKED_CELL"

                diagnostics.read("cell_text", cell_read, 7, (2, 3), 4, 5)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    try:
        with caplog.at_level(logging.INFO):
            thread.start()
            assert report_seen.wait(2)
            release.set()
            thread.join(3)
        assert not thread.is_alive()
        assert not errors
        assert owners == [thread.ident]
        assert not diagnostic_instances[0]._thread.is_alive()
        assert "slide=7 shape=2.3 operation=cell_text row=4 column=5 elapsed=" in caplog.text
        assert "SECRET_" not in caplog.text
    finally:
        release.set()
        thread.join(3)
        ppt_diagnostics.logger.removeHandler(handler)


def test_shape_enumeration_latency_is_in_slide_summary(monkeypatch, caplog):
    clock = SimpleNamespace(value=0.0)
    fake_time = SimpleNamespace(monotonic=lambda: clock.value)
    monkeypatch.setattr(com_reader, "time", fake_time)
    monkeypatch.setattr(ppt_diagnostics, "time", fake_time)

    class Shapes:
        def __iter__(self):
            clock.value += 2.0
            yield _Shape(text="SECRET_TEXT")

    _install_presentation(monkeypatch, [SimpleNamespace(Shapes=Shapes())])
    with caplog.at_level(logging.INFO):
        assert com_reader.PowerPointComReader().parse("/synthetic/deck.pptx") == "SECRET_TEXT"
    assert "shapes_next:2/2.00s" in caplog.text
    assert "seconds=2.00" in caplog.text
    assert "SECRET_" not in caplog.text
