"""Unit tests: no database needed.   Run:  pytest tests/test_rules_unit.py"""
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.main import (
    IST,
    UTC,
    EmployeeCreate,
    PunchInRequest,
    RegularizeRequest,
    attendance_date,
    compute_half_day,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    dt_to_ms,
    ms_to_dt,
    round_half_up,
)


def ist(y, mo, d, h, mi, s=0):
    """An IST wall-clock time as a UTC-aware datetime (what the app uses internally)."""
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(UTC)


# ----------------------------- time helpers ------------------------------- #
def test_ms_roundtrip_and_truncation():
    ms = 1_783_312_500_900  # .900 of a second
    dt = ms_to_dt(ms)
    assert dt.microsecond == 0 and dt.tzinfo is not None
    assert dt_to_ms(dt) == 1_783_312_500_000  # truncated, never rounded up


def test_naive_datetime_from_mongo_is_treated_as_utc():
    naive = datetime(2026, 7, 6, 3, 58, 0)
    assert dt_to_ms(naive) == dt_to_ms(naive.replace(tzinfo=UTC))


# ------------------------------ R1: date ---------------------------------- #
def test_date_is_the_ist_date_not_the_utc_date():
    # 2026-07-06 19:00 UTC is 2026-07-07 00:30 IST
    p = datetime(2026, 7, 6, 19, 0, tzinfo=UTC)
    assert attendance_date(p, "09:30", "18:30") == "2026-07-07"


def test_overnight_punch_in_after_midnight_belongs_to_previous_day():
    assert attendance_date(ist(2026, 7, 7, 1, 10), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"


def test_overnight_boundary_is_strictly_earlier_than_shift_end():
    assert attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"


def test_normal_shift_early_morning_punch_stays_on_same_day():
    assert attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == "2026-07-07"


# ------------------------------ R2: late ---------------------------------- #
@pytest.mark.parametrize(
    "h,m,s,expected",
    [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 28, 0, 0), (10, 5, 0, 35), (8, 0, 0, 0)],
)
def test_late_examples_from_the_spec(h, m, s, expected):
    assert compute_late_minutes(ist(2026, 7, 6, h, m, s), "2026-07-06", "09:30") == expected


def test_late_overnight_shift():
    # punched in 01:00 IST on the 7th for the shift that started 22:00 on the 6th
    p = ist(2026, 7, 7, 1, 0)
    d = attendance_date(p, "22:00", "06:00")
    assert compute_late_minutes(p, d, "22:00") == 180
    # 21:55 is before the shift start -> not late
    assert compute_late_minutes(ist(2026, 7, 6, 21, 55), "2026-07-06", "22:00") == 0


# ---------------------------- R3: overtime -------------------------------- #
@pytest.mark.parametrize(
    "h,m,s,expected",
    [(18, 35, 0, 0), (19, 10, 0, 40), (19, 0, 0, 30), (18, 59, 59, 0), (17, 0, 0, 0), (19, 0, 59, 30)],
)
def test_overtime_normal_shift(h, m, s, expected):
    assert compute_overtime_minutes(ist(2026, 7, 6, h, m, s), "2026-07-06", "09:30", "18:30") == expected


def test_overtime_overnight_shift_end_is_next_day():
    # sample 7: out 06:40 IST on the 7th, shift 22:00-06:00 -> 40
    assert compute_overtime_minutes(ist(2026, 7, 7, 6, 40), "2026-07-06", "22:00", "06:00") == 40


