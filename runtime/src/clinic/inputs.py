"""Canonical CSV/XLSX imports with explicit stand doctor ids, never row-number ids."""

import csv
import json
from datetime import date, datetime, time
from pathlib import Path


def rows(path):
    p = Path(path)
    if p.suffix.lower() == ".json":
        return json.loads(p.read_text())
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        try:
            it = wb.active.iter_rows(values_only=True)
            header = [str(x).strip() for x in next(it)]
            return [
                dict(zip(header, row)) for row in it if any(x is not None for x in row)
            ]
        finally:
            wb.close()
    with p.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load(reg, env):
    result = {}
    if env.get("CRM_SCHEDULE"):
        count = 0
        for r in rows(env["CRM_SCHEDULE"]):
            did = int(r["doctor_id"])
            full = str(r["full_name"])
            active = str(r.get("is_active", "1")).lower() in ("1", "true", "да")
            reg._x(
                "INSERT INTO doctors(id,last_name,full_name,specialty,cabinet,active) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name,specialty=excluded.specialty,cabinet=excluded.cabinet,active=excluded.active",
                (
                    did,
                    full.split()[0],
                    full,
                    str(r["specialty"]),
                    str(r.get("cabinet", "")),
                    int(active),
                ),
            )
            step = int(r.get("slot_minutes", 15))
            if step <= 0 or step > 240:
                raise ValueError("slot_minutes must be in 1..240")
            d = (
                r["date"].date()
                if isinstance(r["date"], datetime)
                else r["date"]
                if isinstance(r["date"], date)
                else date.fromisoformat(r["date"])
            )
            start = (
                r["start"]
                if isinstance(r["start"], time)
                else time.fromisoformat(r["start"])
            )
            end = (
                r["end"] if isinstance(r["end"], time) else time.fromisoformat(r["end"])
            )
            count += reg.add_slots(did, d, start, end, step)
        result["slots"] = count
    if env.get("CRM_PATIENTS"):
        patients = rows(env["CRM_PATIENTS"])
        result["patients"] = reg.load_patients_rows(patients)
        for r in patients:
            if r.get("stand_client_id"):
                if not r.get("id"):
                    raise ValueError(
                        "stand_client_id requires explicit local patient id"
                    )
                reg._x(
                    "INSERT INTO remote_clients(patient_id,remote_id) VALUES(?,?) ON CONFLICT(patient_id) DO UPDATE SET remote_id=excluded.remote_id",
                    (int(r["id"]), int(r["stand_client_id"])),
                )
    if env.get("CRM_BOOKINGS"):
        result["bookings"] = reg.load_bookings_rows(rows(env["CRM_BOOKINGS"]))
    if env.get("CRM_SERVICE_HOURS"):
        for r in rows(env["CRM_SERVICE_HOURS"]):
            reg._x(
                "INSERT OR REPLACE INTO service_hours VALUES(?,?,?,?,?,?)",
                (
                    r["service"],
                    int(r["weekday"]),
                    r.get("opens") or None,
                    r.get("closes") or None,
                    int(str(r.get("closed", "0")).lower() in {"1", "true"}),
                    "clinic_import",
                ),
            )
    if env.get("CRM_SERVICE_CALENDAR"):
        for r in rows(env["CRM_SERVICE_CALENDAR"]):
            reg._x(
                "INSERT OR REPLACE INTO service_calendar VALUES(?,?,?,?,?,?)",
                (
                    r["service"],
                    r["day"],
                    r.get("opens") or None,
                    r.get("closes") or None,
                    int(str(r.get("closed", "0")).lower() in {"1", "true"}),
                    "clinic_order",
                ),
            )
    if env.get("CRM_ASSIGNMENTS"):
        for r in rows(env["CRM_ASSIGNMENTS"]):
            reg._x(
                "INSERT OR REPLACE INTO patient_assignments VALUES(?,?,?)",
                (int(r["patient_id"]), r["specialty"], int(r["doctor_id"])),
            )
    return result
