"""The wake-word matcher is a pure function — so it gets deterministic evals.
Whisper mangles phrases in predictable ways; these cases pin the fuzziness."""

import pytest

from milli.gateway.voice import matches_wake

SHOULD_WAKE = [
    ("milli", "milli"),
    ("Milli!", "milli"),                        # punctuation
    ("so anyway milli schedule it", "milli"),    # embedded in speech
    ("milly", "milli"),                          # one-letter mangle → fuzzy match
    ("mili", "milli"),                           # dropped letter → fuzzy match
    ("Hey Milli", "hey milli"),
    ("hey computer, what's up", "hey computer"),
    # variants after a comma cover other scripts
    ("米莉", "milli,米莉"),
    ("你好米莉", "milli,米莉"),
    ("ミリ", "milli,ミリ"),
]

SHOULD_NOT_WAKE = [
    ("what a nice day", "milli"),
    ("wake up call at nine", "milli"),
    ("", "milli"),
    ("milli", ""),                               # no wake word configured
    ("milk delivery today", "milli"),
    ("give me a minute", "milli"),
]


@pytest.mark.parametrize("heard,wake", SHOULD_WAKE, ids=[h for h, _ in SHOULD_WAKE])
def test_wakes(heard, wake):
    assert matches_wake(heard, wake)


@pytest.mark.parametrize("heard,wake", SHOULD_NOT_WAKE, ids=[h or "empty" for h, _ in SHOULD_NOT_WAKE])
def test_stays_asleep(heard, wake):
    assert not matches_wake(heard, wake)
