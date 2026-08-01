"""What gets voiced is a pure function too. A TTS engine reads emoji out loud
("rocket", "sparkles"), so `_speakable` strips them (and stray markdown) before
speaking. Live bug from filming → pinned here."""

import pytest

from milli.gateway.voice import _speakable

STRIPPED = [
    ("All set! 🎉 Booked for Saturday. 🎾", "All set! Booked for Saturday."),
    ("Done ✅ — added 3 events 🚀🚀🚀", "Done — added 3 events"),
    ("Here you go 😊👍", "Here you go"),
    ("Nice 🇬🇧 flag", "Nice flag"),            # regional-indicator flag
    ("**Bold** and `code` and # heading", "Bold and code and heading"),
    ("plain words, nothing to strip", "plain words, nothing to strip"),
]


@pytest.mark.parametrize("raw,expected", STRIPPED)
def test_speakable_strips_emoji_and_markdown(raw, expected):
    assert _speakable(raw) == expected


def test_speakable_handles_empty():
    assert _speakable("") == ""
    assert _speakable("💥✨🔥") == ""   # all-emoji → nothing to say
