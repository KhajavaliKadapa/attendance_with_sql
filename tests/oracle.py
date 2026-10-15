"""Straightforward Python reference for the four analytics, written independently of the
pipelines (plain loops over plain dicts). Integration tests compare the endpoints with it;
unit tests compare it with hand-computed numbers. Same semantics as the contract:
R5-R9 and the ambiguity defaults agreed in the plan."""
from datetime import date, timedelta

from app.main import PRESENCE_STATUSES, month_bounds, round_half_up, working_days


def _weekday(day):
    return date.fromisoformat(day).weekday() < 5


def _weight(r):
    return (0.5 if r.get("half_day") is True else 1) if r["status"] in PRESENCE_STATUSES else 0


def _late(r):
    return r.get("late_minutes") or 0


def _in_month(logs, month):
    first, last = month_bounds(month)
    return [l for l in logs if first <= l["date"] <= last]


def employee_monthly(emps, logs, code, month):
    e = next(e for e in emps if e["emp_code"] == code)
    mine = [l for l in _in_month(logs, month) if l["emp_code"] == code]
    present = sum(_weight(l) for l in mine if _weekday(l["date"]))
    wd = working_days(month, e["joined_on"])
    return {
        "emp_code": code, "month": month, "working_days": wd,
        "present_days": round_half_up(present, 2),
        "leave_days": sum(1 for l in mine if l["status"] == "LEAVE"),
        "late_count": sum(1 for l in mine if _late(l) > 0),
        "total_late_minutes": sum(_late(l) for l in mine),
        "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in mine),
        "attendance_pct": round_half_up(present / wd * 100, 2) if wd else None,
    }


def department_summary(emps, logs, month, department=None):
    _, last = month_bounds(month)
    by_dept = {}
    for e in emps:
        if e["joined_on"] <= last and (department is None or e["department"] == department):
            by_dept.setdefault(e["department"], []).append(e["emp_code"])
    items = []
    for dept in sorted(by_dept):
        codes = set(by_dept[dept])
        rs = [l for l in _in_month(logs, month) if l["emp_code"] in codes]
        hours = [l["work_hours"] for l in rs if l["status"] in PRESENCE_STATUSES and l.get("work_hours") is not None]
        items.append({
            "department": dept, "headcount": len(codes),
            "present_days": round_half_up(sum(_weight(l) for l in rs if _weekday(l["date"])), 2),
            "avg_work_hours": round_half_up(sum(hours) / len(hours), 2) if hours else None,
            "late_count": sum(1 for l in rs if _late(l) > 0),
            "total_late_minutes": sum(_late(l) for l in rs),
            "leave_count": sum(1 for l in rs if l["status"] == "LEAVE"),
            "on_duty_count": sum(1 for l in rs if l["status"] == "ON_DUTY"),
        })
    return {"month": month, "items": items}


def late_leaderboard(emps, logs, month, limit=10, department=None):
    known = {e["emp_code"]: e for e in emps}
    totals = {}
    for l in _in_month(logs, month):
        t = totals.setdefault(l["emp_code"], [0, 0])
        t[0] += _late(l)
        t[1] += 1 if _late(l) > 0 else 0
    rows = [(c, t) for c, t in totals.items()
            if t[0] > 0 and c in known and (department is None or known[c]["department"] == department)]
    rows.sort(key=lambda x: (-x[1][0], x[0]))
    items = []
    for c, t in rows:
        rank = 1 + sum(1 for _, o in rows if o[0] > t[0])  # standard competition rank
        if rank <= limit:
            items.append({"rank": rank, "emp_code": c, "name": known[c]["name"], "department": known[c]["department"],
                          "total_late_minutes": t[0], "late_count": t[1]})
    return {"month": month, "items": items}


def department_trend(emps, logs, department, d_from, d_to):
    members = {e["emp_code"]: e for e in emps if e["department"] == department}
    start, end = date.fromisoformat(d_from), date.fromisoformat(d_to)
    rows, rates = [], []
    day = start
    while day <= end:
        ds = day.isoformat()
        hc = sum(1 for e in members.values() if e["joined_on"] <= ds)
        todays = [l for l in logs if l["date"] == ds and l["emp_code"] in members]
        present = sum(_weight(l) for l in todays)
        wd = day.weekday() < 5
        rate = present / hc if (wd and hc > 0) else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append({
            "date": ds, "is_working_day": wd, "headcount": hc, "present_count": float(present),
            "late_count": sum(1 for l in todays if _late(l) > 0),
            "attendance_rate": None if rate is None else round_half_up(rate, 4),
            "moving_avg_7d": round_half_up(sum(window) / len(window), 4) if window else None,
        })
        day += timedelta(days=1)
    return {"department": department, "items": rows}
