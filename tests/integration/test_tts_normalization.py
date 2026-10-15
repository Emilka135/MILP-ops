"""The actual TTS boundary expands clinical text, not just a helper unused by speech."""
import numpy as np
import pytest
from calls.tts.normalize import normalize_text
from calls.tts.silero import build_ssml
from calls.tts.processing import process_audio

@pytest.mark.parametrize(('source','expected'),[
 ('Мы отправим СМС и SMS.','Мы отправим эс эм эс и эс эм эс.'),
 ('16.10.2026 в 14:30','шестнадцатого октября две тысячи двадцать шестого года в четырнадцать тридцать'),
 ('2026-11-21 в 09:05','двадцать первого ноября две тысячи двадцать шестого года в девять ноль пять'),
 ('Кабинет №201, нажмите 1 или 0.','Кабинет номер двести один, нажмите один или ноль.'),
 ('Карта 0012345','Карта ноль ноль один два три четыре пять'),
 ('+7 (903) 555-12-34','семь, девятьсот три, пятьсот пятьдесят пять, двенадцать, тридцать четыре'),
 ('Полис ОМС, УЗИ и ЭКГ','Полис обязательное медицинское страхование, ультразвуковое исследование и электрокардиограмма'),
])
def test_acoustic_model_never_gets_raw_abbreviations_or_digits(source,expected):
    assert normalize_text(source)==expected
    ssml,_=build_ssml(source,1.5)
    assert expected in ssml
    assert normalize_text(expected)==expected

@pytest.mark.parametrize('text',['Запись {slot_date}','31.02.2026','2026-19-20','25:99','Dr Smith'])
def test_ambiguous_or_invalid_text_is_not_silently_spoken(text):
    with pytest.raises(ValueError):normalize_text(text)


def test_tempo_preserves_pitch_and_headroom_at_native_rate():
    rate=48000;t=np.arange(rate*2)/rate;x=(.5*np.sin(2*np.pi*440*t)).astype('float32')
    y=process_audio(x,1.5,8000)
    assert abs(len(y)/8000-2/1.5)<.07
    spectrum=np.abs(np.fft.rfft(y*np.hanning(len(y))))
    peak=np.fft.rfftfreq(len(y),1/8000)[spectrum.argmax()]
    assert abs(peak-440)<3
    assert .3<float(np.sqrt(np.mean(y*y)))<.4
    assert float(np.abs(y).max())<.9
