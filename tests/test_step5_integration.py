"""Analytics endpoints against a database seeded with tests/dataset.py.
Run against a real server:   MONGO_URI=mongodb://localhost:27017 pytest -m integration
Every endpoint answer is compared with tests/oracle.py (plain Python), and key answers with
numbers worked out by hand."""
import pytest

from tests import oracle
from tests.dataset import EMPLOYEES, LOGS, load

pytestmark = [pytest.mark.integration, pytest.mark.real_mongo]


@pytest.fixture
def seeded(client, database):
    load(database)
    return client


# ------------------------------ employee monthly --------------------------- #
@pytest.mark.parametrize("month", ["2026-07", "2026-08", "2026-02", "2026-06"])
@pytest.mark.parametrize("code", [e["emp_code"] for e in EMPLOYEES])
def test_employee_monthly_matches_oracle(seeded, code, month):
    r = seeded.get(f"/analytics/employees/{code}/monthly", params={"month": month})
    assert r.status_code == 200
    assert r.json() == oracle.employee_monthly(EMPLOYEES, LOGS, code, month)


def test_employee_monthly_hand_values(seeded):
    b = seeded.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert b == dict(emp_code="EMP0001", month="2026-07", working_days=23, present_days=4.5, leave_days=1,
                     late_count=3, total_late_minutes=62, total_overtime_minutes=100, attendance_pct=19.57)
    b = seeded.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert b["working_days"] == 15 and b["present_days"] == 1.5 and b["attendance_pct"] == 10.0   # mid-month joiner
    b = seeded.get("/analytics/employees/EMP0008/monthly", params={"month": "2026-07"}).json()
    assert b["working_days"] == 0 and b["attendance_pct"] is None                                  # not joined yet
    b = seeded.get("/analytics/employees/EMP0003/monthly", params={"month": "2026-07"}).json()
    assert b["present_days"] == 0 and b["attendance_pct"] == 0.0                                   # no logs at all


def test_employee_monthly_errors(seeded):
    assert seeded.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    for bad in ["2026-13", "2026-7", "26-07", "2026-00", "0000-01", "july", ""]:
        assert seeded.get("/analytics/employees/EMP0001/monthly", params={"month": bad}).status_code == 422, bad
    assert seeded.get("/analytics/employees/EMP0001/monthly").status_code == 422


# ----------------------------- department summary -------------------------- #
@pytest.mark.parametrize("month", ["2026-07", "2026-08", "2026-02", "2026-01", "2026-12"])
@pytest.mark.parametrize("department", [None, "Engineering", "Sales", "Legal", "Nope"])
def test_department_summary_matches_oracle(seeded, month, department):
    params = {"month": month, **({"department": department} if department else {})}
    r = seeded.get("/analytics/departments/summary", params=params)
    assert r.status_code == 200
    assert r.json() == oracle.department_summary(EMPLOYEES, LOGS, month, department)


def test_department_summary_hand_values(seeded):
    items = seeded.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert [i["department"] for i in items] == ["Engineering", "Sales", "Support"]   # sorted; Legal not yet joined
    assert items[0] == dict(department="Engineering", headcount=3, present_days=6.0, avg_work_hours=6.7,
                            late_count=4, total_late_minutes=82, leave_count=2, on_duty_count=1)
    assert items[2]["avg_work_hours"] == 7.13                                         # 7.125 rounds up
    aug = {i["department"]: i for i in seeded.get("/analytics/departments/summary", params={"month": "2026-08"}).json()["items"]}
    assert aug["Legal"]["headcount"] == 1 and aug["Legal"]["avg_work_hours"] is None  # headcount without any log
    assert aug["Engineering"]["headcount"] == 3                                       # includes EMP0003 (no logs ever)


def test_department_summary_errors(seeded):
    assert seeded.get("/analytics/departments/summary").status_code == 422
    assert seeded.get("/analytics/departments/summary", params={"month": "2026-13"}).status_code == 422


