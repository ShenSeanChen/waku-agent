"""TTS engine selection and TADA config are pure — no model, no audio device.

Hume TADA is opt-in: these pin that the default voice path is untouched and
that the engine fails loudly on a typo instead of silently falling back.
"""

import pytest

from waku.gateway.voice import (
    TadaConfig,
    _tada_config,
    _tada_waveform,
    _tts_engine,
)

ENGINE_CASES = [
    (None, False, "say"),
    (None, True, "kokoro"),
    ("", True, "kokoro"),
    ("say", True, "say"),
    ("kokoro", False, "kokoro"),
    ("  TADA ", False, "tada"),
    ("SAY", False, "say"),
]


@pytest.mark.parametrize("configured,kokoro,expected", ENGINE_CASES)
def test_engine_selection(configured, kokoro, expected):
    assert _tts_engine(configured, kokoro) == expected


@pytest.mark.parametrize(
    "configured,expected",
    [
        ("tadaa", "Unknown WAKU_TTS='tadaa'; expected one of: say, kokoro, tada"),
        ("elevenlabs", "Unknown WAKU_TTS='elevenlabs'; expected one of: say, kokoro, tada"),
    ],
)
def test_unknown_engine_is_rejected(configured, expected):
    with pytest.raises(ValueError) as exc:
        _tts_engine(configured, False)
    assert str(exc.value) == expected


def test_tada_defaults_stay_local_and_english():
    assert _tada_config({}) == TadaConfig(
        model="HumeAI/tada-1b",
        prompt_wav="",
        prompt_text=(
            "The examination and testimony of the experts, enabled the commission "
            "to conclude that five shots may have been fired."
        ),
        device="",
    )


def test_tada_env_overrides():
    cfg = _tada_config(
        {
            "WAKU_TADA_MODEL": "HumeAI/tada-3b-ml",
            "WAKU_TADA_PROMPT_WAV": "/tmp/voice.wav",
            "WAKU_TADA_PROMPT_TEXT": "  my clip transcript  ",
            "WAKU_TADA_DEVICE": " cuda ",
        }
    )
    assert cfg == TadaConfig(
        model="HumeAI/tada-3b-ml",
        prompt_wav="/tmp/voice.wav",
        prompt_text="my clip transcript",
        device="cuda",
    )


def test_blank_env_falls_back_to_defaults():
    cfg = _tada_config({"WAKU_TADA_MODEL": "  ", "WAKU_TADA_PROMPT_TEXT": ""})
    assert cfg.model == "HumeAI/tada-1b"
    assert cfg.prompt_text.startswith("The examination")


def test_custom_clip_without_transcript_is_rejected():
    """A foreign transcript would silently misalign against someone's own voice."""
    with pytest.raises(ValueError) as exc:
        _tada_config({"WAKU_TADA_PROMPT_WAV": "/tmp/voice.wav"})
    assert str(exc.value) == (
        "WAKU_TADA_PROMPT_WAV needs WAKU_TADA_PROMPT_TEXT: the clip's exact transcript"
    )


def test_custom_transcript_without_clip_is_rejected():
    with pytest.raises(ValueError) as exc:
        _tada_config({"WAKU_TADA_PROMPT_TEXT": "someone's transcript"})
    assert str(exc.value) == (
        "WAKU_TADA_PROMPT_TEXT needs WAKU_TADA_PROMPT_WAV: the clip it transcribes"
    )


class _Output:
    def __init__(self, audio):
        self.audio = audio


def test_waveform_takes_the_first_generation():
    first, second = object(), object()
    assert _tada_waveform(_Output([first, second])) is first


def test_empty_generation_is_rejected():
    with pytest.raises(ValueError) as exc:
        _tada_waveform(_Output([]))
    assert str(exc.value) == "TADA produced no audio"


def test_mouth_speaks_speakable_text_with_tada(monkeypatch):
    """The engine is wired into the existing gateway path, emoji stripping included."""
    from waku.gateway import voice

    spoken = []
    monkeypatch.setattr(voice._TadaEngine, "speak", lambda self, text: spoken.append(text))
    monkeypatch.setenv("WAKU_TTS", "tada")
    voice.Mouth().speak("hello 🚀 **world**")

    assert spoken == ["hello world"]
