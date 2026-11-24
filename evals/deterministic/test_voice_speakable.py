"""Written dates and clock times must reach every TTS engine as spoken English."""

import pytest

from waku.gateway.speakable import _number, _ordinal, speakable_dates
from waku.gateway.voice import _speakable

SPOKEN = [
    ("2026-07-14", "July fourteenth, twenty twenty-six"),
    ("2026-07-14T09:00", "July fourteenth, twenty twenty-six at nine a.m."),
    # seconds and a UTC offset are dropped; the wall-clock time is what is said
    ("2026-07-14 17:05:30+02:00", "July fourteenth, twenty twenty-six at five oh five p.m."),
    ("2026-07-14T09:00:00.123456", "July fourteenth, twenty twenty-six at nine a.m."),
    ("Sat 11 5:00 p.m.", "Saturday the eleventh five p.m."),
    (
        "Norway versus England Sat 11 10:00 a.m. - 12:00 p.m.",
        "Norway versus England Saturday the eleventh ten a.m. to noon",
    ),
    ("14:05", "two oh five p.m."),
    ("12:00 a.m.", "midnight"),
    ("12:00 p.m.", "noon"),
    ("July 14, 2026", "July fourteenth, twenty twenty-six"),
    ("Jul 14th", "July fourteenth"),
    ("July 2026", "July twenty twenty-six"),
    ("21st", "twenty-first"),
    ("5pm", "five p.m."),
    ("Call me at 5 p.m. tomorrow", "Call me at five p.m. tomorrow"),
    ("Meeting 09:00 → 10:00", "Meeting nine a.m. to ten a.m."),
    ("Meeting at 9:00.", "Meeting at nine a.m."),
    (
        "The meeting moved to 2026-12-02T14:30.",
        "The meeting moved to December second, twenty twenty-six at two thirty p.m.",
    ),
    ("Start 9:00, end 10:00.", "Start nine a.m., end ten a.m."),
    ("2024-02-29", "February twenty-ninth, twenty twenty-four"),
    ("Feb 29, 2024", "February twenty-ninth, twenty twenty-four"),
    (
        "Meet https://x.test/2026-07-14 on 2026-07-14",
        "Meet https://x.test/2026-07-14 on July fourteenth, twenty twenty-six",
    ),
]


@pytest.mark.parametrize("raw,spoken", SPOKEN, ids=[raw for raw, _ in SPOKEN])
def test_dates_and_times_are_spoken(raw, spoken):
    assert speakable_dates(raw) == spoken


UNCHANGED = [
    "03/04/2026",        # ambiguous date order — never guess
    "2026-13-45",        # impossible month and day
    "2026-02-31",        # impossible calendar date
    "2026-02-29",        # not a leap year
    "https://example.com/2026-07-14/report",
    "See docs at https://x.test/2026-07-14T09:00",
    "9:00-ish",          # approximation, not a clock time
    "3 tools and 10 iterations",
    "Step 3.7 Flash",
]


@pytest.mark.parametrize("raw", UNCHANGED)
def test_unrelated_or_ambiguous_text_is_untouched(raw):
    assert speakable_dates(raw) == raw


@pytest.mark.parametrize(
    "value,word",
    [
        (0, "zero"), (9, "nine"), (10, "ten"), (11, "eleven"), (20, "twenty"),
        (21, "twenty-one"), (100, "one hundred"), (101, "one hundred one"),
        (2026, "two thousand twenty-six"),
    ],
)
def test_number_words(value, word):
    assert _number(value) == word


@pytest.mark.parametrize(
    "value,word",
    [(1, "first"), (3, "third"), (5, "fifth"), (12, "twelfth"), (21, "twenty-first")],
)
def test_ordinal_words(value, word):
    assert _ordinal(value) == word


def test_the_gateway_speaks_dates_after_stripping_markdown():
    text = "🚀 Meeting **Sat 11 5:00 p.m.**"
    assert _speakable(text) == "Meeting Saturday the eleventh five p.m."


# Full replies in the shape the calendar tools produce — the failure from the
# demo clip, pinned end to end rather than as isolated tokens.
REPLIES = [
    (
        "Here is what is on your local calendar from today onward.",
        "Here is what is on your local calendar from today onward.",
    ),
    (
        "Swim with Sergey: Sat 11 5:00 p.m.",
        "Swim with Sergey: Saturday the eleventh five p.m.",
    ),
    (
        "Norway versus England: Sat 11 10:00 a.m. - 12:00 p.m.",
        "Norway versus England: Saturday the eleventh ten a.m. to noon",
    ),
    (
        "Created event 2026-07-14T09:00 → 2026-07-14T10:00",
        (
            "Created event July fourteenth, twenty twenty-six at nine a.m. "
            "to July fourteenth, twenty twenty-six at ten a.m."
        ),
    ),
]


@pytest.mark.parametrize("reply,spoken", REPLIES, ids=[r for r, _ in REPLIES])
def test_calendar_replies_are_spoken_correctly(reply, spoken):
    assert _speakable(reply) == spoken


def test_installing_tada_does_not_change_the_default_engine(monkeypatch):
    """The extra is opt-in: the model being installed must not select it."""
    from waku.gateway import voice

    monkeypatch.delenv("WAKU_TTS", raising=False)
    assert voice.Mouth().engine in {"say", "kokoro"}
