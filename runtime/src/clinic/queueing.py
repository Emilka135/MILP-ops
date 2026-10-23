"""Measured booking waits and a consent-based cancellation backfill queue."""

import json
import math
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS wait_requests(id INTEGER PRIMARY KEY,patient_id INTEGER NOT NULL,doctor_id INTEGER,specialty TEXT NOT NULL,request_at TEXT NOT NULL,appointment_at TEXT,state TEXT NOT NULL DEFAULT 'pending',source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS early_consent(patient_id INTEGER NOT NULL,specialty TEXT NOT NULL,doctor_id INTEGER,procedure_code TEXT,prep_confirmed INTEGER NOT NULL DEFAULT 0,opt_in INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(patient_id,specialty));
CREATE TABLE IF NOT EXISTS backfill_campaigns(slot_id INTEGER PRIMARY KEY,generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS backfill_offers(id INTEGER PRIMARY KEY,slot_id INTEGER NOT NULL,patient_id INTEGER NOT NULL,old_booking_id INTEGER,state TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,campaign INTEGER NOT NULL DEFAULT 0,UNIQUE(slot_id,patient_id,campaign));
"""

CLINIC_ZONE = timezone(timedelta(hours=4))
OFFER_TTL_SECONDS = 300


def _instant(value):
    """Registry naive timestamps are clinic wall time, never host local time."""
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (
        stamp.replace(tzinfo=CLINIC_ZONE) if stamp.tzinfo is None else stamp
    ).astimezone(UTC)


def _stamp(value):
    return _instant(value).isoformat(timespec="microseconds")


def waiting_estimate(rows, at, min_samples=10):
    observations = []
    withdrawn = 0
    for row in rows:
        if row.get("state") == "withdrawn":
            withdrawn += 1
            continue
        start = _instant(row["request_at"])
        end = (
            _instant(row["appointment_at"])
            if row.get("appointment_at")
            else _instant(at)
        )
        event = bool(row.get("appointment_at")) and end <= _instant(at)
        end = min(end, _instant(at))
        if end >= start:
            observations.append(((end - start).total_seconds() / 86400, event))
    if len(observations) < min_samples:
        return {
            "status": "insufficient_data",
            "samples": len(observations),
            "withdrawn": withdrawn,
            "median_days": None,
            "p75_days": None,
            "method": "Kaplan-Meier",
        }
    survival = 1.0
    risk = len(observations)
    median = p75 = None
    for duration in sorted({x[0] for x in observations}):
        events = sum(d == duration and event for d, event in observations)
        censored = sum(d == duration and not event for d, event in observations)
        survival *= 1 - events / risk
        if median is None and survival <= 0.5:
            median = duration
        if p75 is None and survival <= 0.25:
            p75 = duration
        risk -= events + censored
        if not risk:
            break
    return {
        "status": "measured" if median is not None else "censored",
        "samples": len(observations),
        "withdrawn": withdrawn,
        "median_days": median,
        "p75_days": p75,
        "method": "Kaplan-Meier",
    }


def reminder_days(stats, prep_hours=0, fill_hours=0):
    waiting = stats.get("median_days")
    if waiting is None:
        return {
            "days": None,
            "basis": "unknown_waiting_time",
            "early_offer_enabled": False,
        }
    required = max(
        2,
        math.ceil(waiting / 3),
        math.ceil(prep_hours / 24) + 1,
        math.ceil(fill_hours / 24) + 1,
    )
    return {
        "days": max(min(14, required), math.ceil(prep_hours / 24) + 1),
        "basis": "measured_pilot_policy",
        "early_offer_enabled": waiting > 3,
    }


class WaitingQueue:
    def __init__(self, crm):
        self.crm = crm
        self.reg = crm.reg
        self.reg._conn.executescript(SCHEMA)
        with self._transaction() as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(backfill_offers)")}
            request_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(wait_requests)")
            }
            for name in ("request_key", "request_payload"):
                if name not in request_columns:
                    conn.execute(f"ALTER TABLE wait_requests ADD COLUMN {name} TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_wait_request_key ON wait_requests(request_key) WHERE request_key IS NOT NULL"
            )
            for column in ("expires_at", "result"):
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE backfill_offers ADD COLUMN {column} TEXT"
                    )
            if "campaign" not in columns:
                # Preserve durable IDs/results while replacing the old lifetime
                # UNIQUE(slot_id,patient_id) constraint with a campaign-scoped one.
                conn.execute("ALTER TABLE backfill_offers RENAME TO old_backfill_offers")
                conn.execute(
                    "CREATE TABLE backfill_offers(id INTEGER PRIMARY KEY,slot_id INTEGER NOT NULL,patient_id INTEGER NOT NULL,old_booking_id INTEGER,state TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,expires_at TEXT,result TEXT,campaign INTEGER NOT NULL DEFAULT 0,UNIQUE(slot_id,patient_id,campaign))"
                )
                conn.execute(
                    "INSERT INTO backfill_offers(id,slot_id,patient_id,old_booking_id,state,created_at,expires_at,result) SELECT id,slot_id,patient_id,old_booking_id,state,created_at,expires_at,result FROM old_backfill_offers"
                )
                conn.execute("DROP TABLE old_backfill_offers")
            # Old versions could create several pending offers for one slot. Quarantine
            # that campaign rather than choosing an arbitrary patient to call.
            conn.execute(
                "UPDATE backfill_offers SET state='uncertain' WHERE state='pending' AND slot_id IN (SELECT slot_id FROM backfill_offers WHERE state='pending' GROUP BY slot_id HAVING count(*)>1)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_backfill_one_pending ON backfill_offers(slot_id) WHERE state IN ('pending','accepting')"
            )
        path = Path(__file__).resolve().parents[3] / "data/preparation/catalog.json"
        self.preparation = (
            json.loads(path.read_text())
            if path.exists()
            else {"procedures": [], "enabled": False}
        )

    @contextmanager
    def _transaction(self):
        # SQLite protects independent CRM owners too; a Python lock alone does not.
        with self.reg._lock:
            conn = self.reg._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _approval(self, item, at):
        for record in (self.preparation, item):
            if (
                record.get("enabled") is not True
                or record.get("review_required", True) is not False
            ):
                return False, "clinic_review_required"
            if not all(
                record.get(key)
                for key in ("clinic_id", "approved_by", "approved_at", "review_due_on")
            ):
                return False, "clinic_approval_missing"
            try:
                if _instant(record["approved_at"]) > _instant(at):
                    return False, "clinic_approval_not_effective"
                if (
                    date.fromisoformat(record["review_due_on"])
                    < _instant(at).astimezone(CLINIC_ZONE).date()
                ):
                    return False, "clinic_approval_expired"
            except (TypeError, ValueError):
                return False, "invalid_clinic_approval"
        if item["clinic_id"] != self.preparation["clinic_id"]:
            return False, "clinic_approval_mismatch"
        return True, "approved_protocol"

    def _expire(self, conn, at):
        for row in conn.execute(
            "SELECT id,created_at,expires_at FROM backfill_offers WHERE state='pending'"
        ).fetchall():
            expiry = (
                _instant(row["expires_at"])
                if row["expires_at"]
                else _instant(row["created_at"]) + timedelta(seconds=OFFER_TTL_SECONDS)
            )
            if expiry <= _instant(at):
                conn.execute(
                    "UPDATE backfill_offers SET state='expired' WHERE id=? AND state='pending'",
                    (row["id"],),
                )

    @staticmethod
    def _offer(row):
        return {
            "offer_id": row["id"],
            "campaign": row["campaign"],
            "slot_id": row["slot_id"],
            "patient_id": row["patient_id"],
            "old_booking_id": row["old_booking_id"],
            "state": row["state"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"]
            or _stamp(
                _instant(row["created_at"]) + timedelta(seconds=OFFER_TTL_SECONDS)
            ),
        }

    def stats(self, args, at):
        params = [str(args["specialty"])]
        query = "SELECT request_at,appointment_at,state FROM wait_requests WHERE specialty=?"
        if args.get("doctor_id"):
            query += " AND doctor_id=?"
            params.append(int(args["doctor_id"]))
        return waiting_estimate(
            self.reg.execute(query, params), at, int(args.get("min_samples", 10))
        )

    def preparation_ok(self, code, slot_at, at, confirmed=False):
        if not code:
            return True, "ordinary_consultation"
        procedure = next(
            (p for p in self.preparation.get("procedures", []) if p["id"] == code), None
        )
        if not procedure:
            return False, "unknown_procedure"
        ok, reason = self._approval(procedure, at)
        if not ok:
            return ok, reason
        hours = (_instant(slot_at) - _instant(at)).total_seconds() / 3600
        try:
            lead_hours = float(procedure["lead_time_hours"])
        except (KeyError, TypeError, ValueError):
            return False, "invalid_preparation_lead_time"
        if not math.isfinite(lead_hours) or lead_hours < 0:
            return False, "invalid_preparation_lead_time"
        if hours < lead_hours:
            return False, "insufficient_preparation_time"
        if procedure.get("requires_individual_plan", True) and not confirmed:
            return False, "individual_preparation_not_confirmed"
        return True, "approved_protocol"

    def candidates(self, args, at):
        sid = int(args["slot_id"])
        slot = self.reg.slot(sid)
        if not slot:
            return "error", {"error": "slot_missing"}
        row = self.reg.execute("SELECT status FROM slots WHERE id=?", (sid,))[0]
        if row["status"] != "free":
            return "taken", {}
        if _instant(slot.starts_at) <= _instant(at):
            return "blocked", {"reason": "slot_in_past"}
        doctor = self.reg.execute(
            "SELECT active FROM doctors WHERE id=?", (slot.doctor_id,)
        )
        if not doctor or not doctor[0]["active"]:
            return "blocked", {"reason": "doctor_inactive"}
        stats = self.stats(
            {
                "specialty": slot.specialty,
                "doctor_id": slot.doctor_id,
                "min_samples": args.get("min_samples", 10),
            },
            at,
        )
        policy = reminder_days(stats)
        if not policy["early_offer_enabled"]:
            return "blocked", {
                "reason": "short_or_unknown_wait",
                "statistics": stats,
                "policy": policy,
            }
        consents = self.reg.execute(
            "SELECT * FROM early_consent WHERE opt_in=1 AND specialty=? AND (doctor_id IS NULL OR doctor_id=?)",
            (slot.specialty, slot.doctor_id),
        )
        campaign = self.reg.execute(
            "SELECT generation FROM backfill_campaigns WHERE slot_id=?", (sid,)
        )
        generation = campaign[0]["generation"] if campaign else 0
        candidates = []
        for consent in consents:
            pid = consent["patient_id"]
            if self.reg.execute(
                "SELECT id FROM backfill_offers WHERE slot_id=? AND patient_id=? AND campaign=?",
                (sid, pid, generation),
            ):
                continue
            ok, _reason = self.preparation_ok(
                consent["procedure_code"],
                slot.starts_at,
                at,
                bool(consent["prep_confirmed"]),
            )
            if not ok:
                continue
            # Timestamps can carry different UTC offsets. SQL text ordering is
            # not chronological, and an earlier existing visit is not a wait-list
            # candidate for a later visit with the same doctor.
            bookings = self.reg.execute(
                "SELECT id,starts_at FROM bookings WHERE patient_id=? AND status='booked' AND doctor_id=?",
                (pid, slot.doctor_id),
            )
            if bookings:
                old = min(bookings, key=lambda row: _instant(row["starts_at"]))
                gain = (
                    _instant(old["starts_at"]) - _instant(slot.starts_at)
                ).total_seconds() / 86400
                if gain <= 0:
                    continue
                candidates.append(
                    {
                        "patient_id": pid,
                        "old_booking_id": old["id"],
                        "priority": 0,
                        "sort": _stamp(old["starts_at"]),
                        "gain_days": gain,
                    }
                )
            else:
                requests = self.reg.execute(
                    "SELECT request_at FROM wait_requests WHERE patient_id=? AND specialty=? AND state='pending' AND appointment_at IS NULL AND (doctor_id IS NULL OR doctor_id=?)",
                    (pid, slot.specialty, slot.doctor_id),
                )
                if requests:
                    candidates.append(
                        {
                            "patient_id": pid,
                            "old_booking_id": None,
                            "priority": 1,
                            "sort": min(_stamp(row["request_at"]) for row in requests),
                            "gain_days": None,
                        }
                    )
        candidates.sort(key=lambda c: (c["priority"], c["sort"], c["patient_id"]))
        return "ok", {
            "candidates": candidates,
            "statistics": stats,
            "policy": policy,
            "slot_id": sid,
        }

    def _offer_valid(self, offer, at):
        slot = self.reg.slot(offer["slot_id"])
        if not slot or _instant(slot.starts_at) <= _instant(at):
            return False, "slot_unavailable"
        available = self.reg.execute("SELECT status FROM slots WHERE id=?", (slot.id,))
        doctor = self.reg.execute(
            "SELECT active FROM doctors WHERE id=?", (slot.doctor_id,)
        )
        if (
            not available
            or available[0]["status"] != "free"
            or not doctor
            or not doctor[0]["active"]
        ):
            return False, "slot_unavailable"
        consent = self.reg.execute(
            "SELECT * FROM early_consent WHERE patient_id=? AND specialty=? AND opt_in=1 AND (doctor_id IS NULL OR doctor_id=?)",
            (offer["patient_id"], slot.specialty, slot.doctor_id),
        )
        if not consent:
            return False, "consent_withdrawn"
        ok, reason = self.preparation_ok(
            consent[0]["procedure_code"],
            slot.starts_at,
            at,
            bool(consent[0]["prep_confirmed"]),
        )
        if not ok:
            return ok, reason
        if offer["old_booking_id"]:
            old = self.reg.booking(offer["old_booking_id"])
            if (
                not old
                or old.patient_id != offer["patient_id"]
                or old.status != "booked"
                or old.doctor_id != slot.doctor_id
                or (_instant(old.starts_at) - _instant(slot.starts_at)).total_seconds()
                <= 0
            ):
                return False, "old_booking_changed"
        else:
            # A request may be withdrawn, assigned elsewhere or satisfied by
            # another channel after the offer was issued.
            requests = self.reg.execute(
                "SELECT id FROM wait_requests WHERE patient_id=? AND specialty=? AND state='pending' AND appointment_at IS NULL AND (doctor_id IS NULL OR doctor_id=?)",
                (offer["patient_id"], slot.specialty, slot.doctor_id),
            )
            bookings = self.reg.execute(
                "SELECT id FROM bookings WHERE patient_id=? AND doctor_id=? AND status='booked'",
                (offer["patient_id"], slot.doctor_id),
            )
            if not requests or bookings:
                return False, "waiting_request_changed"
        return True, "eligible"

    def next_offer(self, args, at):
        sid = int(args["slot_id"])
        with self._transaction() as conn:
            self._expire(conn, at)
            conn.execute(
                "INSERT OR IGNORE INTO backfill_campaigns(slot_id) VALUES(?)", (sid,)
            )
            generation = conn.execute(
                "SELECT generation FROM backfill_campaigns WHERE slot_id=?", (sid,)
            ).fetchone()["generation"]
            accepted = conn.execute(
                "SELECT id FROM backfill_offers WHERE slot_id=? AND campaign=? AND state='accepted'",
                (sid, generation),
            ).fetchone()
            slot_state = conn.execute(
                "SELECT status FROM slots WHERE id=?", (sid,)
            ).fetchone()
            if accepted and slot_state and slot_state["status"] == "free":
                # A successfully filled visit was cancelled later. This is a
                # new campaign, not a retry of the already completed offer.
                generation += 1
                conn.execute(
                    "UPDATE backfill_campaigns SET generation=? WHERE slot_id=?",
                    (generation, sid),
                )
            current = conn.execute(
                "SELECT * FROM backfill_offers WHERE slot_id=? AND campaign=? AND state IN ('pending','accepting','uncertain','accepted') ORDER BY id",
                (sid, generation),
            ).fetchall()
            for row in current:
                if row["state"] == "accepted":
                    return "complete", {
                        "offer_id": row["id"],
                        "reason": "campaign_filled",
                    }
                if row["state"] == "uncertain":
                    return "blocked", {
                        "offer_id": row["id"],
                        "reason": "reconciliation_required",
                    }
                if row["state"] == "accepting":
                    return "in_progress", self._offer(row)
                ok, _reason = self._offer_valid(row, at)
                if ok:
                    return "ok", self._offer(row)
                conn.execute(
                    "UPDATE backfill_offers SET state='invalid' WHERE id=?",
                    (row["id"],),
                )
            status, data = self.candidates(args, at)
            if status != "ok":
                return status, data
            if not data["candidates"]:
                return "empty", data
            candidate = data["candidates"][0]
            # One durable offer per slot. The same API retry returns this exact ID,
            # including after another connection or process takes over.
            expiry = min(
                _instant(at) + timedelta(seconds=OFFER_TTL_SECONDS),
                _instant(self.reg.slot(sid).starts_at),
            )
            oid = conn.execute(
                "INSERT INTO backfill_offers(slot_id,patient_id,old_booking_id,created_at,expires_at,campaign) VALUES(?,?,?,?,?,?)",
                (
                    sid,
                    candidate["patient_id"],
                    candidate["old_booking_id"],
                    _stamp(at),
                    _stamp(expiry),
                    generation,
                ),
            ).lastrowid
            return "ok", self._offer(
                conn.execute(
                    "SELECT * FROM backfill_offers WHERE id=?", (oid,)
                ).fetchone()
            )

    def accept_offer(self, args, at):
        oid = int(args["offer_id"])
        with self._transaction() as conn:
            self._expire(conn, at)
            offer = conn.execute(
                "SELECT * FROM backfill_offers WHERE id=?", (oid,)
            ).fetchone()
            if not offer:
                return "error", {"error": "offer_missing"}
            if int(args.get("verified_patient_id") or 0) != offer["patient_id"]:
                return "error", {"error": "identity_required"}
            if offer["state"] == "accepted":
                return "ok", json.loads(offer["result"] or "{}")
            if offer["state"] == "accepting":
                return "in_progress", {"offer_id": oid}
            if offer["state"] != "pending":
                return "error", {"error": "offer_not_pending", "state": offer["state"]}
            ok, reason = self._offer_valid(offer, at)
            if not ok:
                conn.execute(
                    "UPDATE backfill_offers SET state='invalid' WHERE id=?", (oid,)
                )
                return "blocked", {"reason": reason}
            conn.execute(
                "UPDATE backfill_offers SET state='accepting' WHERE id=? AND state='pending'",
                (oid,),
            )
            offer = dict(offer)
        # Do not hold a SQLite write transaction during the remote request. The
        # accepting state excludes a second accept/decline/offer even after expiry.
        try:
            status, result = self.crm.handle(
                "reschedule" if offer["old_booking_id"] else "book",
                {
                    "operation_key": "backfill:" + str(oid),
                    "patient_id": offer["patient_id"],
                    "booking_id": offer["old_booking_id"],
                    "slot_id": offer["slot_id"],
                    "call_id": args.get("call_id", "backfill"),
                },
            )
        except Exception:  # noqa: BLE001 -- a lost remote outcome must quarantine the offer
            status, result = (
                "error",
                {"error": "booking_outcome_unknown", "operation_state": "uncertain"},
            )
        if status == "ok":
            state = "accepted"
        elif status == "taken" or result.get("operation_state") == "failed":
            state = "failed"
        else:
            state = "uncertain"
        with self._transaction() as conn:
            conn.execute(
                "UPDATE backfill_offers SET state=?,result=? WHERE id=?",
                (state, json.dumps(result, ensure_ascii=False), oid),
            )
            if state == "accepted":
                conn.execute(
                    "UPDATE wait_requests SET state='scheduled',appointment_at=? WHERE patient_id=? AND specialty=? AND (doctor_id IS NULL OR doctor_id=?) AND state='pending' AND appointment_at IS NULL",
                    (
                        _stamp(self.reg.slot(offer["slot_id"]).starts_at),
                        offer["patient_id"],
                        self.reg.slot(offer["slot_id"]).specialty,
                        self.reg.slot(offer["slot_id"]).doctor_id,
                    ),
                )
        return status, result

    def handle(self, op, args, at):
        if op == "waiting.record":
            start = datetime.fromisoformat(args["request_at"])
            end = (
                datetime.fromisoformat(args["appointment_at"])
                if args.get("appointment_at")
                else None
            )
            if end and _instant(end) < _instant(start):
                return "error", {"error": "negative_wait"}
            if _instant(start) > _instant(at):
                return "error", {"error": "request_in_future"}
            if not self.reg.patient_by_id(int(args["patient_id"])):
                return "error", {"error": "patient_missing"}
            values = (
                int(args["patient_id"]),
                args.get("doctor_id"),
                args["specialty"],
                _stamp(start),
                _stamp(end) if end else None,
                str(args.get("source", "clinic_event")),
            )
            request_key = args.get("request_key")
            if request_key is not None and (
                not isinstance(request_key, str)
                or not request_key.strip()
                or len(request_key) > 128
            ):
                return "error", {"error": "invalid_request_key"}
            payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
            with self._transaction() as conn:
                if request_key is not None:
                    existing = conn.execute(
                        "SELECT * FROM wait_requests WHERE request_key=?",
                        (request_key,),
                    ).fetchone()
                    if existing:
                        recorded = tuple(existing[key] for key in (
                            "patient_id", "doctor_id", "specialty", "request_at",
                            "appointment_at", "source",
                        ))
                        original = existing["request_payload"]
                        conflict = (
                            original != payload
                            if original is not None
                            else recorded != values
                        )
                        if conflict:
                            return "error", {"error": "request_key_conflict"}
                        return "ok", {"request_id": existing["id"], "recorded": False}
                state = (
                    "fulfilled" if _instant(end) <= _instant(at) else "scheduled"
                ) if end else "pending"
                request_id = conn.execute(
                    "INSERT INTO wait_requests(patient_id,doctor_id,specialty,request_at,appointment_at,source,state,request_key,request_payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    (*values, state, request_key, payload),
                ).lastrowid
            return "ok", {"request_id": request_id, "recorded": True}
        if op == "waiting.withdraw":
            request_id = int(args["request_id"])
            with self._transaction() as conn:
                record = conn.execute(
                    "SELECT * FROM wait_requests WHERE id=?", (request_id,)
                ).fetchone()
                if not record:
                    return "error", {"error": "request_missing"}
                if int(args.get("verified_patient_id") or 0) != record["patient_id"]:
                    return "error", {"error": "identity_required"}
                if record["state"] not in ("pending", "withdrawn"):
                    return "error", {"error": "request_not_pending"}
                conn.execute(
                    "UPDATE wait_requests SET state='withdrawn' WHERE id=?", (request_id,)
                )
            return "ok", {"request_id": request_id, "state": "withdrawn"}
        if op == "waiting.stats":
            stats = self.stats(args, at)
            return "ok", {"statistics": stats, "policy": reminder_days(stats)}
        if op == "waiting.join":
            pid = int(args["patient_id"])
            if not self.reg.patient_by_id(pid):
                return "error", {"error": "patient_missing"}
            self.reg._x(
                "INSERT OR REPLACE INTO early_consent VALUES(?,?,?,?,?,?,?)",
                (
                    pid,
                    args["specialty"],
                    args.get("doctor_id"),
                    args.get("procedure_code"),
                    int(args.get("prep_confirmed") is True),
                    int(args.get("opt_in") is True),
                    at.isoformat(),
                ),
            )
            return "ok", {}
        if op == "preparation.lookup":
            item = next(
                (
                    x
                    for x in self.preparation.get("procedures", [])
                    if x["id"] == args.get("procedure_code")
                ),
                None,
            )
            if not item:
                return "empty", {}
            ok, reason = self._approval(item, at)
            return (
                ("ok", item)
                if ok
                else ("blocked", {"reason": reason, "procedure_code": item["id"]})
            )
        if op == "backfill.candidates":
            return self.candidates(args, at)
        if op == "backfill.next":
            return self.next_offer(args, at)
        if op == "backfill.decline":
            oid = int(args["offer_id"])
            with self._transaction() as conn:
                self._expire(conn, at)
                row = conn.execute(
                    "SELECT state FROM backfill_offers WHERE id=?", (oid,)
                ).fetchone()
                if not row or row["state"] not in ("pending", "declined"):
                    return "error", {"error": "offer_not_pending"}
                conn.execute(
                    "UPDATE backfill_offers SET state='declined' WHERE id=? AND state='pending'",
                    (oid,),
                )
                return "ok", {"offer_id": oid, "state": "declined"}
        if op == "backfill.accept":
            return self.accept_offer(args, at)
        return "error", {"error": "unknown_waiting_operation"}