# --------------------------- R4/R5: work hours ---------------------------- #
@pytest.mark.parametrize(
    "pi,po,expected",
    [  # the seeded samples in DATA_MODEL.md (instants in UTC)
        ((2026, 7, 6, 3, 58), (2026, 7, 6, 13, 5), 9.12),
        ((2026, 7, 6, 4, 35), (2026, 7, 6, 13, 40), 9.08),
        ((2026, 7, 7, 4, 0), (2026, 7, 7, 7, 30), 3.5),
        ((2026, 7, 6, 16, 25), (2026, 7, 7, 1, 10), 8.75),
        ((2026, 7, 7, 3, 58), (2026, 7, 7, 13, 0), 9.03),
        ((2026, 7, 14, 4, 2), (2026, 7, 14, 13, 1), 8.98),
    ],
)
def test_work_hours_match_the_sample_documents(pi, po, expected):
    assert compute_work_hours(datetime(*pi, tzinfo=UTC), datetime(*po, tzinfo=UTC)) == expected


def test_work_hours_rounds_half_up_not_half_even():
    t0 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
    # 54 s = 0.015 h exactly -> half-up gives 0.02 (Python's round(0.015, 2) gives 0.01)
    assert compute_work_hours(t0, t0.replace(second=54)) == 0.02


def test_half_day_uses_the_rounded_value():
    t0 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
    from datetime import timedelta

    # 4.495 h = 16182 s -> rounds to 4.50 -> NOT a half day
    assert compute_work_hours(t0, t0 + timedelta(seconds=16182)) == 4.5
    assert compute_half_day(t0, t0 + timedelta(seconds=16182)) is False
    # 4.485 h = 16146 s -> 4.49 -> half day
    assert compute_half_day(t0, t0 + timedelta(seconds=16146)) is True
    # sample 4: 3.5 h
    assert compute_half_day(t0, t0 + timedelta(hours=3, minutes=30)) is True


# ---------------------------- R8: reporting ------------------------------- #
@pytest.mark.parametrize(
    "value,places,expected",
    [(8.285, 2, 8.29), (2.675, 2, 2.68), (0.12345, 4, 0.1235), (0.5, 0, 1.0), (7.0, 2, 7.0)],
)
def test_round_half_up(value, places, expected):
    assert round_half_up(value, places) == expected


# ------------------------------ models ------------------------------------ #
GOOD_EMP = dict(emp_code="EMP0001", name="Asha", email="a@b.co", department="Eng", joined_on="2026-01-05")


def test_employee_defaults_and_valid():
    e = EmployeeCreate(**GOOD_EMP)
    assert (e.shift_start, e.shift_end) == ("09:30", "18:30")


@pytest.mark.parametrize(
    "patch",
    [
        {"emp_code": "E1"}, {"emp_code": "emp0001"}, {"emp_code": "EMP1234567"},
        {"email": "nope"}, {"name": ""}, {"department": ""},
        {"joined_on": "2026-02-30"}, {"joined_on": "20260105"}, {"joined_on": "2026-1-5"},
        {"shift_start": "9:30"}, {"shift_end": "24:00"},
        {"shift_start": "09:00", "shift_end": "09:00"},
    ],
)
def test_employee_rejects_bad_input(patch):
    with pytest.raises(ValidationError):
        EmployeeCreate(**{**GOOD_EMP, **patch})


@pytest.mark.parametrize("bad", [1783312500, 1783312500000.5, 1783312500000.0, "1783312500000", True, 99_999_999_999, 4_102_444_800_001, -5])
def test_punched_at_is_strict_epoch_millis(bad):
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", punched_at=bad)


def test_punch_in_accepts_valid_and_defaults():
    r = PunchInRequest(emp_code="EMP0001", punched_at=1_783_312_500_000)
    assert r.status == "PRESENT"
    assert PunchInRequest(emp_code="EMP0001").punched_at is None


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present", "HOLIDAY"])
def test_punch_in_rejects_non_presence_status(status):
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", status=status)


def test_regularize_ignores_derived_fields_and_validates_text():
    r = RegularizeRequest(reason="biometric glitch", regularized_by="hr", work_hours=99, half_day=True)
    assert not hasattr(r, "work_hours")
    with pytest.raises(ValidationError):
        RegularizeRequest(reason="abc", regularized_by="hr")  # reason < 5 chars
    with pytest.raises(ValidationError):
        RegularizeRequest(reason="long enough", regularized_by="")
