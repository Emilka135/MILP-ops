"""Word stress for the MIT Silero v5_cis_base model: rules, lexicon coverage of the whole prompt vocabulary, TTS wiring."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from calls.tts.silero import DEFAULT_MODEL, build_ssml, model_kind
from calls.tts.stress import CLITICS, LEXICON, VOWELS, DictAccentor, NoStress, auto_stress, load_accentor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import stress_vocab  # noqa: E402


@pytest.fixture
def acc() -> DictAccentor:
    return DictAccentor()


def test_lexicon_entries_are_well_formed():
    for plain, stressed in LEXICON.items():
        assert stressed.count("+") == 1, stressed
        i = stressed.index("+")
        assert stressed[i + 1] in VOWELS, stressed
        assert stressed.replace("+", "") == plain
        assert plain.islower()


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("ещё", "ещ+ё"),  # ё is always stressed
        ("приём", "при+ём"),
        ("нет", "н+ет"),  # single vowel
        ("смс", "эс-эм-+эс"),  # abbreviation read letter by letter
        ("к+ошка", "к+ошка"),  # already stressed → untouched
        ("двадцать", "дв+адцать"),
        ("четырнадцатого", "четырн+адцатого"),
        ("октября", "октябр+я"),
        ("терапевту", "терап+евту"),
        ("Здравствуйте", "Здр+авствуйте"),  # capitalisation preserved
        ("ОКТЯБРЯ", "ОКТЯБР+Я"),
    ],
)
def test_single_words(acc, word, expected):
    got, known = acc.stress_word(word)
    assert (got, known) == (expected, True)


def test_clitics_stay_unstressed(acc):
    for w in ("в", "на", "к", "не", "и", "по", "до", "за", "у"):
        assert w in CLITICS
        assert acc.stress_word(w) == (w, True)
    assert acc("запись к терапевту на пятницу") == "з+апись к терап+евту на п+ятницу"


def test_auto_rules():
    assert auto_stress("всё") == "вс+ё"
    assert auto_stress("да") == "д+а"
    assert auto_stress("ддммгггг") == "ддммгггг"  # no vowels
    assert auto_stress("поликлиника") is None  # several vowels → lexicon needed
    assert auto_stress("уж+е") == "уж+е"


def test_sentence_and_idempotency(acc):
    text = "Запись к хирургу пятнадцатого октября, в среду, в четырнадцать тридцать. Мы отправим СМС."
    once = acc(text)
    assert once == "З+апись к хир+ургу пятн+адцатого октябр+я, в ср+еду, в четырн+адцать тр+идцать. М+ы отпр+авим эс-эм-+эс."
    assert acc(once) == once
    assert acc.unknown == {}


def test_unknown_words_are_counted_and_passed_through(acc):
    got, known = acc.stress_word("Пупкинзаде")
    assert (got, known) == ("Пупкинзаде", False)
    assert acc.unknown["пупкинзаде"] >= 1
    assert acc.coverage("Врач Пупкинзаде работает в пятницу") == ["пупкинзаде"]


def test_fallback_is_used_for_oov_only():
    calls: list[str] = []

    def fb(word: str) -> str | None:
        calls.append(word)
        return "Пупкинзад+е"

    a = DictAccentor(fallback=fb)
    assert a("Врач Пупкинзаде работает") == "Вр+ач Пупкинзад+е раб+отает"
    assert calls == ["Пупкинзаде"]  # lexicon words never hit the fallback


def test_whole_prompt_vocabulary_is_covered():
    """Every word that can reach the TTS (scenarios, verbaliser, CRM demo data) has a stress mark."""
    words = stress_vocab.vocabulary(stress_vocab.collect_texts())
    assert len(words) > 350
    missing = stress_vocab.missing_words(words)
    assert missing == [], f"add to LEXICON: {' '.join(missing)}"


def test_vocab_tool_check_passes():
    assert stress_vocab.main(["--check"]) == 0


def test_build_ssml_applies_stress_and_rate(acc):
    ssml, preset = build_ssml("Нажмите 1, чтобы записаться", 1.5, acc)
    assert ssml == '<speak><prosody rate="fast">Нажм+ите 1, чт+обы запис+аться</prosody></speak>'
    assert preset == "fast"
    ssml_plain, preset_plain = build_ssml("Нажмите 1", 1.0, NoStress())
    assert "+" not in ssml_plain
    assert preset_plain in ("medium", "x-slow", "slow")


def test_model_kind_and_defaults():
    assert model_kind(DEFAULT_MODEL) == "cis"
    assert model_kind("modules/tts/models/v5_cis_base.pt") == "cis"
    assert model_kind("modules/tts/models/silero-v5_5_ru.pt") == "ru"
    assert DEFAULT_MODEL.endswith("v5_cis_base.pt")


def test_load_accentor_modes():
    assert load_accentor("off").name == "off"
    assert isinstance(load_accentor("dict"), DictAccentor)
    a = load_accentor("auto")  # silero-stress absent in CI → lexicon without fallback, no crash
    assert isinstance(a, DictAccentor)
