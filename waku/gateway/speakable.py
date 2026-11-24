"""Rewrite written dates and clock times as spoken English before synthesis.

The calendar demo read "Sat 11 5:00 p.m." as "sat gel 11 5006 00:00 p.m.".
That is a normalization failure, not a model failure: ``say``, Kokoro and TADA
all receive text written in a form nobody says out loud. This module is a pure
pre-synthesis pass — no model, no I/O, no dependency outside the stdlib.

Ambiguous forms are deliberately left alone. A bare ``03/04/2026`` means
different days on different sides of the Atlantic, so guessing here would turn
a pronunciation bug into a wrong date.
"""

from __future__ import annotations

import re
from datetime import date

_URL = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)

_MONTHS = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December",
}
_WEEKDAYS = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}
_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
)
_ORDINAL_LAST = {
    "one": "first", "two": "second", "three": "third", "five": "fifth",
    "eight": "eighth", "nine": "ninth", "twelve": "twelfth",
    "twenty": "twentieth", "thirty": "thirtieth", "forty": "fortieth",
    "fifty": "fiftieth", "sixty": "sixtieth", "seventy": "seventieth",
    "eighty": "eightieth", "ninety": "ninetieth",
}

_MONTH_LOOKUP = {}
for _number_value, _name in _MONTHS.items():
    _MONTH_LOOKUP[_name.lower()] = _number_value
    _MONTH_LOOKUP[_name[:3].lower()] = _number_value
_MONTH_LOOKUP["sept"] = 9

_MONTH_WORDS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
    "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_WEEKDAY_WORDS = (
    "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
    "|Mon|Tue|Wed|Thu|Fri|Sat|Sun"
)
_TIME_TOKEN = r"\d{1,2}:\d{2}(?:\s*[ap]\.?\s*m\.?)?"


def _number(value: int) -> str:
    """English for the ranges dates and clocks need (0..9999)."""
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        return f"{_TENS[tens]}-{_ONES[ones]}" if ones else _TENS[tens]
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {_number(rest)}" if rest else "")
    thousands, rest = divmod(value, 1000)
    return f"{_ONES[thousands]} thousand" + (f" {_number(rest)}" if rest else "")


def _ordinal(value: int) -> str:
    words = _number(value).split()
    last = words[-1]
    if "-" in last:  # twenty-one -> twenty-first, not twenty-oneth
        head, tail = last.split("-", 1)
        words[-1] = f"{head}-{_ORDINAL_LAST.get(tail, tail + 'th')}"
    else:
        words[-1] = _ORDINAL_LAST.get(last, last if last.endswith("th") else last + "th")
    return " ".join(words)


def _year(value: int) -> str:
    # "twenty twenty six" is how a year is read aloud. The hyphenated form
    # ("twenty twenty-six") is spoken by TADA as "twenty twenty ... six".
    if 2000 <= value <= 2099:
        return f"{_number(value // 100)} {_number(value % 100).replace('-', ' ')}"
    return _number(value)


def _clock(hour: int, minute: int) -> str:
    """24-hour numbers to spoken 12-hour time, e.g. (14, 5) -> 'two oh five p.m.'."""
    suffix = "a.m." if hour < 12 else "p.m."
    if hour == 12 and minute == 0:
        return "noon" if suffix == "p.m." else "midnight"
    hour12 = hour % 12 or 12
    if minute == 0:
        spoken = _number(hour12)
    elif minute < 10:
        spoken = f"{_number(hour12)} oh {_number(minute)}"
    else:
        spoken = f"{_number(hour12)} {_number(minute)}"
    return f"{spoken} {suffix}"


def _clock_at(hour: int, minute: int, suffix: str) -> str:
    """A clock whose a.m./p.m. is written explicitly, so 12:00 is not guessed."""
    if hour == 12 and minute == 0:
        return "noon" if suffix.lower().startswith("p") else "midnight"
    spoken = _clock(hour % 12 + (12 if suffix.lower().startswith("p") else 0), minute)
    return spoken


def _date(year: int, month: int, day: int) -> str:
    return f"{_MONTHS[month]} {_ordinal(day)}, {_year(year)}"


