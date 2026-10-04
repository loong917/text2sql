"""Resolve explicit calendar ranges with an injected reference date."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, timedelta


@dataclass(frozen=True)
class DateResolution:
    start: str | None = None
    end: str | None = None
    issue: str | None = None
    relative: bool = False
    number_spans: tuple[tuple[int, int], ...] = ()


def _month_start_after(year: int, month: int) -> date:
    return date(year + month // 12, month % 12 + 1, 1)


RELATIVE_PERIODS = {
    "今年": "this_year",
    "本年": "this_year",
    "去年": "last_year",
    "上年": "last_year",
    "本月": "this_month",
    "这个月": "this_month",
    "上月": "last_month",
    "上个月": "last_month",
    "今天": "today",
    "昨天": "yesterday",
}
ISO_DATE_PATTERN = r"(?<!\d)(?:19|20|21)\d{2}-\d{1,2}(?:-\d{1,2})?(?!\d)"
YEAR_RANGE_PATTERN = r"((?:19|20|21)\d{2})年?\s*(?:至|到|[-~～])\s*((?:19|20|21)\d{2})年?"


def _calendar_conflict(question: str) -> str | None:
    for pattern in (ISO_DATE_PATTERN, YEAR_RANGE_PATTERN, r"(?:19|20|21)\d{2}年"):
        for match in re.finditer(pattern, question):
            if re.search(
                r"(?:大于等于|小于等于|不等于|大于|小于|等于|[<>=≥≤])\s*$",
                question[: match.start()],
            ):
                return "日期形式的比较条件需要明确日期字段与完整边界，不能当作聚合数值或单日范围"
    periods = {period for term, period in RELATIVE_PERIODS.items() if term in question}
    if len(periods) > 1:
        return "多个相对时间周期需要明确连续范围或对比方式，不能只保留第一个周期"
    explicit_year = bool(re.search(r"(?:19|20|21)\d{2}年", question))
    iso_dates = list(re.finditer(ISO_DATE_PATTERN, question))
    if periods and (explicit_year or iso_dates):
        return "相对时间与绝对时间同时出现，需要明确各自范围，不能静默覆盖"
    if (
        periods
        and not periods.intersection({"this_year", "last_year"})
        and re.search(r"\d{1,2}月|\d{1,2}[日号]|季度|上半年|下半年", question)
    ):
        return "相对月或日期与其他日历范围冲突，需要明确完整起止日期"
    if iso_dates:
        remaining = re.sub(ISO_DATE_PATTERN, "", question)
        if re.search(r"(?:19|20|21)\d{2}年|\d{1,2}月|\d{1,2}[日号]|季度|上半年|下半年", remaining):
            return "ISO 日期与其他日历范围同时出现，需要明确唯一的起止范围"
    quarters = re.findall(r"(?:第)?(\d+|[一二三四五六七八九十]+)季度", question)
    valid_quarters = {"1", "2", "3", "4", "一", "二", "三", "四"}
    if any(value not in valid_quarters for value in quarters):
        return "季度无效，只能指定第一至第四季度"
    if len(set(quarters)) > 1:
        return "多个季度需要明确连续范围或对比方式"
    halves = {term for term in ("上半年", "下半年") if term in question}
    if len(halves) > 1:
        return "上下半年同时出现，需要明确全年或分期统计口径"
    if (quarters or halves) and re.search(r"\d{1,2}月|\d{1,2}[日号]", question):
        return "季度或半年与月份日期同时出现，需要明确唯一的日历范围"
    if quarters and halves:
        return "季度与半年同时出现，需要明确唯一的日历范围"
    if re.search(r"\d{1,2}[日号]", question) and not re.search(r"\d{1,2}月", question):
        return "日期需要明确月份，不能只提供年份和日号"
    return None


def resolve_date_range(question: str, today: date) -> DateResolution:
    """Resolve a single range and retain which numerals actually filled date slots."""
    issue = _calendar_conflict(question)
    if issue:
        return DateResolution(issue=issue)
    result = _resolve_date_range(question, today)
    if result.issue or not result.start:
        return result
    numeric_patterns = (
        ISO_DATE_PATTERN,
        YEAR_RANGE_PATTERN,
        r"(?<!\d)(?:19|20|21)\d{2}年度?",
        r"\d{1,2}月?\s*(?:至|到|[-~～])\s*\d{1,2}月",
        r"(?<!\d)\d{1,2}月",
        r"(?<!\d)\d{1,2}[日号]",
        r"(?:第)?[1-4]季度",
    )
    return replace(
        result,
        relative=any(term in question for term in RELATIVE_PERIODS),
        number_spans=tuple(
            match.span() for pattern in numeric_patterns for match in re.finditer(pattern, question)
        ),
    )


def _resolve_date_range(question: str, today: date) -> DateResolution:
    """Resolve half-open calendar ranges; ambiguous windows need clarification."""
    if re.search(
        r"最近|近\d|近[一二三四五六七八九十]|过去|截至|截止|之前|之后|以前|以后", question
    ):
        return DateResolution(
            issue="时间条件需要明确起止日期和包含边界，不能推测滚动窗口或单边范围"
        )
    iso_days = list(re.finditer(r"(?<!\d)((?:19|20|21)\d{2}-\d{1,2}-\d{1,2})(?!\d)", question))
    if iso_days:
        try:
            resolved = [date(*map(int, match.group(1).split("-"))) for match in iso_days]
            if len(resolved) == 1:
                return DateResolution(
                    resolved[0].isoformat(), (resolved[0] + timedelta(days=1)).isoformat()
                )
            if len(resolved) != 2 or not re.search(
                r"至|到|~|～", question[iso_days[0].end() : iso_days[1].start()]
            ):
                return DateResolution(issue="多个日期需要明确起止范围")
            if resolved[0] > resolved[1]:
                return DateResolution(issue="时间范围起始日期晚于结束日期")
            return DateResolution(
                resolved[0].isoformat(), (resolved[1] + timedelta(days=1)).isoformat()
            )
        except ValueError:
            return DateResolution(issue="日期无效，请确认年份、月份和日期")
    iso_months = list(re.finditer(r"(?<!\d)((?:19|20|21)\d{2})-(\d{1,2})(?![\d-])", question))
    if iso_months:
        try:
            year, month = map(int, iso_months[0].groups())
            start = date(year, month, 1)
            if len(iso_months) == 1:
                return DateResolution(
                    start.isoformat(), _month_start_after(year, month).isoformat()
                )
            if len(iso_months) != 2 or not re.search(
                r"至|到|~|～", question[iso_months[0].end() : iso_months[1].start()]
            ):
                return DateResolution(issue="多个月份需要明确起止范围")
            end_year, end_month = map(int, iso_months[1].groups())
            end = date(end_year, end_month, 1)
            if start > end:
                return DateResolution(issue="时间范围起始月份晚于结束月份")
            return DateResolution(
                start.isoformat(), _month_start_after(end_year, end_month).isoformat()
            )
        except ValueError:
            return DateResolution(issue="日期无效，请确认年份、月份和日期")
    years = re.findall(r"(?<!\d)((?:19|20|21)\d{2})(?=年)", question)
    range_match = re.search(YEAR_RANGE_PATTERN, question)
    if range_match:
        start_year, end_year = map(int, range_match.groups())
        if start_year > end_year:
            return DateResolution(issue="时间范围起始年份晚于结束年份")
        if re.search(r"\d{1,2}月|\d{1,2}日|季度|上半年|下半年", question):
            return DateResolution(issue="跨年月份或日期范围需要明确起止日期")
        return DateResolution(f"{start_year}-01-01", f"{end_year + 1}-01-01")
    if len(set(years)) > 1:
        return DateResolution(issue="多个年份需要明确连续范围或对比方式")
    if years:
        year = int(years[0])
    elif "今年" in question or "本年" in question:
        year = today.year
    elif "去年" in question or "上年" in question:
        year = today.year - 1
    elif "本月" in question or "这个月" in question:
        return DateResolution(
            date(today.year, today.month, 1).isoformat(),
            _month_start_after(today.year, today.month).isoformat(),
        )
    elif "上月" in question or "上个月" in question:
        month = today.month - 1 or 12
        year = today.year if today.month > 1 else today.year - 1
        return DateResolution(
            date(year, month, 1).isoformat(), date(today.year, today.month, 1).isoformat()
        )
    elif "今天" in question:
        return DateResolution(today.isoformat(), (today + timedelta(days=1)).isoformat())
    elif "昨天" in question:
        return DateResolution((today - timedelta(days=1)).isoformat(), today.isoformat())
    else:
        if re.search(r"(?:最近|近|过去|本周|上周|本季|上季|\d{1,2}月|\d{1,2}日)", question):
            return DateResolution(issue="时间条件需要明确年份、起止日期或滚动窗口口径")
        return DateResolution()

    month_range = re.search(r"(\d{1,2})月?\s*(?:至|到|[-~～])\s*(\d{1,2})月", question)
    months = re.findall(r"(?<!\d)(\d{1,2})月", question)
    days = re.findall(r"(?<!\d)(\d{1,2})[日号]", question)
    quarter = re.search(r"(?:第)?([1-4一二三四])季度", question)
    try:
        if month_range:
            start_month, end_month = map(int, month_range.groups())
            if not 1 <= start_month <= 12 or not 1 <= end_month <= 12:
                return DateResolution(issue="日期无效，请确认年份、月份和日期")
            if start_month > end_month:
                return DateResolution(issue="时间范围起始月份晚于结束月份")
            return DateResolution(
                date(year, start_month, 1).isoformat(),
                _month_start_after(year, end_month).isoformat(),
            )
        if len(set(months)) > 1 or len(set(days)) > 1:
            return DateResolution(issue="多个日期需要明确起止范围")
        if months:
            month = int(months[0])
            if days:
                start = date(year, month, int(days[0]))
                return DateResolution(start.isoformat(), (start + timedelta(days=1)).isoformat())
            return DateResolution(
                date(year, month, 1).isoformat(), _month_start_after(year, month).isoformat()
            )
        if quarter:
            quarter_number = (
                int(quarter.group(1))
                if quarter.group(1).isdigit()
                else "一二三四".index(quarter.group(1)) + 1
            )
            month = (quarter_number - 1) * 3 + 1
            return DateResolution(
                date(year, month, 1).isoformat(), _month_start_after(year, month + 2).isoformat()
            )
        if "上半年" in question:
            return DateResolution(f"{year}-01-01", f"{year}-07-01")
        if "下半年" in question:
            return DateResolution(f"{year}-07-01", f"{year + 1}-01-01")
        return DateResolution(f"{year}-01-01", f"{year + 1}-01-01")
    except ValueError:
        return DateResolution(issue="日期无效，请确认年份、月份和日期")
