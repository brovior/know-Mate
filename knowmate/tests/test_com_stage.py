"""COM 추출 단계 계측(knowmate/secure/com_stage.py) 단위 테스트.

목적: DRM 문서 등에서 COM hang이 어느 단계(dispatch/open/sheets/cell_read/read/close)
에서 발생했는지 로그 한 줄로 판단할 수 있어야 한다(COM 처리 안정화 요구). win32/COM에
의존하지 않는 순수 파이썬 모듈이라 사외 환경에서도 전부 통과한다.
"""
import time

import pytest

from knowmate.secure import com_stage


@pytest.fixture(autouse=True)
def _clear_stage_state():
    """테스트 간 모듈 레벨 상태가 새지 않도록 전후로 초기화한다."""
    com_stage.clear()
    com_stage.take_last_failed_stage()
    yield
    com_stage.clear()
    com_stage.take_last_failed_stage()


class TestBeginClearSnapshot:
    def test_snapshot_empty_by_default(self):
        stage, path, elapsed = com_stage.snapshot()
        assert stage is None and path is None and elapsed == 0.0

    def test_begin_then_snapshot_reflects_current_stage(self):
        com_stage.begin(com_stage.STAGE_OPEN, "/x/y.xlsx")
        stage, path, elapsed = com_stage.snapshot()
        assert stage == com_stage.STAGE_OPEN
        assert path == "/x/y.xlsx"
        assert elapsed >= 0.0

    def test_clear_resets_to_empty(self):
        com_stage.begin(com_stage.STAGE_OPEN, "/x/y.xlsx")
        com_stage.clear()
        stage, path, elapsed = com_stage.snapshot()
        assert stage is None and path is None and elapsed == 0.0

    def test_begin_overwrites_previous_stage(self):
        """단계는 순차 진행이므로 새 begin()은 이전 단계 정보를 덮어쓴다."""
        com_stage.begin(com_stage.STAGE_DISPATCH, "/a.xlsx")
        com_stage.begin(com_stage.STAGE_OPEN, "/a.xlsx")
        stage, _path, _elapsed = com_stage.snapshot()
        assert stage == com_stage.STAGE_OPEN

    def test_elapsed_grows_over_time(self):
        com_stage.begin(com_stage.STAGE_CELL_READ, "/big.xlsx")
        time.sleep(0.02)
        _stage, _path, elapsed = com_stage.snapshot()
        assert elapsed >= 0.02


class TestCurrentStageName:
    def test_unknown_when_not_started(self):
        assert com_stage.current_stage_name() == "unknown"

    def test_returns_stage_name_only(self):
        com_stage.begin(com_stage.STAGE_SHEETS, "/x.xlsx")
        assert com_stage.current_stage_name() == "sheets"


class TestDescribe:
    def test_no_stage_info_message(self):
        assert com_stage.describe() == "단계 정보 없음"

    def test_includes_stage_elapsed_and_path_no_cell_values(self):
        com_stage.begin(com_stage.STAGE_OPEN, "/docs/secret.xlsx")
        desc = com_stage.describe()
        assert desc.startswith("open(")
        assert "/docs/secret.xlsx" in desc
        # 셀 값·본문이 들어갈 여지 자체가 없는 포맷임을 재확인(경로·시간만)
        assert desc.count(" ") <= 2


