"""One relational service schedule for availability, callbacks and night compute."""

from datetime import datetime, time
from calls.tts.verbalize import time_to_words

SCHEMA = """
CREATE TABLE IF NOT EXISTS service_hours(service TEXT NOT NULL,weekday INTEGER NOT NULL,opens TEXT,closes TEXT,closed INTEGER NOT NULL DEFAULT 0,source TEXT NOT NULL,PRIMARY KEY(service,weekday));
CREATE TABLE IF NOT EXISTS service_calendar(service TEXT NOT NULL,day TEXT NOT NULL,opens TEXT,closes TEXT,closed INTEGER NOT NULL DEFAULT 0,source TEXT NOT NULL,PRIMARY KEY(service,day));
"""


def initialize(reg):
    reg._conn.executescript(SCHEMA)
    for day in range(7):
        opening, closing, closed = (
            ("07:00", "20:00", 0)
            if day < 5
            else ("08:30", "15:00", 0)
            if day == 5
            else (None, None, 1)
        )
        reg._x(
            "INSERT OR IGNORE INTO service_hours VALUES(?,?,?,?,?,?)",
            ("registrar", day, opening, closing, closed, "clinic-regulation §2.2"),
        )


def schedule(reg, day, service="registrar"):
    exception = reg.execute(
        "SELECT * FROM service_calendar WHERE service=? AND day=?",
        (service, day.isoformat()),
    )
    if exception:
        return exception[0]
    rows = reg.execute(
        "SELECT * FROM service_hours WHERE service=? AND weekday=?",
        (service, day.weekday()),
    )
    return (
        rows[0]
        if rows
        else {"closed": 1, "opens": None, "closes": None, "source": "missing_schedule"}
    )


def is_open(reg, at, service="registrar"):
    row = schedule(reg, at.date(), service)
    return not row["closed"] and time.fromisoformat(row["opens"]) <= at.time().replace(
        tzinfo=None
    ) < time.fromisoformat(row["closes"])


def summary(reg):
    rows = reg.execute(
        "SELECT * FROM service_hours WHERE service='registrar' ORDER BY weekday"
    )
    parts = []
    for row in rows:
        if row["closed"]:
            continue
        label = [
            "понедельник",
            "вторник",
            "среда",
            "четверг",
            "пятница",
            "суббота",
            "воскресенье",
        ][row["weekday"]]
        parts.append(
            label
            + ": с "
            + time_to_words(time.fromisoformat(row["opens"]))
            + " до "
            + time_to_words(time.fromisoformat(row["closes"]))
        )
    return "; ".join(parts)
