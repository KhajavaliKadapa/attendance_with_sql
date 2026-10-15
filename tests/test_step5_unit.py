"""Analytics, no database: calendar helpers, rounding, pipeline structure, and the oracle
checked against numbers worked out by hand.   Run: pytest tests/test_step5_unit.py"""
from datetime import date

import bson
import pytest

import app.main as m
from tests import oracle
from tests.dataset import EMPLOYEES, LOGS


# ------------------------------ calendar helpers --------------------------- #
@pytest.mark.parametrize("start,end,expected", [
    ("2026-07-01", "2026-07-31", 23),   # Wed 1st .. Fri 31st
    ("2026-08-03", "2026-08-31", 21),   # Mon 3rd .. Mon 31st
    ("2026-07-04", "2026-07-05", 0),    # Sat-Sun
    ("2026-07-06", "2026-07-06", 1),
    ("2026-07-03", "2026-07-06", 2),    # Fri + Mon
    ("2026-07-06", "2026-07-03", 0),    # end before start
    ("2026-02-01", "2026-02-28", 20),
])
def test_count_weekdays(start, end, expected):
    assert m.count_weekdays(date.fromisoformat(start), date.fromisoformat(end)) == expected


def test_count_weekdays_matches_brute_force_for_every_window_in_a_month():
    from datetime import timedelta
    days = [date(2026, 7, 1) + timedelta(days=i) for i in range(31)]
    for i, a in enumerate(days):
        for b in days[i:]:
            brute = sum(1 for d in days if a <= d <= b and d.weekday() < 5)
            assert m.count_weekdays(a, b) == brute


@pytest.mark.parametrize("month,bounds", [
    ("2026-07", ("2026-07-01", "2026-07-31")), ("2026-02", ("2026-02-01", "2026-02-28")),
    ("2028-02", ("2028-02-01", "2028-02-29")), ("2026-12", ("2026-12-01", "2026-12-31")),
])
def test_month_bounds(month, bounds):
    assert m.month_bounds(month) == bounds


def test_working_days_from_joined_on():
    assert m.working_days("2026-07", "2026-01-05") == 23   # joined earlier
    assert m.working_days("2026-07", "2026-07-13") == 15   # mid-month: 13-17, 20-24, 27-31
    assert m.working_days("2026-07", "2026-07-01") == 23   # joined on the 1st
    assert m.working_days("2026-07", "2026-07-31") == 1    # joined on the last day (a Friday)
    assert m.working_days("2026-07", "2026-08-03") == 0    # joins after the month


# --------------------------------- rounding -------------------------------- #
def test_round_half_up_survives_float_noise():
    assert (8.00 + 8.01) / 2 == 8.004999999999999           # the hazard
    assert m.round_half_up((8.00 + 8.01) / 2, 2) == 8.01    # exact tie, rounded up
    assert m.round_half_up(0.1 + 0.2, 2) == 0.3
    assert m.round_half_up(53.56 / 8, 2) == 6.7             # 6.695
    assert m.round_half_up(1 / 32, 4) == 0.0313             # 0.03125 exactly
    assert m.round_half_up(1 / 3, 4) == 0.3333
    assert m.round_half_up(4.5 / 23 * 100, 2) == 19.57


# ---------------------- pipelines: structure, not semantics ---------------- #
def _all_pipelines():
    return {
        "monthly": m.employee_monthly_pipeline("EMP0001", "2026-07-01", "2026-07-31"),
        "summary": m.department_summary_pipeline("2026-07-01", "2026-07-31"),
        "summary_dept": m.department_summary_pipeline("2026-07-01", "2026-07-31", "Sales"),
        "leaderboard": m.late_leaderboard_pipeline("2026-07-01", "2026-07-31", 10),
        "leaderboard_dept": m.late_leaderboard_pipeline("2026-07-01", "2026-07-31", 10, "Sales"),
        "trend": m.department_trend_pipeline("Engineering", "2026-07-01", "2026-07-14"),
    }


