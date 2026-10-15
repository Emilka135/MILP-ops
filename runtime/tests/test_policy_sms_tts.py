from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from calls.crm.registry import MockRegistry, seed_demo
from calls.notify.sms import MockSmsGateway, SmsError, SmsModule, render
from calls.outbound.policy import CampaignItem, DialPolicy, decide, record_attempt
from calls.outbound.reminder import Campaign, build_campaign
from calls.tts.silero import SAMPLE_RATE, StubTTS, TtsModule, wsola_stretch
from calls.tts.verbalize import date_to_words, digits_to_words, number_to_words, phone_to_words, time_to_words
from tests.conftest import TODAY

# ---- outbound policy --------------------------------------------------------------------------


def item(appt=datetime(2026, 10, 15, 9, 0)):
    return CampaignItem(1, "79035551234", appt, {})


def test_window_10_22_is_enforced():
    it = item()
    assert not decide(it, datetime(2026, 10, 14, 9, 59)).allowed
    assert decide(it, datetime(2026, 10, 14, 9, 59)).not_before == datetime(2026, 10, 14, 10, 0)
    assert decide(it, datetime(2026, 10, 14, 10, 0)).allowed
    assert decide(it, datetime(2026, 10, 14, 21, 59)).allowed
    d = decide(it, datetime(2026, 10, 14, 22, 0))
    assert not d.allowed and d.not_before == datetime(2026, 10, 15, 10, 0)


def test_six_calls_per_day_as_three_occasions_of_two():
    it = item(datetime(2026, 10, 16, 9, 0))
    pol = DialPolicy()
    now = datetime(2026, 10, 14, 10, 0)
    dials = []
    for _ in range(40):
        d = decide(it, now, pol)
        if d.allowed:
            record_attempt(it, now, "no_answer", pol)
            dials.append(now)
            now += timedelta(minutes=1)
        else:
            assert d.not_before is not None and d.not_before > now
            now = d.not_before
        if it.closed:
            break
    first_day = [t for t in dials if t.date() == date(2026, 10, 14)]
    assert len(first_day) == 6 and it.closed and it.close_reason == "budget exhausted"
    occ = it.occasions()
    assert [len(o) for o in occ] == [2, 2, 2]
    assert all(o[1].at - o[0].at >= pol.retry_gap for o in occ)
    assert all(occ[i + 1][0].at - occ[i][-1].at >= pol.min_gap_between_occasions for i in range(2))
    assert all(time(10, 0) <= t.time() < time(22, 0) for t in dials)


def test_answered_closes_item_and_past_appointment_blocks():
    it = item()
    record_attempt(it, datetime(2026, 10, 14, 11, 0), "answered")
    assert it.closed and not decide(it, datetime(2026, 10, 14, 12, 0)).allowed
    assert not decide(item(datetime(2026, 10, 14, 8, 0)), datetime(2026, 10, 14, 12, 0)).allowed


def test_campaign_from_registry_and_tick():
    reg = MockRegistry(now=datetime(2026, 10, 14, 9, 0))
    seed_demo(reg, TODAY)
    s = reg.find_slots("терапевт", date(2026, 10, 15))
    reg.book(1, s[0].id)
    reg.book(3, s[1].id)
    confirmed = reg.book(2, s[2].id)
    reg.confirm(confirmed.id)
    items = build_campaign(reg, date(2026, 10, 15), {"clinic_name": "поликлиники", "clinic_phone": "+7"}, today=TODAY)
    assert sorted(i.vars["patient_name"] for i in items) == ["Иван Иванович", "Мария Сергеевна"]  # confirmed one skipped
    assert all("snils" not in i.vars and "birth_date" not in i.vars for i in items)
    camp = Campaign(items)
    outcomes = iter(["answered", "no_answer"])
    n = camp.run_tick(datetime(2026, 10, 14, 9, 0), lambda it: next(outcomes))
    assert n == 0  # before 10:00 nothing is dialed
    n = camp.run_tick(datetime(2026, 10, 14, 10, 0), lambda it: next(outcomes))
    assert n == 2 and items[0].closed and not items[1].closed
    assert camp.next_wakeup(datetime(2026, 10, 14, 10, 1)) == datetime(2026, 10, 14, 10, 20)


