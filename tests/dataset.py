"""A small analytics dataset (July 2026) built so every expected number can be checked by hand.

July 2026: the 1st is a Wednesday -> 23 weekdays; weekends are 4/5, 11/12, 18/19, 25/26.
Traps on purpose: tie at the leaderboard cutoff, mid-month joiner, employee without logs,
employee who joins after the month, an orphan log (no employee), weekend records, an
open record (no punch_out), a legacy document (no half_day/history), exact rounding ties.
"""
from datetime import datetime, timedelta

from app.main import IST, UTC


def emp(code, dept, joined, start="09:30", end="18:30"):
    return dict(emp_code=code, name=f"Name {code[-4:]}", email=f"{code.lower()}@example.com", department=dept,
                shift_start=start, shift_end=end, joined_on=joined,
                created_at=datetime(2026, 1, 5, 4, 30, tzinfo=UTC))


def log(code, day, status, wh=None, late=0, ot=0, half=False, legacy=False):
    presence = status in ("PRESENT", "WFH", "ON_DUTY")
    d = {"emp_code": code, "date": day, "status": status}
    if presence:
        pin = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=IST).astimezone(UTC)
        d.update(punch_in=pin, punch_out=None if wh is None else pin + timedelta(hours=wh))
    else:
        d.update(punch_in=None, punch_out=None)
    d.update(work_hours=wh, late_minutes=late, overtime_minutes=ot)
    if not legacy:
        d.update(half_day=half, history=[])
    return d


EMPLOYEES = [
    emp("EMP0001", "Engineering", "2026-01-05"),
    emp("EMP0002", "Engineering", "2026-07-13"),          # joins mid-month
    emp("EMP0003", "Engineering", "2026-02-01"),          # no logs at all
    emp("EMP0004", "Sales", "2026-02-01"),
    emp("EMP0005", "Sales", "2026-02-01"),
    emp("EMP0006", "Sales", "2026-02-01"),
    emp("EMP0007", "Support", "2026-03-10", start="22:00", end="06:00"),
    emp("EMP0008", "Legal", "2026-08-03"),                # joins after July
]

LOGS = [
    # EMP0001
    log("EMP0001", "2026-07-01", "PRESENT", wh=9.0),
    log("EMP0001", "2026-07-02", "PRESENT", wh=9.08, late=35, ot=40),
    log("EMP0001", "2026-07-03", "WFH", wh=3.5, half=True),
    log("EMP0001", "2026-07-04", "PRESENT", wh=6.0, late=15, ot=60),   # Saturday
    log("EMP0001", "2026-07-06", "LEAVE"),
    log("EMP0001", "2026-07-07", "ABSENT"),
    log("EMP0001", "2026-07-08", "ON_DUTY", wh=8.0),
    log("EMP0001", "2026-07-09", "PRESENT", wh=None, late=12),         # open record
    # EMP0002 (joined 07-13)
    log("EMP0002", "2026-07-14", "PRESENT", wh=8.98, late=20, legacy=True),
    log("EMP0002", "2026-07-15", "PRESENT", wh=4.0, half=True),
    log("EMP0002", "2026-07-16", "LEAVE"),
    log("EMP0002", "2026-07-18", "WFH", wh=5.0),                       # Saturday
    # Sales: EMP0004 and EMP0005 tie on 60 late minutes
    log("EMP0004", "2026-07-01", "PRESENT", wh=8.0, late=30),
    log("EMP0004", "2026-07-02", "PRESENT", wh=9.0, late=30),
    log("EMP0004", "2026-07-03", "ABSENT"),
    log("EMP0005", "2026-07-01", "PRESENT", wh=7.0, late=60),
    log("EMP0005", "2026-07-06", "PRESENT", wh=8.5),
    log("EMP0006", "2026-07-02", "PRESENT", wh=7.5, late=15),
    log("EMP0006", "2026-07-08", "ON_DUTY", wh=8.0),
    # Support, overnight shift
    log("EMP0007", "2026-07-06", "PRESENT", wh=8.75, ot=40),
    log("EMP0007", "2026-07-07", "PRESENT", wh=5.5, late=180, ot=30),
    log("EMP0007", "2026-07-08", "LEAVE"),
    # orphan: no such employee -> must be ignored everywhere
    log("EMP0099", "2026-07-01", "PRESENT", wh=8.0, late=500),
]


def load(db):
    db.employees.insert_many([dict(e) for e in EMPLOYEES])
    db.attendance_logs.insert_many([dict(l) for l in LOGS])