def _keys(node):
    """Every dict key in a pipeline (operators like $match, $group, $cond ...)."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


# Operators the pipelines are allowed to use; a typo such as "$isoDayOfweek" fails here.
KNOWN = {
    "$match", "$group", "$sort", "$lookup", "$unwind", "$project", "$replaceRoot", "$setWindowFields",
    "$expr", "$and", "$eq", "$gt", "$gte", "$lte", "$in", "$cond", "$ifNull", "$sum", "$avg", "$push",
    "$isNumber", "$isoDayOfWeek", "$dateFromString", "$dateAdd", "$dateToString", "$range", "$map",
    "$filter", "$arrayElemAt", "$reduce", "$add", "$divide", "$let", "$rank",
}


@pytest.mark.parametrize("name", list(_all_pipelines()))
def test_pipeline_is_bson_encodable_and_uses_only_known_operators(name):
    pipeline = _all_pipelines()[name]
    bson.encode({"pipeline": pipeline})  # raises on anything BSON cannot represent
    used = {k for k in _keys(pipeline) if k.startswith("$")}
    assert used <= KNOWN, f"unknown operators: {used - KNOWN}"


def test_no_dollar_round_anywhere():
    # MongoDB's $round rounds half to even; the contract wants half up (done in Python).
    for name, p in _all_pipelines().items():
        assert "$round" not in set(_keys(p)), name


def test_first_stage_is_an_indexable_match():
    p = _all_pipelines()
    assert list(p["monthly"][0]["$match"]) == ["emp_code", "date"]          # (emp_code, date) index
    assert list(p["leaderboard"][0]["$match"]) == ["date"]                   # (date, emp_code) index
    assert list(p["summary_dept"][0]["$match"]) == ["joined_on", "department"]
    assert p["trend"][0]["$match"] == {"department": "Engineering"}          # (department, ...) index


def test_leaderboard_ranks_then_limits_on_rank():
    p = _all_pipelines()["leaderboard"]
    stages = [next(iter(s)) for s in p]
    assert "$limit" not in stages                                           # a $limit would cut ties
    assert stages.index("$setWindowFields") < stages.index("$match", stages.index("$setWindowFields"))
    assert p[stages.index("$setWindowFields")]["$setWindowFields"]["output"]["rank"] == {"$rank": {}}
    assert {"$match": {"rank": {"$lte": 10}}} in p


def test_leaderboard_department_filter_comes_before_ranking():
    p = _all_pipelines()["leaderboard_dept"]
    stages = [next(iter(s)) for s in p]
    dept_filter = p.index({"$match": {"e.department": "Sales"}})
    assert dept_filter < stages.index("$setWindowFields")


def test_leaderboard_drops_unknown_employees_via_unwind():
    p = _all_pipelines()["leaderboard"]
    assert {"$unwind": "$e"} in p  # no preserveNullAndEmptyArrays -> orphans disappear


def test_summary_starts_from_employees_so_zero_log_employees_are_counted():
    p = _all_pipelines()["summary"]
    lookup = p[1]["$lookup"]
    assert lookup["from"] == "attendance_logs" and "$unwind" not in [next(iter(s)) for s in p]
    assert p[2]["$group"]["headcount"] == {"$sum": 1}


def test_trend_generates_one_entry_per_calendar_day():
    p = _all_pipelines()["trend"]
    project = next(s for s in p if "$project" in s and "days" in s["$project"])
    assert project["$project"]["days"]["$map"]["input"] == {"$range": [0, 14]}   # 07-01 .. 07-14
    assert "$limit" not in [next(iter(s)) for s in p]
    window = next(s for s in p if "$setWindowFields" in s)["$setWindowFields"]
    assert window["output"]["moving_avg_7d"]["window"] == {"documents": [-6, 0]}  # current + 6 before


# ------------- the oracle against numbers computed by hand (see dataset.py) ------------- #
def test_oracle_employee_monthly_by_hand():
    om = lambda c, mo="2026-07": oracle.employee_monthly(EMPLOYEES, LOGS, c, mo)
    assert om("EMP0001") == dict(emp_code="EMP0001", month="2026-07", working_days=23, present_days=4.5, leave_days=1,
                                 late_count=3, total_late_minutes=62, total_overtime_minutes=100, attendance_pct=19.57)
    assert om("EMP0002") == dict(emp_code="EMP0002", month="2026-07", working_days=15, present_days=1.5, leave_days=1,
                                 late_count=1, total_late_minutes=20, total_overtime_minutes=0, attendance_pct=10.0)
    assert om("EMP0003")["attendance_pct"] == 0.0 and om("EMP0003")["working_days"] == 23
    assert om("EMP0007") == dict(emp_code="EMP0007", month="2026-07", working_days=23, present_days=2.0, leave_days=1,
                                 late_count=1, total_late_minutes=180, total_overtime_minutes=70, attendance_pct=8.7)
    assert om("EMP0008")["working_days"] == 0 and om("EMP0008")["attendance_pct"] is None
    assert om("EMP0008", "2026-08")["working_days"] == 21


def test_oracle_department_summary_by_hand():
    s = oracle.department_summary(EMPLOYEES, LOGS, "2026-07")
    assert [i["department"] for i in s["items"]] == ["Engineering", "Sales", "Support"]  # Legal joins in August
    eng, sales, sup = s["items"]
    assert eng == dict(department="Engineering", headcount=3, present_days=6.0, avg_work_hours=6.7,  # 53.56/8 = 6.695 tie
                       late_count=4, total_late_minutes=82, leave_count=2, on_duty_count=1)
    assert sales == dict(department="Sales", headcount=3, present_days=6.0, avg_work_hours=8.0,
                         late_count=4, total_late_minutes=135, leave_count=0, on_duty_count=1)
    assert sup == dict(department="Support", headcount=1, present_days=2.0, avg_work_hours=7.13,  # 7.125 tie
                       late_count=1, total_late_minutes=180, leave_count=1, on_duty_count=0)


def test_oracle_department_summary_headcount_rules():
    aug = {i["department"]: i for i in oracle.department_summary(EMPLOYEES, LOGS, "2026-08")["items"]}
    assert aug["Legal"] == dict(department="Legal", headcount=1, present_days=0.0, avg_work_hours=None,
                                late_count=0, total_late_minutes=0, leave_count=0, on_duty_count=0)
    assert aug["Engineering"]["headcount"] == 3 and aug["Engineering"]["avg_work_hours"] is None
    feb = {i["department"]: i["headcount"] for i in oracle.department_summary(EMPLOYEES, LOGS, "2026-02")["items"]}
    assert feb == {"Engineering": 2, "Sales": 3}  # EMP0002 and Support join later


def test_oracle_leaderboard_by_hand():
    lb = lambda **kw: oracle.late_leaderboard(EMPLOYEES, LOGS, "2026-07", **kw)["items"]
    full = lb(limit=10)
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"], r["late_count"]) for r in full] == [
        (1, "EMP0007", 180, 1), (2, "EMP0001", 62, 3), (3, "EMP0004", 60, 2), (3, "EMP0005", 60, 1),
        (5, "EMP0002", 20, 1), (6, "EMP0006", 15, 1)]          # tie -> 3,3 then 5; orphan EMP0099 absent
    assert [r["emp_code"] for r in lb(limit=3)] == ["EMP0007", "EMP0001", "EMP0004", "EMP0005"]  # tie at the cutoff kept
    assert [r["emp_code"] for r in lb(limit=2)] == ["EMP0007", "EMP0001"]
    assert [r["emp_code"] for r in lb(limit=5)][-1] == "EMP0002"
    sales = lb(limit=10, department="Sales")
    assert [(r["rank"], r["emp_code"]) for r in sales] == [(1, "EMP0004"), (1, "EMP0005"), (3, "EMP0006")]
    assert oracle.late_leaderboard(EMPLOYEES, LOGS, "2026-09")["items"] == []


def test_oracle_trend_by_hand():
    t = oracle.department_trend(EMPLOYEES, LOGS, "Engineering", "2026-07-01", "2026-07-14")["items"]
    assert [r["date"] for r in t] == [f"2026-07-{d:02d}" for d in range(1, 15)]           # one row per day
    assert [r["headcount"] for r in t] == [2] * 12 + [3, 3]                                 # EMP0002 joins on the 13th
    assert [r["present_count"] for r in t] == [1, 1, 0.5, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0, 1]  # weekend 07-04 counted
    assert [r["late_count"] for r in t] == [0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    assert [r["is_working_day"] for r in t] == [True, True, True, False, False, True, True, True, True, True, False, False, True, True]
    assert [r["attendance_rate"] for r in t] == [0.5, 0.5, 0.25, None, None, 0.0, 0.0, 0.5, 0.5, 0.0, None, None, 0.0, 0.3333]
    assert [r["moving_avg_7d"] for r in t] == [0.5, 0.5, 0.4167, 0.4167, 0.4167, 0.3125, 0.25, 0.25, 0.25, 0.2, 0.2, 0.2, 0.2, 0.2667]


def test_oracle_trend_edge_cases():
    weekend = oracle.department_trend(EMPLOYEES, LOGS, "Engineering", "2026-07-04", "2026-07-05")["items"]
    assert [r["moving_avg_7d"] for r in weekend] == [None, None]                      # all-null window -> null
    legal = oracle.department_trend(EMPLOYEES, LOGS, "Legal", "2026-07-30", "2026-08-04")["items"]
    assert [(r["headcount"], r["attendance_rate"]) for r in legal] == [   # Thu, Fri, Sat, Sun, Mon, Tue
        (0, None), (0, None), (0, None), (0, None), (1, 0.0), (1, 0.0)]
    assert [r["moving_avg_7d"] for r in legal] == [None, None, None, None, 0.0, 0.0]
    empty = oracle.department_trend(EMPLOYEES, LOGS, "Sales", "2026-09-01", "2026-09-03")["items"]
    assert [(r["present_count"], r["attendance_rate"], r["moving_avg_7d"]) for r in empty] == [(0.0, 0.0, 0.0)] * 3