# ---- SMS ----------------------------------------------------------------------------------------

V = {"patient_name": "Анна Ивановна", "specialty_dative": "педиатру", "slot_date": "2026-10-15", "slot_time": "14:30", "cabinet": "201", "doctor": "Сидорова А. В."}


def test_sms_contains_name_and_details():
    txt = render("booked", V, {"clinic_name": "Поликлиника №7", "clinic_phone": "+7 495 000-00-00"})
    assert txt.startswith("Анна Ивановна, вы записаны к педиатру: 15 октября в 14:30, каб. 201")
    assert "коды из СМС" in txt and len(txt) < 300


def test_sms_without_name_is_refused():
    with pytest.raises(SmsError):
        render("booked", {**V, "patient_name": ""})
    with pytest.raises(SmsError):
        render("nonexistent", V)


def test_sms_module_resolves_phone_from_patient_id_not_from_vars():
    gw = MockSmsGateway()
    mod = SmsModule(gw, lambda v: {1: "79035551234"}.get(v.get("patient_id")), {"clinic_name": "П", "clinic_phone": "+7"})
    assert mod.handle("send", {"template": "cancelled", "vars": {**V, "patient_id": 1}})[0] == "ok"
    assert gw.sent[0][0] == "79035551234" and gw.sent[0][1].startswith("Анна Ивановна")
    status, payload = mod.handle("send", {"template": "cancelled", "vars": {**V, "patient_id": 99}})
    assert status == "error" and "no phone" in payload["error"]


# ---- TTS / verbalisation ---------------------------------------------------------------------------


@pytest.mark.parametrize("n,words", [(0, "ноль"), (14, "четырнадцать"), (21, "двадцать один"), (101, "сто один"), (2026, "две тысячи двадцать шесть"), (1000, "одна тысяча")])
def test_number_to_words(n, words):
    assert number_to_words(n) == words


def test_date_time_digits_to_words():
    assert date_to_words(date(2026, 10, 14)) == "четырнадцатого октября, в среду"
    assert date_to_words(date(2026, 10, 21), with_weekday=False) == "двадцать первого октября"
    assert time_to_words(time(14, 30)) == "четырнадцать тридцать"
    assert time_to_words(time(9, 0)) == "девять часов"
    assert time_to_words(time(13, 5)) == "тринадцать ноль пять"
    assert digits_to_words("1002") == "один ноль ноль два"
    assert phone_to_words("79035551234") == "семь, девять ноль три, пять пять пять, один два, три четыре"


@pytest.mark.parametrize("factor", [1.3, 1.5, 1.75])
def test_wsola_keeps_pitch_and_scales_duration(factor):
    sr = SAMPLE_RATE
    t = np.arange(sr * 2) / sr
    x = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    y = wsola_stretch(x, factor)
    assert abs(len(y) / len(x) - 1 / factor) < 0.01
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    peak = np.fft.rfftfreq(len(y), 1 / sr)[np.argmax(spec)]
    assert abs(peak - 440) < 3  # pitch preserved (plain resampling would give 440*factor)
    assert abs(float(np.sqrt((y ** 2).mean())) - float(np.sqrt((x ** 2).mean()))) < 0.02


def test_tts_module_cache_and_rate_bounds():
    mod = TtsModule(StubTTS(), cache_size=4)
    s1, p1 = mod.handle("synth", {"text": "Здравствуйте", "rate": 1.5})
    s2, p2 = mod.handle("synth", {"text": "Здравствуйте", "rate": 1.5})
    assert s1 == s2 == "ok" and p1["duration_ms"] == p2["duration_ms"] and mod.hits == 1 and mod.misses == 1
    assert p1["sample_rate"] == 8000 and p1["bytes"] == len(p1["pcm"]) and abs(p1["bytes"] - p1["duration_ms"] * 16) <= 32
    slow = mod.handle("synth", {"text": "Здравствуйте", "rate": 1.0})[1]["duration_ms"]
    assert abs(slow / p1["duration_ms"] - 1.5) < 0.05  # 1.5× faster speech is 1.5× shorter
    assert mod.handle("synth", {"text": "x", "rate": 3.0})[0] == "error"
