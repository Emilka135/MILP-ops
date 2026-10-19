"""Synthetic identities only; surname suggestions never authorize a patient ID."""

import sqlite3
from datetime import date

import pytest
from calls.crm.registry import MockRegistry
from clinic.identity import (ALIAS_SCHEMA, IdentityLookup, checked_pronunciation,
                             normalize_birth_date, normalize_identifier, normalize_surname)

TODAY = date(2026, 10, 19)


@pytest.fixture
def registry():
    reg = MockRegistry()
    reg._conn.executescript(ALIAS_SCHEMA)
    for surname, first, birthday, card, snils, phone in [
        ("Шварценеггер", "Арнольд", date(1990, 5, 5), "01003", "11223344595", "79160000001"),
        ("Хачатурян", "Арам", date(1990, 5, 5), "01004", "22334455606", "79160000002"),
        ("O’Connor", "Шон", date(2001, 12, 21), "01005", "33445566717", "79160000003"),
        ("Петрова-Смирнова", "Анна", date(1995, 1, 1), "01006", "44556677828", "79160000004"),
        ("Иванов", "Петр", date(1980, 1, 1), "01007", "55667788939", "79160000005"),
        ("Иванов", "Сергей", date(1980, 1, 1), "01008", "66778899040", "79160000005"),
    ]:
        reg.add_patient(surname, first, "Тестовый", birthday, snils, card, phone)
    reg._x("INSERT INTO identity_name_aliases VALUES(3,?,1,'reviewed synthetic corpus')", ("О’Коннор",))
    reg._x("INSERT INTO identity_name_aliases VALUES(3,?,1,'reviewed synthetic corpus')", ("О Коннор",))
    return reg


@pytest.mark.parametrize("spoken,expected", [
    ("  ШВАРЦЕНЕГГЕР. ", "шварценеггер"),
    ("Моя фамилия Хачатурян.", "хачатурян"),
    ("O’Connor", "oconnor"), ("O'Connor", "oconnor"),
    ("ПЕТРОВА–СМИРНОВА", "петрова смирнова"),
    ("Фамилия: Сёмин.", "семин"),
])
def test_names_normalize_punctuation_but_preserve_letters(spoken, expected):
    assert normalize_surname(spoken) == expected


@pytest.mark.parametrize("invalid", [None, [], "", "Хачатурян123", "x'; DROP TABLE patients;--", "С+емин"])
def test_invalid_surnames_are_not_sql_or_pronunciation_markup(invalid):
    with pytest.raises(ValueError): normalize_surname(invalid)


@pytest.mark.parametrize("spoken,field,expected", [
    ("Ноль один ноль ноль три.", "card_no", "01003"),
    ("01003", "card_no", "01003"), ("01 003", "card_no", "01003"),
    ("112-233-445 95", "snils", "11223344595"),
    ("+7 (916) 000-00-01", "phone", "79160000001"),
    ("8 916 000 00 01", "phone", "79160000001"),
])
def test_complete_identifiers_preserve_leading_zeros(spoken, field, expected):
    assert normalize_identifier(spoken, field) == expected


@pytest.mark.parametrize("spoken,field", [
    ("Сто три", "card_no"), ("01", "card_no"), ("01003000000", "card_no"),
    ("112233", "snils"), ("Один ноль три и два", "card_no"),
    ("1.003", "card_no"), (True, "card_no"), ("+19160000001", "phone"),
])
def test_ambiguous_partial_or_invalid_identifiers_require_repetition(spoken, field):
    with pytest.raises(ValueError): normalize_identifier(spoken, field)


@pytest.mark.parametrize("spoken,expected", [
    ("05.05.1990", date(1990, 5, 5)), ("1990-05-05", date(1990, 5, 5)),
    ("05051990", date(1990, 5, 5)),
    ("ноль пять ноль пять один девять девять ноль", date(1990, 5, 5)),
    ("пятого мая тысяча девятьсот девяностого года", date(1990, 5, 5)),
    ("двадцать первого декабря две тысячи первого года", date(2001, 12, 21)),
])
def test_birth_date_requires_and_preserves_full_historical_year(spoken, expected):
    assert normalize_birth_date(spoken, today=TODAY) == expected