# ------------------------------ late leaderboard --------------------------- #
@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 6, 10, 50])
@pytest.mark.parametrize("department", [None, "Sales", "Engineering", "Support", "Nope"])
def test_leaderboard_matches_oracle(seeded, limit, department):
    params = {"month": "2026-07", "limit": limit, **({"department": department} if department else {})}
    r = seeded.get("/analytics/leaderboard/late", params=params)
    assert r.status_code == 200
    assert r.json() == oracle.late_leaderboard(EMPLOYEES, LOGS, "2026-07", limit, department)


def test_leaderboard_ties_and_orphans(seeded):
    rows = seeded.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"]
    assert [(r["rank"], r["emp_code"]) for r in rows] == [
        (1, "EMP0007"), (2, "EMP0001"), (3, "EMP0004"), (3, "EMP0005"), (5, "EMP0002"), (6, "EMP0006")]
    assert "EMP0099" not in {r["emp_code"] for r in rows}                  # orphan log ignored
    top3 = seeded.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()["items"]
    assert [r["emp_code"] for r in top3] == ["EMP0007", "EMP0001", "EMP0004", "EMP0005"]   # tie at cutoff keeps both
    assert seeded.get("/analytics/leaderboard/late", params={"month": "2026-09"}).json()["items"] == []


def test_leaderboard_errors(seeded):
    for bad in [0, 51, -1, "x"]:
        assert seeded.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": bad}).status_code == 422
    assert seeded.get("/analytics/leaderboard/late").status_code == 422


# ------------------------------- department trend -------------------------- #
TREND_CASES = [
    ("Engineering", "2026-07-01", "2026-07-14"),
    ("Engineering", "2026-07-04", "2026-07-05"),   # weekend only: everything null
    ("Engineering", "2026-07-13", "2026-07-13"),   # a single day
    ("Legal", "2026-07-30", "2026-08-04"),         # headcount 0 -> 1
    ("Sales", "2026-09-01", "2026-09-03"),         # no logs at all in range
    ("Support", "2026-07-05", "2026-07-12"),       # overnight-shift employee
    ("Engineering", "2026-06-01", "2026-08-31"),   # exactly 92 days
]


@pytest.mark.parametrize("department,d_from,d_to", TREND_CASES)
def test_trend_matches_oracle(seeded, department, d_from, d_to):
    r = seeded.get(f"/analytics/departments/{department}/trend", params={"from": d_from, "to": d_to})
    assert r.status_code == 200
    assert r.json() == oracle.department_trend(EMPLOYEES, LOGS, department, d_from, d_to)


def test_trend_hand_values(seeded):
    items = seeded.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-14"}).json()["items"]
    assert len(items) == 14 and items[0]["date"] == "2026-07-01" and items[-1]["date"] == "2026-07-14"
    assert [r["attendance_rate"] for r in items] == [0.5, 0.5, 0.25, None, None, 0.0, 0.0, 0.5, 0.5, 0.0, None, None, 0.0, 0.3333]
    assert [r["moving_avg_7d"] for r in items] == [0.5, 0.5, 0.4167, 0.4167, 0.4167, 0.3125, 0.25, 0.25, 0.25, 0.2, 0.2, 0.2, 0.2, 0.2667]
    assert items[3]["present_count"] == 1.0 and items[3]["is_working_day"] is False   # weekend record counted, rate null
    assert items[12]["headcount"] == 3 and items[11]["headcount"] == 2                  # EMP0002 joins on the 13th


def test_trend_has_one_row_for_every_day_even_with_no_data(seeded):
    items = seeded.get("/analytics/departments/Sales/trend", params={"from": "2026-12-28", "to": "2027-01-03"}).json()["items"]
    assert [i["date"] for i in items] == ["2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02", "2027-01-03"]


def test_trend_errors(seeded):
    url = "/analytics/departments/Engineering/trend"
    assert seeded.get(url, params={"from": "2026-07-14", "to": "2026-07-01"}).status_code == 422   # to < from
    assert seeded.get(url, params={"from": "2026-06-01", "to": "2026-09-01"}).status_code == 422   # 93 days
    assert seeded.get(url, params={"from": "2026-02-30", "to": "2026-03-01"}).status_code == 422
    assert seeded.get(url, params={"from": "2026-07-01"}).status_code == 422
    assert seeded.get(url).status_code == 422
    assert seeded.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert seeded.get("/analytics/departments/engineering/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404  # case-sensitive
