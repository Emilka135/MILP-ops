"""The organizer workbook IDs must never be confused with API doctor IDs."""
from datetime import date, datetime, time
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("schedule_import", Path(__file__).resolve().parents[2] / "tools/import_dbi_schedule.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Sheet:
    def __init__(self, rows):
        self.rows = rows

    def iter_rows(self, **kwargs):
        return iter(self.rows)


DOCTOR = {"id": 73, "full_name": "Ковалёва Людмила Петровна", "specialty": "Терапевт", "is_active": True}


def slot(status="Свободен", day=date(2026, 10, 22)):
    return ("S001", datetime.combine(day, time()), "Чт", "D002", DOCTOR["full_name"], "Терапевт", "Отделение", "102", time(9), time(9, 20), 20, status)


def workbook(slots, unavailable=()):
    return {"Врачи": Sheet([("D002", "Ковалева Людмила Петровна")]), "Недоступность": Sheet(unavailable), "Слоты": Sheet(slots)}


def test_maps_by_name_instead_of_workbook_number():
    rows, stats, mapping = module.convert(workbook([slot()]), [DOCTOR], date(2026, 10, 21))
    assert mapping == {"D002": 73}
    assert rows[0]["doctor_id"] == 73
    assert rows[0]["specialty"] == "терапевт"
    assert stats["imported_slots"] == 1


def test_excludes_occupied_cancelled_and_past_rows():
    rows, stats, _ = module.convert(workbook([slot(), slot("Записан"), slot("Отменён пациентом"), slot(day=date(2026, 10, 20))]), [DOCTOR], date(2026, 10, 21))
    assert len(rows) == 1
    assert stats["not_free"] == 2
    assert stats["past"] == 1


@pytest.mark.parametrize("doctors", [[{**DOCTOR, "is_active": False}], [DOCTOR]])
def test_rejects_no_available_slots(doctors):
    unavailable = [("D002", "", "", "Отпуск", date(2026, 10, 22), date(2026, 10, 23))]
    with pytest.raises(ValueError, match="No future free slots"):
        module.convert(workbook([slot()], unavailable), doctors, date(2026, 10, 21))


def test_rejects_duplicate_api_names_and_missing_match():
    with pytest.raises(ValueError, match="Ambiguous API"):
        module.doctor_mapping([("D002", DOCTOR["full_name"])], [DOCTOR, {**DOCTOR, "id": 74}])
    with pytest.raises(ValueError, match="missing from API"):
        module.doctor_mapping([("D002", "Другой Врач")], [DOCTOR])


def test_rejects_duplicate_slot_and_inconsistent_name():
    with pytest.raises(ValueError, match="Duplicate slot"):
        module.convert(workbook([slot(), slot()]), [DOCTOR], date(2026, 10, 21))
    wrong = list(slot())
    wrong[4] = "Другой Врач"
    with pytest.raises(ValueError, match="differs"):
        module.convert(workbook([wrong]), [DOCTOR], date(2026, 10, 21))