class TestStageTimer:
    def test_stage_records_duration(self):
        timer = com_stage.StageTimer("/x.xlsx")
        with timer.stage(com_stage.STAGE_DISPATCH):
            time.sleep(0.01)
        assert timer.failed_stage is None
        assert "dispatch=" in timer.summary()

    def test_multiple_stages_recorded_in_order(self):
        timer = com_stage.StageTimer("/x.xlsx")
        with timer.stage(com_stage.STAGE_DISPATCH):
            pass
        with timer.stage(com_stage.STAGE_OPEN):
            pass
        with timer.stage(com_stage.STAGE_CELL_READ):
            pass
        summary = timer.summary()
        assert "dispatch=" in summary and "open=" in summary and "cell_read=" in summary

    def test_stage_publishes_to_module_level_during_execution(self):
        """블록 안에서는 com_stage.snapshot()으로 다른 스레드가(워치독 등) 현재
        단계를 읽을 수 있어야 한다."""
        timer = com_stage.StageTimer("/x.xlsx")
        observed = {}
        with timer.stage(com_stage.STAGE_CELL_READ):
            observed["stage"], observed["path"], _ = com_stage.snapshot()
        assert observed == {"stage": com_stage.STAGE_CELL_READ, "path": "/x.xlsx"}

    def test_exception_records_failed_stage_and_propagates(self):
        timer = com_stage.StageTimer("/x.xlsx")
        with pytest.raises(ValueError):
            with timer.stage(com_stage.STAGE_CELL_READ):
                raise ValueError("셀 읽기 실패(시뮬레이션)")
        assert timer.failed_stage == com_stage.STAGE_CELL_READ
        assert "실패단계=cell_read" in timer.summary()

    def test_only_first_failed_stage_is_recorded(self):
        """실패 후 finally에서 close 단계도 실패해도, 최초 실패 단계만 의미가 있다."""
        timer = com_stage.StageTimer("/x.xlsx")
        with pytest.raises(ValueError):
            with timer.stage(com_stage.STAGE_CELL_READ):
                raise ValueError("셀 읽기 실패")
        with pytest.raises(RuntimeError):
            with timer.stage(com_stage.STAGE_CLOSE):
                raise RuntimeError("닫기도 실패")
        assert timer.failed_stage == com_stage.STAGE_CELL_READ  # close로 덮이지 않음

    def test_summary_never_contains_cell_values(self):
        """로그에 셀 내용·문서 내용이 절대 남으면 안 된다(CLAUDE.md 원칙7) — summary는
        단계명·소요시간만으로 구성된 문자열이어야 한다."""
        timer = com_stage.StageTimer("/docs/salary.xlsx")
        with timer.stage(com_stage.STAGE_CELL_READ):
            pass
        summary = timer.summary()
        assert "salary" not in summary  # 경로조차 summary()엔 없음(log_summary가 별도로 붙임)

    def test_log_summary_includes_path_but_not_cell_content(self, caplog):
        import logging
        timer = com_stage.StageTimer("/docs/salary.xlsx")
        with timer.stage(com_stage.STAGE_OPEN):
            pass
        with caplog.at_level(logging.DEBUG, logger="knowmate.secure.com_stage"):
            timer.log_summary()
        messages = [r.message for r in caplog.records]
        assert any("open=" in m and "/docs/salary.xlsx" in m for m in messages)


class TestTakeLastFailedStage:
    """3차(실패 원인 분류 및 기록) — 스케줄러가 실패 단계를 읽어갈 통로."""

    def test_none_when_no_failure(self):
        assert com_stage.take_last_failed_stage() is None

    def test_returns_failed_stage_after_exception(self):
        timer = com_stage.StageTimer("/x.xlsx")
        with pytest.raises(ValueError):
            with timer.stage(com_stage.STAGE_CELL_READ):
                raise ValueError("실패")
        assert com_stage.take_last_failed_stage() == com_stage.STAGE_CELL_READ

    def test_reading_clears_the_slot(self):
        timer = com_stage.StageTimer("/x.xlsx")
        with pytest.raises(ValueError):
            with timer.stage(com_stage.STAGE_OPEN):
                raise ValueError("실패")
        assert com_stage.take_last_failed_stage() == com_stage.STAGE_OPEN
        assert com_stage.take_last_failed_stage() is None  # 두 번째 호출은 비어있음

    def test_success_does_not_set_failed_stage(self):
        timer = com_stage.StageTimer("/x.xlsx")
        with timer.stage(com_stage.STAGE_OPEN):
            pass
        assert com_stage.take_last_failed_stage() is None

    def test_only_first_failed_stage_published(self):
        """StageTimer.failed_stage와 동일하게, 첫 실패 단계만 모듈 슬롯에 남는다."""
        timer = com_stage.StageTimer("/x.xlsx")
        with pytest.raises(ValueError):
            with timer.stage(com_stage.STAGE_CELL_READ):
                raise ValueError("셀 읽기 실패")
        with pytest.raises(RuntimeError):
            with timer.stage(com_stage.STAGE_CLOSE):
                raise RuntimeError("닫기도 실패")
        assert com_stage.take_last_failed_stage() == com_stage.STAGE_CELL_READ
