"""Word stress for Silero ``v5_cis_base`` (MIT).

The MIT-licensed Silero CIS models contain *no* auto-stress and no homograph
resolution: Russian text must carry a ``+`` before the stressed vowel of every
word (``к+ошка``), otherwise the voice guesses and reads menus unnaturally.

Three layers, cheapest first:

1. **Rules** – words with ``ё`` (always stressed), single-vowel words, words
   that already carry ``+``; unstressed clitics (prepositions, particles,
   conjunctions) are left without a mark on purpose – that is how they are
   pronounced.
2. **Built-in lexicon** (:data:`LEXICON`) – every word that can reach the TTS
   from the scenarios, the verbaliser (numbers 0–2100, dates, times, weekdays,
   months), specialties in all used cases and the demo CRM names.
   ``tools/stress_vocab.py --check`` / ``tests/test_stress.py`` fail the build
   when a new prompt word is not covered, so the dictionary stays complete.
3. **Fallback** – optional ``silero-stress`` (MIT, ~50 MB, ~0.5 ms/word) for
   out-of-vocabulary words such as doctor surnames imported from the Excel
   schedule.  Without it, OOV words are passed through unstressed and counted
   in :attr:`DictAccentor.unknown` so they can be added to the lexicon.

The accentor is idempotent: text that is already stressed is returned as is.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from typing import Protocol

VOWELS = "аеёиоуыэюя"

#: Unstressed function words (proclitics/enclitics): no ``+`` on purpose.
CLITICS = frozenset(
    "а и но да ли же бы б не ни в во к ко с со на за по до из у о об от при для без над под про через без "
    "же ль то".split()
)

#: Abbreviations read letter by letter or as a word; applied before the lexicon lookup (case-insensitive).
ABBREVIATIONS = {
    "смс": "эс-эм-+эс",
    "снилс": "сн+илс",
    "узи": "уз+и",
    "ддммгггг": "д+ень м+есяц г+од",
}

_LEXICON_TEXT = """
+августа автомат+ический +адрес аллерг+олог аллерг+ологу андр+еевна андр+ей +анна апр+еля
б+анковских ближ+айшая ближ+айшее ближ+айший в+ашу введ+ите ввест+и верн+уться в+ечером в+икторовна
возьм+ите волк+ов восемн+адцатого восемн+адцать в+осемь в+осемьдесят восемьс+от воскрес+енье восьм+ого
врач+а врач+у вр+еменем вр+емени вр+емя всег+о вт+орник втор+ого выходн+ой
гастроэнтер+олог гастроэнтер+ологу гинек+олог гинек+ологу г+ода голосов+ой городск+ая гот+ово гр+афику
д+анные д+анными д+ата д+ату двадц+атого дв+адцать двен+адцатого двен+адцать дв+ести девян+осто
девятн+адцатого девятн+адцать дев+ятого д+евять девятьс+от деж+урный декабр+я дермат+олог дермат+ологу
дес+ятого д+есять дет+алями дм+итрий д+оброго докум+ентов докум+енты друг+ого друг+ое друг+ой
ед+инства ел+ена +если заверш+ить з+аняли зап+исан зап+исано запис+аться з+аписей з+аписи зап+исывается
з+апись запиш+у звон+ок здр+авствуйте ив+ан иван+ова ив+анович ив+ановна +игоревна идентифиц+ирован
измен+ятся +или инфекцион+ист инфекцион+исту ищ+у и+юля и+юня кабин+ет карди+олог карди+ологу к+арте
к+арты к+ода к+оды ком+у конкр+етного кот+орому кузнец+ов лаборат+орию лаборат+ория л+ебедев л+ибо
мар+ия м+арта маршр+ут масс+аж м+ая м+есяц м+инус мог+у м+ожно мор+озова нажм+ите наз+ад назв+ать назов+ите
н+айден нал+ичие нам+ерение напомин+аем направл+ение нар+одного нач+ало начин+ая н+ашем невр+олог
невр+ологу н+есколько никогд+а никол+аевич ничег+о новог+одние н+омер ноябр+я н+ужно об+ед об+еда
обращ+аетесь об+ычному огранич+ения од+ин од+иннадцатого од+иннадцать одн+а одн+им октябр+я ол+ег +ольга
онк+олог онк+ологу опер+атор опер+атора опер+атором ортоп+ед ортоп+еду ост+авить отв+ета отменен+а
отмен+ить отоларинг+олог отоларинг+ологу отпр+авим оф+ормлена офтальм+олог офтальм+ологу п+авлович
п+аспорт паци+ент паци+ента паци+ентов педи+атр педи+атру п+ервого перев+од перевож+у перезвон+ите
перенесен+а перенест+и петр+ов петр+овна пл+аны п+оводу повтор+ить повтор+ять повтор+яю подбер+у
подтверд+ил подтверд+ить подтвержден+а подтвержд+ения подх+одит подход+ящее подход+ящие пож+алуйста
позвон+ите поликл+иника поликл+иники поликл+инику п+олис пол+ученное получ+или пом+очь пом+ощник
понед+ельник п+осле пр+аздники пр+аздничные пр+аздничных предлож+ить предсто+ящих прив+ивка прив+ивку
прос+ил пр+осим психи+атр психи+атру психотерап+евт психотерап+евту пульмон+олог пульмон+ологу
пятн+адцатого пятн+адцать п+ятница п+ятницу п+ятого пятьдес+ят пятьс+от раб+ота раб+отает раб+оты
разгов+ор разгов+ора р+аньше ревмат+олог ревмат+ологу реж+им рожд+ения с+айте своб+одного своб+одное
своб+одным своб+одных св+язи себ+я седьм+ого семн+адцатого семн+адцать с+емьдесят семьс+от сентябр+я
серг+еевич серг+еевна серг+ей с+идорова скаж+ите сказ+ал сказ+ать сл+едующий слов+ам сл+оты сл+ышу
смирн+ова сн+ова совпад+ает сожал+ению сообщ+ать с+орок сохранен+ы сохран+яется спас+ибо специал+ист
специал+иста специ+альность сп+исок сп+особов ср+азу сред+а ср+еду стомат+олог стомат+ологу субб+ота
субб+оту так+им так+ими так+ое телеф+она терап+евт терап+евта терап+евту т+олько травмат+олог
травмат+ологу тр+ебуется тр+етьего тридц+атого тр+идцать трин+адцатого трин+адцать тр+иста т+ысяча т+ысячи
ув+ерены уд+обнее уд+обный уж+е ук+азан ук+азанное ук+азанный ук+азаны ур+олог ур+ологу +утром феврал+я
физиотерап+евт физиотерап+евту флюорогр+афию флюорогр+афия хир+ург хир+ургу хот+ите час+а час+ов
челов+ека четв+ерг чет+ыре чет+ыреста четырн+адцатого четырн+адцать чт+обы шестн+адцатого шестн+адцать
шест+ого шестьдес+ят шестьс+от эндокрин+олог эндокрин+ологу +это +этот январ+я
з+аписью свободн+ы х+очется н+апротив зап+исаны п+ервый втор+ой тр+етий ут+очните м+есто
мин+ут мин+ута мин+уты п+олдень с+утки приним+ает приним+ают нед+еля нед+ели кабин+ета кабин+ете этаж+е эт+аж
"""

LEXICON: dict[str, str] = {w.replace("+", ""): w for w in _LEXICON_TEXT.split()}

_WORD_RE = re.compile(r"[А-Яа-яЁё+]+(?:-[А-Яа-яЁё+]+)*")


def auto_stress(word: str) -> str | None:
    """Rule layer: already stressed → as is; ``ё`` → stressed; one vowel → stressed; else ``None``."""
    if "+" in word:
        return word
    low = word.lower()
    if "ё" in low:
        i = low.index("ё")
        return word[:i] + "+" + word[i:]
    positions = [i for i, ch in enumerate(low) if ch in VOWELS]
    if not positions:
        return word  # no vowels: nothing to stress (consonant abbreviations)
    if len(positions) == 1:
        i = positions[0]
        return word[:i] + "+" + word[i:]
    return None


def _apply_template(original: str, stressed_lower: str) -> str:
    """Copy the ``+`` positions of ``stressed_lower`` onto ``original`` (keeps capitalisation)."""
    out = []
    j = 0
    for ch in stressed_lower:
        if ch == "+":
            out.append("+")
        else:
            out.append(original[j] if j < len(original) else ch)
            j += 1
    return "".join(out)


class Accentor(Protocol):
    name: str

    def __call__(self, text: str) -> str: ...


class DictAccentor:
    """Rules + lexicon + optional per-word fallback; counts unknown words."""

    name = "dict"

    def __init__(
        self,
        lexicon: dict[str, str] | None = None,
        clitics: frozenset[str] = CLITICS,
        fallback: Callable[[str], str | None] | None = None,
    ) -> None:
        self.lexicon = dict(LEXICON if lexicon is None else lexicon)
        self.clitics = clitics
        self.fallback = fallback
        self.unknown: Counter[str] = Counter()

    def stress_word(self, word: str) -> tuple[str, bool]:
        """→ (stressed word, known). Hyphenated words are stressed part by part."""
        if "+" in word:
            return word, True
        low = word.lower()
        if low in ABBREVIATIONS:
            rep = ABBREVIATIONS[low]
            return (_apply_template(word, rep) if rep.replace("+", "") == low else rep), True
        if low in self.clitics:
            return word, True
        if low in self.lexicon:
            return _apply_template(word, self.lexicon[low]), True
        if "-" in word:
            parts = [self.stress_word(p) for p in word.split("-")]
            return "-".join(p for p, _ in parts), all(k for _, k in parts)
        rule = auto_stress(word)
        if rule is not None:
            return rule, True
        if self.fallback is not None:
            got = self.fallback(word)
            if got:
                return got, True
        self.unknown[low] += 1
        return word, False

    def __call__(self, text: str) -> str:
        return _WORD_RE.sub(lambda m: self.stress_word(m.group(0))[0], text)

    def coverage(self, text: str) -> list[str]:
        """Words of ``text`` that no layer can stress (for the vocabulary check)."""
        missing: list[str] = []
        for m in _WORD_RE.finditer(text):
            _, known = self.stress_word(m.group(0))
            if not known:
                missing.append(m.group(0).lower())
        return missing


class SileroStressAccentor:
    """``silero-stress`` (MIT) – full Russian accentor with homograph disambiguation; imported lazily."""

    name = "silero-stress"

    def __init__(self, threads: int = 1) -> None:
        import torch  # type: ignore[import-not-found]
        from silero_stress import load_accentor  # type: ignore[import-not-found]

        torch.set_num_threads(threads)
        self._accentor = load_accentor()

    def __call__(self, text: str) -> str:
        return str(self._accentor(text))

    def word(self, word: str) -> str | None:
        got = self(word).strip()
        return got if "+" in got else None


class NoStress:
    name = "off"

    def __call__(self, text: str) -> str:
        return text


def load_accentor(mode: str = "auto") -> Accentor:
    """``dict`` – lexicon only; ``silero`` – silero-stress for everything; ``auto`` – lexicon with
    silero-stress for OOV words when the package is installed; ``off`` – pass-through."""
    mode = (mode or "auto").lower()
    if mode == "off":
        return NoStress()
    if mode == "dict":
        return DictAccentor()
    silero: SileroStressAccentor | None = None
    try:
        silero = SileroStressAccentor()
    except Exception:  # pragma: no cover - optional dependency
        silero = None
    if mode == "silero" and silero is not None:
        return silero
    return DictAccentor(fallback=silero.word if silero is not None else None)