def _valid_date(year: int, month: int, day: int) -> bool:
    """Reject impossible dates instead of speaking a hallucination as fact."""
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _no_double_stop(match: re.Match, spoken: str) -> str:
    """Keep the sentence's own period; the spoken a.m./p.m. already has one."""
    if spoken.endswith(".") and match.string[match.end() : match.end() + 1] == ".":
        return spoken[:-1]
    return spoken


def _normalize_dates(text: str) -> str:
    """Expand unambiguous dates and times inside one non-URL segment."""
    if not text:
        return text

    def iso_datetime(match: re.Match) -> str:
        year, month, day, hour, minute = (int(match.group(i)) for i in range(1, 6))
        if not (
            1 <= month <= 12
            and 1 <= day <= 31
            and hour <= 23
            and minute <= 59
            and _valid_date(year, month, day)
        ):
            return match.group(0)
        return _no_double_stop(match, f"{_date(year, month, day)} at {_clock(hour, minute)}")

    def iso_date(match: re.Match) -> str:
        year, month, day = (int(g) for g in match.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31 and _valid_date(year, month, day)):
            return match.group(0)
        return _date(year, month, day)

    def month_day(match: re.Match) -> str:
        month = _MONTH_LOOKUP.get(match.group(1).lower())
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else 2000  # leap year validates Feb 29
        if month is None or not _valid_date(year, month, day):
            return match.group(0)
        spoken = f"{_MONTHS[month]} {_ordinal(day)}"
        return f"{spoken}, {_year(year)}" if match.group(3) else spoken

    def weekday_day(match: re.Match) -> str:
        weekday = _WEEKDAYS.get(match.group(1).lower()[:3])
        day = int(match.group(2))
        if weekday is None or not _valid_date(2000, 1, day):  # January has 31 days
            return match.group(0)
        return f"{weekday} the {_ordinal(day)}"

    def month_year(match: re.Match) -> str:
        month = _MONTH_LOOKUP.get(match.group(1).lower())
        if month is None:
            return match.group(0)
        return f"{_MONTHS[month]} {_year(int(match.group(2)))}"

    def clock_ampm(match: re.Match) -> str:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return match.group(0)
        return _no_double_stop(match, _clock_at(hour, minute, match.group(3)))

    def bare_hour(match: re.Match) -> str:
        hour = int(match.group(1))
        if hour > 23:
            return match.group(0)
        return _no_double_stop(match, _clock_at(hour, 0, match.group(2)))

    def clock_24(match: re.Match) -> str:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return match.group(0)
        return _no_double_stop(match, _clock(hour, minute))

    def ordinal(match: re.Match) -> str:
        value = int(match.group(1))
        return match.group(0) if not 1 <= value <= 31 else _ordinal(value)

    # Ranges first: the lookahead keeps the second clock untouched so the
    # clock rules below still see it.
    text = re.sub(
        rf"({_TIME_TOKEN})\s*(?:-|–|—|→|to)\s*(?={_TIME_TOKEN})",
        r"\1 to ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*→\s*", " to ", text)
    text = re.sub(
        r"\b(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?"
        r"(?:Z|[+-]\d{2}:?\d{2})?\b",
        iso_datetime,
        text,
    )
    text = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", iso_date, text)
    text = re.sub(
        rf"\b({_MONTH_WORDS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?"
        r"(?:,\s*(\d{4}))?\b",
        month_day,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"\b({_WEEKDAY_WORDS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
        weekday_day,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"\b({_MONTH_WORDS})\.?\s+(\d{{4}})\b", month_year, text, flags=re.IGNORECASE
    )
    text = re.sub(
        r"\b(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?(?!\w)",
        clock_ampm,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b(?![\w-])", clock_24, text)
    text = re.sub(r"\b(\d{1,2})\s*([ap])\.?\s*m\.?(?!\w)", bare_hour, text, flags=re.IGNORECASE)
    text = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", ordinal, text, flags=re.IGNORECASE)
    return text


def speakable_dates(text: str) -> str:
    """Expand dates and times for speech, leaving URLs byte-for-byte intact."""
    if not text:
        return text
    parts = _URL.split(text)
    # split() with a capturing group keeps URLs at odd indices.
    return "".join(part if index % 2 else _normalize_dates(part) for index, part in enumerate(parts))