@pytest.mark.parametrize("spoken", [
    "завтра", "05.05", "05.05.90", "пятого мая", "пятого мая девяностого года",
    "31.02.1990", "2027-01-01", "пятого мая тысяча тысяча года",
    "пятого мая 1990 или 1991 года", "пятоо мая тысяча девятьсот девяностого года",
])
def test_partial_relative_ambiguous_and_invalid_birth_dates_never_guess(spoken):
    with pytest.raises(ValueError): normalize_birth_date(spoken, today=TODAY)


@pytest.mark.parametrize("surname", ["Шварценеггер", "Хачатурян", "O'Connor", "О’Коннор", "Петрова Смирнова"])
def test_known_surname_alone_only_requests_full_identifier(registry, surname):
    status, result = IdentityLookup(registry).lookup({"surname": surname}, today=TODAY)
    assert status == "clarify" and result["candidate_count"] == 1
    assert "patient_id" not in result and "patient_name" not in result


def test_duplicate_surname_and_birth_date_remain_ambiguous_without_disclosing_ids(registry):
    status, result = IdentityLookup(registry).lookup({"surname": "Иванов", "birth_date": "01.01.1980"}, today=TODAY)
    assert status == "clarify" and result["reason"] == "ambiguous_identity"
    assert result["candidate_count"] == 2 and "patient_id" not in result


def test_typo_or_unapproved_transliteration_never_autoselects(registry):
    lookup = IdentityLookup(registry)
    for surname in ("Хачатурин", "Шварценегер", "Khachaturyan"):
        status, result = lookup.lookup({"surname": surname}, today=TODAY)
        assert status == "clarify" and result["candidate_count"] == 0
        assert "patient_id" not in result
    registry._x("INSERT INTO identity_name_aliases VALUES(2,'Khachaturyan',0,'unapproved fixture')")
    assert lookup.lookup({"surname": "Khachaturyan"})[1]["candidate_count"] == 0
    registry._x("UPDATE identity_name_aliases SET approved=1 WHERE alias='Khachaturyan'")
    assert lookup.lookup({"surname": "Khachaturyan"})[1]["candidate_count"] == 1


def test_all_supplied_identifiers_must_belong_to_the_same_patient(registry):
    lookup = IdentityLookup(registry)
    status, result = lookup.lookup({"card_no": "01003", "snils": "22334455606"}, today=TODAY)
    assert status == "clarify" and result["candidate_count"] == 0 and "patient_id" not in result
    status, result = lookup.lookup({"card_no": "01003", "surname": "Хачатурян"})
    assert status == "clarify" and result["candidate_count"] == 0


def test_exact_full_identifier_is_required_and_locked_id_cannot_switch(registry):
    lookup = IdentityLookup(registry)
    assert lookup.lookup({"phone": "+79160000001"})[0] == "clarify"
    status, result = lookup.lookup({"card_no": "Ноль один ноль ноль три.", "birth_date": "05.05.1990"}, today=TODAY)
    assert status == "ok" and result["patient_id"] == 1
    assert lookup.lookup({"card_no": "01003"}, locked_patient_id=2)[0] == "mismatch"
    assert lookup.lookup({"card_no": "01003"}, locked_patient_id=1)[1]["patient_id"] == 1
    assert lookup.lookup({"card_no": "01003", "patient_id": 2})[0] == "invalid"


def test_module_only_executes_reads_and_never_mutates_appointments(registry):
    writes = []
    def authorizer(operation, *args):
        if operation in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                         sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE}:
            writes.append(operation)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    registry._conn.set_authorizer(authorizer)
    lookup = IdentityLookup(registry)
    assert lookup.lookup({"card_no": "01005", "surname": "О'Коннор"})[1]["patient_id"] == 3
    assert lookup.lookup({"surname": "Хачатурян"})[0] == "clarify"
    assert writes == [] and registry.execute("SELECT count(*) AS n FROM bookings")[0]["n"] == 0


def test_alias_schema_is_optional_and_never_created_by_lookup():
    reg = MockRegistry()
    reg.add_patient("O'Connor", "Шон", card_no="01005")
    assert IdentityLookup(reg).lookup({"card_no": "01005"})[1]["patient_id"] == 1
    assert reg.execute("SELECT name FROM sqlite_master WHERE name='identity_name_aliases'") == []


