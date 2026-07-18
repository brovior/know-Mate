"""날짜 필터 파서(rag/date_filter.py) 단위 테스트.

순수 datetime 기반이라 의존성 0 — 사외 환경에서 항상 전체 통과한다.
now를 고정 주입해 결정적으로 검증한다.
"""
from __future__ import annotations

from datetime import datetime

from knowmate.rag.date_filter import parse_date_range_ko

# 기준 시각: 2026-07-17(금) — ISO 29주차
NOW = datetime(2026, 7, 17, 15, 30, 0)


def _range(query: str):
    return parse_date_range_ko(query, now=NOW)


class TestNoMatch:
    def test_no_date_expression_returns_none(self):
        assert _range("설비점검 보고서 요약해줘") is None

    def test_unrelated_numbers_do_not_match(self):
        assert _range("2026년도 예산안 검토") is None


class TestRelativeDays:
    def test_today(self):
        start, end = _range("오늘 회의록")
        assert datetime.fromtimestamp(start) == datetime(2026, 7, 17, 0, 0, 0)
        assert datetime.fromtimestamp(end).date() == datetime(2026, 7, 17).date()

    def test_yesterday(self):
        start, end = _range("어제 온 메일")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 7, 16).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 7, 16).date()

    def test_day_before_yesterday(self):
        start, _ = _range("그저께 회의")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 7, 15).date()


class TestWeek:
    def test_this_week_monday_start(self):
        start, end = _range("이번주 보고서")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 7, 13).date()  # 월
        assert datetime.fromtimestamp(end).date() == datetime(2026, 7, 19).date()    # 일

    def test_last_week(self):
        start, end = _range("지난주 메일 정리")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 7, 6).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 7, 12).date()

    def test_two_weeks_ago(self):
        start, end = _range("지지난주 회의")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 6, 29).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 7, 5).date()


class TestMonth:
    def test_this_month(self):
        start, end = _range("이번달 계획")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 7, 1).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 7, 31).date()

    def test_last_month(self):
        start, end = _range("지난달 실적")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 6, 1).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 6, 30).date()

    def test_explicit_month_current_year(self):
        start, end = _range("3월 보고서")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 3, 1).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 3, 31).date()

    def test_explicit_month_with_year(self):
        start, end = _range("2025년 3월 자료")
        assert datetime.fromtimestamp(start).date() == datetime(2025, 3, 1).date()
        assert datetime.fromtimestamp(end).date() == datetime(2025, 3, 31).date()

    def test_december_year_boundary(self):
        start, end = _range("12월 결산")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 12, 1).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 12, 31).date()


class TestYear:
    def test_this_year(self):
        start, end = _range("올해 목표")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 1, 1).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 12, 31).date()

    def test_last_year(self):
        start, end = _range("작년 알람 폭주 처리 절차")
        assert datetime.fromtimestamp(start).date() == datetime(2025, 1, 1).date()
        assert datetime.fromtimestamp(end).date() == datetime(2025, 12, 31).date()


class TestIsoWeek:
    def test_week_number_current_year_matches_now(self):
        # NOW(2026-07-17)의 ISO 주차는 29주 → "이번주"와 동일 범위여야 함
        start, end = _range("29주차 회의록")
        this_week = _range("이번주")
        assert (start, end) == this_week

    def test_week_number_with_explicit_year(self):
        start, end = _range("2026년 25주차")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 6, 15).date()  # 월요일
        assert datetime.fromtimestamp(end).date() == datetime(2026, 6, 21).date()    # 일요일

    def test_invalid_week_number_falls_back(self):
        # 53주차가 없는 해 등 유효하지 않으면 다음 규칙(월 매칭 없음)으로 넘어가 None
        assert _range("99주차 자료") is None


class TestRecent:
    def test_recent_n_days(self):
        start, end = _range("최근 7일 메일")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 7, 10).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 7, 17).date()

    def test_recent_n_weeks(self):
        start, end = _range("최근 2주 자료")
        assert datetime.fromtimestamp(start).date() == datetime(2026, 7, 3).date()
        assert datetime.fromtimestamp(end).date() == datetime(2026, 7, 17).date()

    def test_recent_n_months(self):
        start, end = _range("최근 3개월 실적")
        assert datetime.fromtimestamp(end).date() == datetime(2026, 7, 17).date()
        # 근사(30일*3)이므로 대략 4월 중순 시작만 확인
        assert datetime.fromtimestamp(start).date() < datetime(2026, 5, 1).date()


class TestPriority:
    def test_specific_beats_generic_yesterday_over_today_substring(self):
        # "오늘" 문자열이 포함되지 않은 경우만 확인 — 우선순위 회귀 방지
        start, _ = _range("어제와 오늘 비교")
        # "그저께" 없음 → "어제"가 먼저 매칭되어야 함(오늘이 함께 있어도 어제 우선)
        assert datetime.fromtimestamp(start).date() == datetime(2026, 7, 16).date()
