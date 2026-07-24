from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

TIME_RANGE_PATTERN = re.compile(r"^(\d{2}):(\d{2}) - (\d{2}):(\d{2})$")
LUNCH_WARNING_MINUTES = 300
LUNCH_WARNING = "Из рабочего времени будет вычтен 1 час на обед"


@dataclass(frozen=True)
class WorkTimeRange:
    start_minutes: int
    end_minutes: int

    @property
    def is_overnight(self) -> bool:
        return self.end_minutes < self.start_minutes

    @property
    def duration_minutes(self) -> int:
        if self.is_overnight:
            return 24 * 60 - self.start_minutes + self.end_minutes
        return self.end_minutes - self.start_minutes

    @property
    def normalized(self) -> str:
        return f"{_format_minutes(self.start_minutes)} - {_format_minutes(self.end_minutes)}"

    @property
    def lunch_warning(self) -> bool:
        return self.duration_minutes >= LUNCH_WARNING_MINUTES


@dataclass(frozen=True)
class WorkTimeSegment:
    work_date: date
    time_range: str


def _format_minutes(value: int) -> str:
    if value == 24 * 60:
        return "00:00"
    hours, minutes = divmod(value, 60)
    return f"{hours:02d}:{minutes:02d}"


def parse_work_time(value: str) -> WorkTimeRange:
    match = TIME_RANGE_PATTERN.fullmatch(value or "")
    if not match:
        raise ValueError("Некорректный формат времени (ожидается HH:MM - HH:MM)")

    start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
    if start_hour > 23 or end_hour > 23 or start_minute > 59 or end_minute > 59:
        raise ValueError("Некорректное время")

    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        raise ValueError("Продолжительность рабочего интервала должна быть больше нуля")
    if end == 0:
        end = 24 * 60
    return WorkTimeRange(start_minutes=start, end_minutes=end)


def validate_time_range(value: str) -> bool:
    try:
        parse_work_time(value)
    except ValueError:
        return False
    return True


def duration_minutes(value: str) -> int:
    return parse_work_time(value).duration_minutes


def needs_lunch_warning(value: str) -> bool:
    return parse_work_time(value).lunch_warning


def split_overnight_interval(work_date: date | str, value: str) -> list[WorkTimeSegment]:
    parsed_date = (
        datetime.strptime(work_date, "%Y-%m-%d").date()
        if isinstance(work_date, str)
        else work_date
    )
    interval = parse_work_time(value)
    if not interval.is_overnight:
        return [WorkTimeSegment(parsed_date, interval.normalized)]

    segments = [
        WorkTimeSegment(parsed_date, f"{_format_minutes(interval.start_minutes)} - 00:00"),
    ]
    if interval.end_minutes:
        segments.append(
            WorkTimeSegment(parsed_date + timedelta(days=1), f"00:00 - {_format_minutes(interval.end_minutes)}")
        )
    return segments


split_overnight = split_overnight_interval
