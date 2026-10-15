"""Local CRM mirror (SQLite) + registry operations used by the scenario.

The demo stand gives us the doctors' schedule as an Excel file and a REST API
limited to **10 requests per minute**.  Reads therefore never hit the API: the
schedule is imported into this local mirror, slot search / day search / day
parts are plain SQL, and only *writes* (book, cancel, reschedule, operator
transfer) are forwarded through :class:`calls.crm.remote.HttpRegistry`.

Schema (also the target of the text→SQL automaton)::

    doctors(id, last_name, full_name, specialty, cabinet, needs_referral)
    patients(id, last_name, first_name, middle_name, birth_date, snils, card_no, phone)
    slots(id, doctor_id, starts_at ISO-8601, status free|booked|blocked, booking_id)
    bookings(id, patient_id, slot_id, doctor_id, starts_at, status booked|cancelled|rescheduled,
             confirmed, remote_id, sync_state, created_at, updated_at)
    clinic_hours(weekday, opens, closes) / holidays(day, note)
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional

DAYPART_WINDOWS: dict[str, tuple[str, str]] = {
    "morning": ("08:00", "12:00"),
    "midday": ("12:00", "15:00"),
    "afternoon": ("13:00", "17:00"),
    "evening": ("17:00", "20:00"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS doctors (
    id INTEGER PRIMARY KEY, last_name TEXT NOT NULL, full_name TEXT NOT NULL,
    specialty TEXT NOT NULL, cabinet TEXT DEFAULT '', needs_referral INTEGER DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY, last_name TEXT, first_name TEXT, middle_name TEXT,
    birth_date TEXT, snils TEXT UNIQUE, card_no TEXT UNIQUE, phone TEXT);
CREATE INDEX IF NOT EXISTS ix_patients_phone ON patients(phone);
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY, doctor_id INTEGER NOT NULL REFERENCES doctors(id),
    starts_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'free', booking_id INTEGER,
    UNIQUE(doctor_id, starts_at));
CREATE INDEX IF NOT EXISTS ix_slots_lookup ON slots(status, starts_at);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
    slot_id INTEGER NOT NULL REFERENCES slots(id), doctor_id INTEGER NOT NULL,
    starts_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'booked', confirmed INTEGER DEFAULT 0,
    remote_id TEXT, sync_state TEXT DEFAULT 'pending', created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_bookings_patient ON bookings(patient_id, status, starts_at);
CREATE TABLE IF NOT EXISTS clinic_hours (weekday INTEGER PRIMARY KEY, opens TEXT, closes TEXT);
CREATE TABLE IF NOT EXISTS holidays (day TEXT PRIMARY KEY, note TEXT);
CREATE TABLE IF NOT EXISTS operator_requests (
    id INTEGER PRIMARY KEY, call_id TEXT, reason TEXT, payload TEXT, created_at TEXT, remote_id TEXT, sync_state TEXT DEFAULT 'pending');
"""


class RegistryError(Exception):
    pass


class SlotTaken(RegistryError):
    pass


class NotFound(RegistryError):
    pass


@dataclass(frozen=True)
class Slot:
    id: int
    doctor_id: int
    doctor: str
    specialty: str
    starts_at: datetime
    cabinet: str = ""

    @property
    def day(self) -> date:
        return self.starts_at.date()

    @property
    def clock(self) -> time:
        return self.starts_at.time()


@dataclass(frozen=True)
class Patient:
    id: int
    last_name: str
    first_name: str
    middle_name: str
    birth_date: Optional[date]
    snils: Optional[str]
    card_no: Optional[str]
    phone: Optional[str]

    @property
    def short_name(self) -> str:
        """Name + patronymic – what the SMS and the voice use (no surname aloud)."""
        return " ".join(p for p in (self.first_name, self.middle_name) if p)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.last_name, self.first_name, self.middle_name) if p)


@dataclass(frozen=True)
class Booking:
    id: int
    patient_id: int
    slot_id: int
    doctor_id: int
    doctor: str
    specialty: str
    starts_at: datetime
    status: str
    confirmed: bool
    cabinet: str = ""