def test_candidate_cap_never_turns_many_into_unique(registry):
    for index in range(30): registry.add_patient("Тестов", "Тест", card_no=str(20000 + index))
    status, result = IdentityLookup(registry).lookup({"surname": "Тестов"})
    assert status == "clarify" and result["candidate_count"] == 20 and result["more_candidates"] is True


@pytest.mark.parametrize("surname,pronunciation", [
    ("Шварценеггер", "Шварцен+еггер"), ("Хачатурян", "Хачатур+ян"),
    ("O'Connor", "О К+оннор"), ("Петрова-Смирнова", "Петр+ова-Смирн+ова"),
])
def test_foreign_name_pronunciation_is_explicit_and_stress_checked(surname, pronunciation):
    assert checked_pronunciation(surname, pronunciation) == pronunciation


@pytest.mark.parametrize("pronunciation", [None, "Шварценеггер", "Schwarzenegger", "Шварцен+еггер 123"])
def test_missing_or_unverified_pronunciation_is_not_guessed(pronunciation):
    with pytest.raises(ValueError): checked_pronunciation("Шварценеггер", pronunciation)


def test_generic_registry_combines_all_identifiers_instead_of_any_match(registry):
    assert [p.id for p in registry.find_patient(card_no="01003", phone="79160000001")] == [1]
    assert registry.find_patient(card_no="01003", phone="79160000002") == []
    assert registry.find_patient(card_no="01003", phone="79160000999") == []
    assert registry.find_patient(card_no="01003", snils="22334455606") == []


def test_birth_speech_uses_full_historical_year_and_does_not_become_slot_preference():
    from clinic.modules import Nlu
    nlu = Nlu()
    status, data = nlu.handle("interpret", {"text":"пятого мая тысяча девятьсот девяностого года", "today":"2026-10-19", "identity_field":"birth_date"})
    assert status == "ok" and data["digits"] == "05051990"
    assert not any(key in data["vars"] for key in ("said_date", "said_time", "said_daypart"))
    assert nlu.handle("interpret", {"text":"пятого мая", "today":"2026-10-19", "identity_field":"birth_date"})[1]["digits"] == ""
    assert nlu.handle("interpret", {"text":"ноль один ноль ноль три", "identity_field":"card_no"})[1]["digits"] == "01003"
    assert nlu.handle("interpret", {"text":"срочно позовите оператора", "identity_field":"birth_date"})[1]["intent"] in {"operator", "emergency"}


def test_real_asr_punctuation_and_abbreviated_year_are_normalized_without_guessing():
    assert normalize_surname("Петрова, Смирнова.") == normalize_surname("Петрова-Смирнова")
    assert normalize_birth_date("5 мая 1990 г.", today=TODAY) == date(1990,5,5)
    assert normalize_identifier("0. 1. 0. 0. 3.") == "01003"
    assert normalize_identifier("Ноль. Один. Ноль. Ноль. Три.") == "01003"


def test_crm_identity_route_preserves_internal_metadata_and_locked_identity(registry):
    from clinic.crm import ClinicCrm
    crm = ClinicCrm(registry)
    status, patient = crm.handle("identity.lookup", {
        "card_no": "Ноль один ноль ноль три.", "birth_date": "5 мая 1990 года",
        "call_id": "synthetic-identity", "request_id": "request-one", "operation_key": "read-only",
        "locked_patient_id": 1,
    })
    assert status == "ok" and patient["patient_id"] == 1
    assert crm.handle("identity.lookup", {"card_no": "01003", "locked_patient_id": 2})[0] == "mismatch"
    assert registry.execute("SELECT count(*) AS n FROM bookings")[0]["n"] == 0


@pytest.mark.parametrize("extra", [{"sql": "SELECT * FROM patients"}, {"patient_id": 2}, {"role": "admin"}])
def test_crm_identity_route_rejects_client_supplied_query_and_identity_switch(registry, extra):
    from clinic.crm import ClinicCrm
    status, result = ClinicCrm(registry).handle("identity.lookup", {"card_no": "01003", **extra})
    assert status == "error" and "patient_id" not in result
