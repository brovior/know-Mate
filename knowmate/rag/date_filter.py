"""질의 문자열에서 한국어 날짜 표현을 파싱해 (시작, 끝) epoch 범위로 변환한다.

규칙기반 파서 — 외부 의존성 없음, 결정적(now 주입 가능), 사외 완전 테스트 가능.
매칭되는 표현이 없으면 None을 반환해 호출측이 일반 벡터 검색으로 폴백하게 한다.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_KO_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _day_range(d: datetime) -> tuple[float, float]:
    """하루의 00:00:00 ~ 23:59:59.999999 epoch 범위를 반환한다."""
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    return start.timestamp(), end.timestamp()


def _week_range(d: datetime) -> tuple[float, float]:
    """d가 속한 주(월요일 시작)의 epoch 범위를 반환한다."""
    monday = d - timedelta(days=d.weekday())
    start, _ = _day_range(monday)
    sunday = monday + timedelta(days=6)
    _, end = _day_range(sunday)
    return start, end


def _month_range(year: int, month: int) -> tuple[float, float]:
    """해당 연·월의 epoch 범위를 반환한다."""
    start_d = datetime(year, month, 1)
    if month == 12:
        next_start = datetime(year + 1, 1, 1)
    else:
        next_start = datetime(year, month + 1, 1)
    start, _ = _day_range(start_d)
    end, _ = _day_range(next_start)
    return start, end - 0.000001


def _year_range(year: int) -> tuple[float, float]:
    """해당 연도의 epoch 범위를 반환한다."""
    start, _ = _day_range(datetime(year, 1, 1))
    end, _ = _day_range(datetime(year + 1, 1, 1))
    return start, end - 0.000001


def _iso_week_range(year: int, week: int) -> tuple[float, float]:
    """ISO 주차(연·주)의 월요일~일요일 epoch 범위를 반환한다."""
    # %G-W%V-%u: ISO 연도-주차-요일(1=월요일)
    monday = datetime.strptime(f"{year}-W{week:02d}-1", "%G-W%V-%u")
    return _week_range(monday)


def parse_date_range_ko(query: str, now: datetime | None = None) -> tuple[float, float] | None:
    """질의에서 한국어 날짜 표현을 찾아 (start_epoch, end_epoch)를 반환한다.

    지원 표현: 오늘/어제/그저께, 이번주/지난주/지지난주, 이번달/지난달,
    올해/작년, N월, N주차(ISO), 최근 N일/N주/N개월.
    매칭 실패 시 None (호출측은 날짜 필터 없이 일반 검색으로 폴백).
    """
    now = now or datetime.now()
    q = query.strip()

    # ── 최근 N일/N주/N개월 ──────────────────────────────────────
    m = re.search(r"최근\s*(\d+)\s*일", q)
    if m:
        n = int(m.group(1))
        start, _ = _day_range(now - timedelta(days=n))
        _, end = _day_range(now)
        return start, end

    m = re.search(r"최근\s*(\d+)\s*주", q)
    if m:
        n = int(m.group(1))
        start, _ = _day_range(now - timedelta(weeks=n))
        _, end = _day_range(now)
        return start, end

    m = re.search(r"최근\s*(\d+)\s*개?월", q)
    if m:
        n = int(m.group(1))
        # n개월 전 같은 날부터 오늘까지 (월말 경계는 근사)
        approx_start = now - timedelta(days=n * 30)
        start, _ = _day_range(approx_start)
        _, end = _day_range(now)
        return start, end

    # ── 그저께/어제/오늘 (구체적인 것부터 우선 매칭) ─────────────
    if "그저께" in q or "그제" in q:
        return _day_range(now - timedelta(days=2))
    if "어제" in q:
        return _day_range(now - timedelta(days=1))
    if "오늘" in q:
        return _day_range(now)

    # ── N주차 (ISO 주차, 연도 생략 시 올해) ───────────────────────
    m = re.search(r"(?:(\d{4})\s*년\s*)?(\d{1,2})\s*주\s*차", q)
    if m:
        year = int(m.group(1)) if m.group(1) else now.year
        week = int(m.group(2))
        try:
            return _iso_week_range(year, week)
        except ValueError:
            pass  # 유효하지 않은 주차면 다음 규칙으로

    # ── 지지난주/지난주/이번주 ────────────────────────────────────
    if "지지난주" in q:
        return _week_range(now - timedelta(weeks=2))
    if "지난주" in q or "저번주" in q:
        return _week_range(now - timedelta(weeks=1))
    if "이번주" in q or "금주" in q:
        return _week_range(now)

    # ── 지난달/이번달 ─────────────────────────────────────────────
    if "지난달" in q or "저번달" in q:
        first_of_this_month = now.replace(day=1)
        last_month_day = first_of_this_month - timedelta(days=1)
        return _month_range(last_month_day.year, last_month_day.month)
    if "이번달" in q or "이달" in q:
        return _month_range(now.year, now.month)

    # ── 작년/올해 ─────────────────────────────────────────────────
    if "작년" in q:
        return _year_range(now.year - 1)
    if "올해" in q:
        return _year_range(now.year)

    # ── N월 (연도 생략 시 올해, "N주차"와 겹치지 않도록 이후 매칭) ─
    m = re.search(r"(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월(?!\s*\d)", q)
    if m:
        year = int(m.group(1)) if m.group(1) else now.year
        month = int(m.group(2))
        if 1 <= month <= 12:
            return _month_range(year, month)

    return None