def _row_slot(r: sqlite3.Row) -> Slot:
    return Slot(r["id"], r["doctor_id"], r["full_name"], r["specialty"], datetime.fromisoformat(r["starts_at"]), r["cabinet"] or "")


class MockRegistry:
    """SQLite-backed registry; thread-safe through a single lock (SQLite serialises anyway)."""

    def __init__(self, path: str = ":memory:", now: Optional[datetime] = None) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        # in-memory mirror (tests): no journal; file mirror shared by several module processes (compose volume): WAL
        self._conn.execute("PRAGMA journal_mode=MEMORY" if path == ":memory:" else "PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(SCHEMA)
        if "active" not in {r[1] for r in self._conn.execute("PRAGMA table_info(doctors)")}:
            self._conn.execute("ALTER TABLE doctors ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        self._lock = threading.RLock()
        self._now = now  # frozen clock for tests
        self.sql_log: list[tuple[str, tuple]] = []
        self._log_sql = path == ":memory:"  # the SQL log is a test aid; do not grow it forever in a service

    # ---- infrastructure ---------------------------------------------------------------

    def now(self) -> datetime:
        return self._now or datetime.now()

    def _log(self, sql: str, params: tuple) -> None:
        if self._log_sql:
            self.sql_log.append((sql, params))
            if len(self.sql_log) > 10000:
                del self.sql_log[:5000]

    def _q(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        params = tuple(params)
        with self._lock:
            self._log(sql, params)
            return self._conn.execute(sql, params).fetchall()

    def _x(self, sql: str, params: Iterable = ()) -> int:
        params = tuple(params)
        with self._lock:
            self._log(sql, params)
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    def execute(self, sql: str, params: Optional[dict] = None) -> list[dict]:
        """Run a parameterised statement from the text→SQL automaton (named params)."""
        with self._lock:
            self._log(sql, tuple(sorted((params or {}).items())))
            cur = self._conn.execute(sql, params or {})
            if cur.description is None:
                return [{"rowcount": cur.rowcount}]
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ---- loading ------------------------------------------------------------------------

    def add_doctor(self, last_name: str, full_name: str, specialty: str, cabinet: str = "", needs_referral: bool = False) -> int:
        return self._x(
            "INSERT INTO doctors(last_name, full_name, specialty, cabinet, needs_referral) VALUES (?,?,?,?,?)",
            (last_name, full_name, specialty, cabinet, int(needs_referral)),
        )

    def add_slots(self, doctor_id: int, day: date, start: time, end: time, minutes: int) -> int:
        t = datetime.combine(day, start)
        stop = datetime.combine(day, end)
        n = 0
        with self._lock:
            while t < stop:
                self._x("INSERT OR IGNORE INTO slots(doctor_id, starts_at, status) VALUES (?,?,'free')", (doctor_id, t.isoformat(timespec="minutes")))
                t += timedelta(minutes=minutes)
                n += 1
        return n

    def add_patient(self, last_name: str, first_name: str, middle_name: str = "", birth_date: Optional[date] = None,
                    snils: Optional[str] = None, card_no: Optional[str] = None, phone: Optional[str] = None) -> int:
        return self._x(
            "INSERT INTO patients(last_name, first_name, middle_name, birth_date, snils, card_no, phone) VALUES (?,?,?,?,?,?,?)",
            (last_name, first_name, middle_name, birth_date.isoformat() if birth_date else None, snils, card_no, phone),
        )

    def load_schedule_csv(self, path: Path | str) -> int:
        """CSV columns: last_name,full_name,specialty,cabinet,date,start,end,slot_minutes[,needs_referral]."""
        n = 0
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                doc = self._q("SELECT id FROM doctors WHERE full_name = ?", (row["full_name"],))
                did = doc[0]["id"] if doc else self.add_doctor(
                    row["last_name"], row["full_name"], row["specialty"], row.get("cabinet", ""), row.get("needs_referral", "0") in ("1", "true", "да")
                )
                n += self.add_slots(did, date.fromisoformat(row["date"]), time.fromisoformat(row["start"]), time.fromisoformat(row["end"]), int(row.get("slot_minutes", 15)))
        return n

    def load_schedule_xlsx(self, path: Path | str, sheet: Optional[str] = None) -> int:  # pragma: no cover - needs openpyxl
        """Same columns as the CSV; header row = column names (adapt mapping to the organisers' file)."""
        import openpyxl  # type: ignore[import-not-found]

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.active
        rows = ws.iter_rows(values_only=True)
        header = [str(h).strip() for h in next(rows)]
        n = 0
        for values in rows:
            row = dict(zip(header, values))
            if not row.get("full_name"):
                continue
            doc = self._q("SELECT id FROM doctors WHERE full_name = ?", (row["full_name"],))
            did = doc[0]["id"] if doc else self.add_doctor(str(row["last_name"]), str(row["full_name"]), str(row["specialty"]), str(row.get("cabinet") or ""))
            d = row["date"] if isinstance(row["date"], date) else date.fromisoformat(str(row["date"]))
            st = row["start"] if isinstance(row["start"], time) else time.fromisoformat(str(row["start"]))
            en = row["end"] if isinstance(row["end"], time) else time.fromisoformat(str(row["end"]))
            n += self.add_slots(did, d, st, en, int(row.get("slot_minutes") or 15))
        return n

    # ---- input files (patients / existing bookings) ---------------------------------------------

    PATIENT_COLUMNS = ("last_name", "first_name", "middle_name", "birth_date", "snils", "card_no", "phone")
    BOOKING_COLUMNS = ("remote_id", "patient_id", "doctor", "starts_at", "confirmed")

    def load_patients_rows(self, rows: Iterable[dict]) -> int:
        """Import patients (``PATIENT_COLUMNS``; optional ``id``/``remote_id`` keeps the stand's patient id).

        Idempotent by SNILS / card number: an existing patient is updated, not duplicated.
        """
        n = 0
        with self._lock:
            for row in rows:
                if not (row.get("last_name") or row.get("snils") or row.get("card_no")):
                    continue
                snils = _digits(row.get("snils")) or None
                card = (str(row.get("card_no") or "").strip() or None)
                phone = normalize_phone(str(row.get("phone") or "")) or None
                bd = _as_date(row.get("birth_date"))
                found = None
                if snils:
                    found = self._q("SELECT id FROM patients WHERE snils = ?", (snils,))
                if not found and card:
                    found = self._q("SELECT id FROM patients WHERE card_no = ?", (card,))
                vals = (str(row.get("last_name") or "").strip(), str(row.get("first_name") or "").strip(), str(row.get("middle_name") or "").strip(),
                        bd.isoformat() if bd else None, snils, card, phone)
                if found:
                    self._x("UPDATE patients SET last_name=?, first_name=?, middle_name=?, birth_date=?, snils=?, card_no=?, phone=? WHERE id = ?",
                            vals + (found[0]["id"],))
                else:
                    pid = row.get("id") or row.get("remote_id") or row.get("patient_id")
                    if pid:
                        self._x("INSERT INTO patients(id, last_name, first_name, middle_name, birth_date, snils, card_no, phone) VALUES (?,?,?,?,?,?,?,?)",
                                (int(pid),) + vals)
                    else:
                        self._x("INSERT INTO patients(last_name, first_name, middle_name, birth_date, snils, card_no, phone) VALUES (?,?,?,?,?,?,?)", vals)
                n += 1
        return n

    def load_patients_csv(self, path: Path | str) -> int:
        with open(path, newline="", encoding="utf-8") as fh:
            return self.load_patients_rows(csv.DictReader(fh))

    def load_patients_xlsx(self, path: Path | str, sheet: Optional[str] = None) -> int:  # pragma: no cover - needs openpyxl
        return self.load_patients_rows(_xlsx_rows(path, sheet))

    def import_booking(self, remote_id: str, patient_id: int, doctor: str | int, starts_at: datetime, confirmed: bool = False) -> int:
        """Mirror a booking that already exists in the stand (made by a registrar).

        ``doctor`` is the stand's doctor id or the full name from the schedule; the slot is created on the fly
        when the schedule file did not contain it.  Idempotent by ``remote_id``.
        """
        with self._lock:
            have = self._q("SELECT id FROM bookings WHERE remote_id = ?", (str(remote_id),))
            if have:
                return have[0]["id"]
            if isinstance(doctor, int) or str(doctor).isdigit():
                did = int(doctor)
            else:
                doc = self._q("SELECT id FROM doctors WHERE full_name = ? OR last_name = ?", (str(doctor), str(doctor)))
                if not doc:
                    raise NotFound(f"doctor {doctor!r}")
                did = doc[0]["id"]
            ts_slot = starts_at.isoformat(timespec="minutes")
            self._x("INSERT OR IGNORE INTO slots(doctor_id, starts_at, status) VALUES (?,?,'free')", (did, ts_slot))
            slot_id = self._q("SELECT id FROM slots WHERE doctor_id = ? AND starts_at = ?", (did, ts_slot))[0]["id"]
            occupancy = self._q("SELECT status FROM slots WHERE id = ?", (slot_id,))[0]["status"]
            if occupancy not in ("free", "external"):
                raise SlotTaken(f"import conflicts with occupied slot {slot_id}")
            now = self.now().isoformat(timespec="seconds")
            bid = self._x(
                "INSERT INTO bookings(patient_id, slot_id, doctor_id, starts_at, status, confirmed, remote_id, sync_state, created_at, updated_at) "
                "VALUES (?,?,?,?,'booked',?,?,'synced',?,?)",
                (int(patient_id), slot_id, did, ts_slot, 1 if confirmed else 0, str(remote_id), now, now),
            )
            self._x("UPDATE slots SET status = 'booked', booking_id = ? WHERE id = ?", (bid, slot_id))
            self.sql_log.append(("IMPORT_BOOKING", (remote_id, patient_id, slot_id)))
            return bid

    def load_bookings_rows(self, rows: Iterable[dict]) -> int:
        """``BOOKING_COLUMNS``: remote_id,patient_id,doctor,starts_at(ISO),confirmed(0/1)."""
        n = 0
        for row in rows:
            if not row.get("remote_id") and not row.get("id"):
                continue
            rid = str(row.get("remote_id") or row.get("id"))
            when = row.get("starts_at")
            when = when if isinstance(when, datetime) else datetime.fromisoformat(str(when))
            conf = str(row.get("confirmed") or "0").strip().lower() in ("1", "true", "yes", "да")
            self.import_booking(rid, int(row["patient_id"]), row.get("doctor") or row.get("doctor_id") or "", when, conf)
            n += 1
        return n

    def load_bookings_csv(self, path: Path | str) -> int:
        with open(path, newline="", encoding="utf-8") as fh:
            return self.load_bookings_rows(csv.DictReader(fh))

    def load_bookings_json(self, path: Path | str) -> int:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data.get("items", data) if isinstance(data, dict) else data
        return self.load_bookings_rows(items)

    def load_bookings_xlsx(self, path: Path | str, sheet: Optional[str] = None) -> int:  # pragma: no cover - needs openpyxl
        return self.load_bookings_rows(_xlsx_rows(path, sheet))

    def export_patients(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM patients ORDER BY id")]

    def export_bookings(self) -> list[dict]:
        rows = self._q("SELECT b.id, b.remote_id, b.patient_id, d.full_name AS doctor, b.starts_at, b.confirmed, b.status, b.sync_state "
                       "FROM bookings b JOIN doctors d ON d.id = b.doctor_id ORDER BY b.id")
        return [dict(r) for r in rows]

    def set_clinic_hours(self, hours: dict[int, tuple[str, str]]) -> None:
        for wd, (o, c) in hours.items():
            self._x("INSERT OR REPLACE INTO clinic_hours(weekday, opens, closes) VALUES (?,?,?)", (wd, o, c))

    def add_holiday(self, day: date, note: str) -> None:
        self._x("INSERT OR REPLACE INTO holidays(day, note) VALUES (?,?)", (day.isoformat(), note))

    # ---- patients -----------------------------------------------------------------------

    def _patient(self, r: sqlite3.Row) -> Patient:
        return Patient(r["id"], r["last_name"], r["first_name"], r["middle_name"] or "",
                       date.fromisoformat(r["birth_date"]) if r["birth_date"] else None, r["snils"], r["card_no"], r["phone"])

    def patient_by_id(self, pid: int) -> Optional[Patient]:
        rows = self._q("SELECT * FROM patients WHERE id = ?", (pid,))
        return self._patient(rows[0]) if rows else None

    def find_patient(self, snils: Optional[str] = None, card_no: Optional[str] = None, phone: Optional[str] = None,
                     birth_date: Optional[date] = None) -> list[Patient]:
        """Any identifier; ``birth_date`` narrows the result (never identifies alone)."""
        where, params = [], []
        if snils:
            where.append("snils = ?")
            params.append(snils)
        if card_no:
            where.append("card_no = ?")
            params.append(card_no)
        if phone:
            where.append("phone = ?")
            params.append(normalize_phone(phone))
        if not where:
            return []
        rows = self._q("SELECT * FROM patients WHERE " + " OR ".join(where), params)
        pats = [self._patient(r) for r in rows]
        if birth_date is not None:
            pats = [p for p in pats if p.birth_date == birth_date]
        return pats

    def patients_by_phone(self, phone: str) -> list[Patient]:
        return self.find_patient(phone=phone)

    # ---- slots ----------------------------------------------------------------------------

    def find_slots(self, specialty: Optional[str] = None, day: Optional[date] = None, not_before: Optional[datetime] = None,
                   t_from: Optional[str] = None, t_to: Optional[str] = None, doctor_id: Optional[int] = None,
                   surname_prefix: Optional[str] = None, limit: int = 10) -> list[Slot]:
        where = ["s.status = 'free'", "d.active = 1", "s.starts_at >= ?"]
        params: list[object] = [(not_before or self.now()).isoformat(timespec="minutes")]
        if specialty:
            where.append("d.specialty = ?")
            params.append(specialty)
        if doctor_id:
            where.append("d.id = ?")
            params.append(doctor_id)
        if surname_prefix:
            where.append("lower(d.last_name) LIKE ?")
            params.append(surname_prefix.lower() + "%")
        if day:
            where.append("date(s.starts_at) = ?")
            params.append(day.isoformat())
        if t_from:
            where.append("time(s.starts_at) >= ?")
            params.append(t_from)
        if t_to:
            where.append("time(s.starts_at) < ?")
            params.append(t_to)
        rows = self._q(
            "SELECT s.id, s.doctor_id, d.full_name, d.specialty, d.cabinet, s.starts_at FROM slots s JOIN doctors d ON d.id = s.doctor_id "
            "WHERE " + " AND ".join(where) + " ORDER BY s.starts_at LIMIT ?", params + [limit],
        )
        return [_row_slot(r) for r in rows]

    def highlight_sql(self, specialty: Optional[str], day: Optional[date], daypart: Optional[str], t_from: Optional[str] = None) -> tuple[str, dict]:
        """The SQL the operator window uses to paint matching slots green."""
        where = ["s.status = 'free'"]
        params: dict[str, object] = {}
        if specialty:
            where.append("d.specialty = :specialty")
            params["specialty"] = specialty
        if day:
            where.append("date(s.starts_at) = :day")
            params["day"] = day.isoformat()
        if daypart:
            a, b = DAYPART_WINDOWS[daypart]
            where.append("time(s.starts_at) >= :t_from AND time(s.starts_at) < :t_to")
            params["t_from"], params["t_to"] = a, b
        elif t_from:
            where.append("time(s.starts_at) >= :t_from")
            params["t_from"] = t_from
        sql = "SELECT s.id FROM slots s JOIN doctors d ON d.id = s.doctor_id WHERE " + " AND ".join(where) + " ORDER BY s.starts_at"
        return sql, params

    def next_day(self, specialty: str, after: date, limit_days: int = 60, doctor_id: Optional[int] = None) -> Optional[date]:
        """First day strictly after ``after`` with a free slot for the specialty."""
        rows = self._q(
            "SELECT date(s.starts_at) AS d FROM slots s JOIN doctors d ON d.id = s.doctor_id WHERE s.status = 'free' "
            "AND d.specialty = ? AND date(s.starts_at) > ? AND date(s.starts_at) <= ? AND s.starts_at >= ?"
            + (" AND d.id = ?" if doctor_id else "") + " ORDER BY s.starts_at LIMIT 1",
            [specialty, after.isoformat(), (after + timedelta(days=limit_days)).isoformat(), self.now().isoformat(timespec="minutes")] + ([doctor_id] if doctor_id else []),
        )
        return date.fromisoformat(rows[0]["d"]) if rows else None

    def day_parts(self, specialty: str, day: date, doctor_id: Optional[int] = None) -> list[str]:
        """Which halves of the day still have free slots: subset of ["morning", "afternoon"]."""
        parts = []
        for part, (a, b) in (("morning", ("00:00", "13:00")), ("afternoon", ("13:00", "23:59"))):
            if self.find_slots(specialty, day, t_from=a, t_to=b, doctor_id=doctor_id, limit=1):
                parts.append(part)
        return parts

    def nearest_in_part(self, specialty: str, daypart: str, after: date, limit_days: int = 60) -> Optional[Slot]:
        a, b = DAYPART_WINDOWS[daypart]
        for d in range(0, limit_days + 1):
            day = after + timedelta(days=d)
            found = self.find_slots(specialty, day, t_from=a, t_to=b, limit=1)
            if found:
                return found[0]
        return None

    def slot(self, slot_id: int) -> Optional[Slot]:
        rows = self._q("SELECT s.id, s.doctor_id, d.full_name, d.specialty, d.cabinet, s.starts_at FROM slots s JOIN doctors d ON d.id = s.doctor_id WHERE s.id = ?", (slot_id,))
        return _row_slot(rows[0]) if rows else None

    # ---- bookings -------------------------------------------------------------------------

    def _booking(self, r: sqlite3.Row) -> Booking:
        return Booking(r["id"], r["patient_id"], r["slot_id"], r["doctor_id"], r["full_name"], r["specialty"],
                       datetime.fromisoformat(r["starts_at"]), r["status"], bool(r["confirmed"]), r["cabinet"] or "")

    def book(self, patient_id: int, slot_id: int) -> Booking:
        ts = self.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT status FROM slots WHERE id = ?", (slot_id,)).fetchone()
                if row is None:
                    raise NotFound(f"slot {slot_id}")
                if row["status"] != "free":
                    raise SlotTaken(f"slot {slot_id} is {row['status']}")
                s = self._conn.execute("SELECT doctor_id, starts_at FROM slots WHERE id = ?", (slot_id,)).fetchone()
                cur = self._conn.execute(
                    "INSERT INTO bookings(patient_id, slot_id, doctor_id, starts_at, status, created_at, updated_at) VALUES (?,?,?,?,'booked',?,?)",
                    (patient_id, slot_id, s["doctor_id"], s["starts_at"], ts, ts),
                )
                bid = cur.lastrowid
                self._conn.execute("UPDATE slots SET status = 'booked', booking_id = ? WHERE id = ?", (bid, slot_id))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self.sql_log.append(("BOOK", (patient_id, slot_id)))
        b = self.booking(bid)
        assert b is not None
        return b

    def booking(self, booking_id: int) -> Optional[Booking]:
        rows = self._q(
            "SELECT b.*, d.full_name, d.specialty, d.cabinet FROM bookings b JOIN doctors d ON d.id = b.doctor_id WHERE b.id = ?", (booking_id,)
        )
        return self._booking(rows[0]) if rows else None

    def booking_by_remote_id(self, remote_id: str) -> Optional[Booking]:
        rows = self._q("SELECT id FROM bookings WHERE remote_id = ?", (str(remote_id),))
        return self.booking(rows[0]["id"]) if rows else None

    def upcoming_booking(self, patient_id: int, after: Optional[datetime] = None) -> Optional[Booking]:
        rows = self._q(
            "SELECT b.*, d.full_name, d.specialty, d.cabinet FROM bookings b JOIN doctors d ON d.id = b.doctor_id "
            "WHERE b.patient_id = ? AND b.status = 'booked' AND b.starts_at >= ? ORDER BY b.starts_at LIMIT 1",
            (patient_id, (after or self.now()).isoformat(timespec="minutes")),
        )
        return self._booking(rows[0]) if rows else None

    def bookings_on(self, day: date) -> list[Booking]:
        rows = self._q(
            "SELECT b.*, d.full_name, d.specialty, d.cabinet FROM bookings b JOIN doctors d ON d.id = b.doctor_id "
            "WHERE b.status = 'booked' AND date(b.starts_at) = ? ORDER BY b.starts_at", (day.isoformat(),)
        )
        return [self._booking(r) for r in rows]

    def cancel(self, booking_id: int) -> Booking:
        ts = self.now().isoformat(timespec="seconds")
        with self._lock:
            b = self.booking(booking_id)
            if b is None:
                raise NotFound(f"booking {booking_id}")
            if b.status != "booked":
                raise RegistryError(f"booking {booking_id} is {b.status}")
            self._x("UPDATE bookings SET status = 'cancelled', updated_at = ?, sync_state = 'pending' WHERE id = ?", (ts, booking_id))
            self._x("UPDATE slots SET status = 'free', booking_id = NULL WHERE id = ?", (b.slot_id,))
        out = self.booking(booking_id)
        assert out is not None
        return out

    def confirm(self, booking_id: int) -> Booking:
        ts = self.now().isoformat(timespec="seconds")
        n = self._x("UPDATE bookings SET confirmed = 1, updated_at = ? WHERE id = ? AND status = 'booked'", (ts, booking_id))
        if not n:
            raise NotFound(f"booking {booking_id}")
        out = self.booking(booking_id)
        assert out is not None
        return out

    def reschedule(self, booking_id: int, new_slot_id: int) -> Booking:
        """Atomic: new slot booked first, old one released only on success."""
        with self._lock:
            old = self.booking(booking_id)
            if old is None or old.status != "booked":
                raise NotFound(f"booking {booking_id}")
            new = self.book(old.patient_id, new_slot_id)
            ts = self.now().isoformat(timespec="seconds")
            self._x("UPDATE bookings SET status = 'rescheduled', updated_at = ? WHERE id = ?", (ts, booking_id))
            self._x("UPDATE slots SET status = 'free', booking_id = NULL WHERE id = ?", (old.slot_id,))
        return new

    # ---- info -----------------------------------------------------------------------------

    def clinic_hours(self, weekday: Optional[int] = None) -> list[tuple[int, str, str]]:
        rows = self._q("SELECT weekday, opens, closes FROM clinic_hours" + (" WHERE weekday = ?" if weekday is not None else "") + " ORDER BY weekday",
                       (weekday,) if weekday is not None else ())
        return [(r["weekday"], r["opens"], r["closes"]) for r in rows]

    def holidays(self, after: Optional[date] = None, limit: int = 5) -> list[tuple[date, str]]:
        rows = self._q("SELECT day, note FROM holidays WHERE day >= ? ORDER BY day LIMIT ?", ((after or self.now().date()).isoformat(), limit))
        return [(date.fromisoformat(r["day"]), r["note"]) for r in rows]

    def doctors(self, specialty: Optional[str] = None) -> list[dict]:
        rows = self._q("SELECT * FROM doctors" + (" WHERE specialty = ?" if specialty else ""), (specialty,) if specialty else ())
        return [dict(r) for r in rows]

    def record_operator_request(self, call_id: str, reason: str, payload: str) -> int:
        return self._x("INSERT INTO operator_requests(call_id, reason, payload, created_at) VALUES (?,?,?,?)", (call_id, reason, payload, self.now().isoformat(timespec="seconds")))

    # ---- stand synchronisation bookkeeping -------------------------------------------------

    def remote_id_of(self, booking_id: int) -> Optional[str]:
        rows = self._q("SELECT remote_id FROM bookings WHERE id = ?", (int(booking_id),))
        return rows[0]["remote_id"] if rows and rows[0]["remote_id"] else None

    def mark_booking_synced(self, booking_id: int, remote_id: Optional[str] = None, state: str = "synced") -> None:
        if remote_id is not None:
            self._x("UPDATE bookings SET sync_state = ?, remote_id = ?, updated_at = ? WHERE id = ?", (state, str(remote_id), self.now().isoformat(timespec="seconds"), int(booking_id)))
        else:
            self._x("UPDATE bookings SET sync_state = ?, updated_at = ? WHERE id = ?", (state, self.now().isoformat(timespec="seconds"), int(booking_id)))

    def mark_operator_synced(self, request_id: int, remote_id: Optional[str] = None, state: str = "synced") -> None:
        self._x("UPDATE operator_requests SET sync_state = ?, remote_id = COALESCE(?, remote_id) WHERE id = ?", (state, None if remote_id is None else str(remote_id), int(request_id)))

    def sync_report(self) -> dict:
        """Counts per sync_state – the operator's console and /v1/stand.status show it."""
        out: dict = {"bookings": {}, "operator_requests": {}}
        for r in self._q("SELECT sync_state AS s, COUNT(*) AS n FROM bookings GROUP BY sync_state"):
            out["bookings"][r["s"] or "pending"] = r["n"]
        for r in self._q("SELECT sync_state AS s, COUNT(*) AS n FROM operator_requests GROUP BY sync_state"):
            out["operator_requests"][r["s"] or "pending"] = r["n"]
        return out


def _digits(raw: object) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _as_date(raw: object) -> Optional[date]:
    """Accept ISO ``1980-05-12``, Russian ``12.05.1980``, ``12051980`` and Excel datetimes."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    d = _digits(s)
    if len(d) == 8:
        try:
            return date(int(d[4:8]), int(d[2:4]), int(d[0:2]))
        except ValueError:
            return None
    return None


def _xlsx_rows(path: Path | str, sheet: Optional[str] = None) -> Iterable[dict]:  # pragma: no cover - needs openpyxl
    import openpyxl  # type: ignore[import-not-found]

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() for h in next(rows)]
    for values in rows:
        yield dict(zip(header, values))


def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def seed_demo(reg: MockRegistry, start: date, days: int = 14) -> bool:
    """A small polyclinic: 8 doctors, weekdays, 15-minute slots.

    Idempotent: several module processes may share one SQLite file (compose volume) and each of them
    calls ``seed_demo`` at start-up – only the first one populates the mirror.  Returns True when seeded.
    """
    with reg._lock:  # noqa: SLF001 - seeding is part of the registry's own API
        try:
            reg._conn.execute("BEGIN IMMEDIATE")  # serialises concurrent seeders across processes
            if reg._conn.execute("SELECT count(*) AS n FROM patients").fetchone()[0]:
                reg._conn.execute("ROLLBACK")
                return False
            _seed_rows(reg, start, days)
            reg._conn.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:  # another process won the race between our check and insert
            reg._conn.execute("ROLLBACK")
            return False


def _seed_rows(reg: MockRegistry, start: date, days: int) -> None:
    docs = [
        ("Иванова", "Иванова Мария Петровна", "терапевт", "101", False),
        ("Петров", "Петров Сергей Иванович", "терапевт", "102", False),
        ("Сидорова", "Сидорова Анна Викторовна", "педиатр", "201", False),
        ("Кузнецов", "Кузнецов Олег Николаевич", "хирург", "301", False),
        ("Смирнова", "Смирнова Елена Андреевна", "кардиолог", "302", True),
        ("Волков", "Волков Дмитрий Сергеевич", "невролог", "303", True),
        ("Морозова", "Морозова Ольга Игоревна", "офтальмолог", "401", False),
        ("Лебедев", "Лебедев Андрей Павлович", "отоларинголог", "402", False),
    ]
    for i, (ln, fn, sp, cab, ref) in enumerate(docs):
        did = reg.add_doctor(ln, fn, sp, cab, ref)
        for d in range(days):
            day = start + timedelta(days=d)
            if day.weekday() >= 5:
                continue
            if i % 2 == 0:
                reg.add_slots(did, day, time(8, 0), time(14, 0), 15)
            else:
                reg.add_slots(did, day, time(14, 0), time(20, 0), 15)
    reg.set_clinic_hours({wd: ("08:00", "20:00") for wd in range(5)} | {5: ("09:00", "15:00")})
    reg.add_holiday(date(start.year, 11, 4), "День народного единства — поликлиника не работает")
    reg.add_holiday(date(start.year + 1, 1, 1), "Новогодние праздники: дежурный режим 1–8 января")
    reg.add_patient("Иванов", "Иван", "Иванович", date(1980, 5, 12), "12345678901", "1001", "79035551234")
    reg.add_patient("Иванова", "Анна", "Ивановна", date(2015, 3, 1), "22345678901", "1002", "79035551234")
    reg.add_patient("Петрова", "Мария", "Сергеевна", date(1992, 11, 23), "32345678901", "1003", "79165550000")
