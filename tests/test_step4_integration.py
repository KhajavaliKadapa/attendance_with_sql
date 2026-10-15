"""Punch-out and regularization (PATCH).   Run: pytest -m integration"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

import app.main as m
from tests.conftest import add_emp, ist_ms

pytestmark = pytest.mark.integration


def pin(client, code, t, status="PRESENT"):
    r = client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": t, "status": status})
    assert r.status_code == 201, r.text
    return r.json()


def pout(client, code, t):
    return client.post("/attendance/punch-out", json={"emp_code": code, "punched_at": t})


def patch(client, code, day, **body):
    body.setdefault("reason", "manual correction")
    body.setdefault("regularized_by", "hr.admin")
    return client.patch(f"/attendance/{code}/{day}", json=body)


# ------------------------------- punch-out -------------------------------- #
def test_punch_out_normal_day_matches_sample_1(client, database):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 9, 28))
    r = pout(client, "EMP0001", ist_ms(2026, 7, 6, 18, 35))
    assert r.status_code == 200
    b = r.json()
    assert b["work_hours"] == 9.12 and b["overtime_minutes"] == 0 and b["half_day"] is False
    assert b["punch_out"] == ist_ms(2026, 7, 6, 18, 35) and b["late_minutes"] == 0 and b["history"] == []
    assert isinstance(database.attendance_logs.find_one({})["punch_out"], datetime)


def test_punch_out_with_overtime_and_late_sample_2(client, database):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 10, 5))
    b = pout(client, "EMP0001", ist_ms(2026, 7, 6, 19, 10)).json()
    assert b["late_minutes"] == 35 and b["overtime_minutes"] == 40 and b["work_hours"] == 9.08


def test_punch_out_half_day_sample_4(client, database):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 7, 9, 30), "WFH")
    b = pout(client, "EMP0001", ist_ms(2026, 7, 7, 13, 0)).json()
    assert b["work_hours"] == 3.5 and b["half_day"] is True and b["status"] == "WFH"


def test_punch_out_across_midnight_sample_7(client, database):
    add_emp(database, "EMP0005", start="22:00", end="06:00")
    pin(client, "EMP0005", ist_ms(2026, 7, 6, 21, 55))
    b = pout(client, "EMP0005", ist_ms(2026, 7, 7, 6, 40)).json()
    assert b["date"] == "2026-07-06" and b["work_hours"] == 8.75 and b["overtime_minutes"] == 40 and b["late_minutes"] == 0


def test_punch_out_overnight_punched_in_after_midnight(client, database):
    add_emp(database, "EMP0005", start="22:00", end="06:00")
    assert pin(client, "EMP0005", ist_ms(2026, 7, 7, 1, 0))["date"] == "2026-07-06"
    b = pout(client, "EMP0005", ist_ms(2026, 7, 7, 6, 30)).json()
    assert b["overtime_minutes"] == 30 and b["work_hours"] == 5.5  # shift end is 06:00 on the 7th


def test_punch_out_picks_the_latest_open_record(client, database):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 9, 30))
    pout(client, "EMP0001", ist_ms(2026, 7, 6, 18, 30))
    pin(client, "EMP0001", ist_ms(2026, 7, 7, 9, 30))
    b = pout(client, "EMP0001", ist_ms(2026, 7, 7, 18, 30)).json()
    assert b["date"] == "2026-07-07"


def test_punch_out_exactly_24h_is_allowed(client, database):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 9, 30))
    r = pout(client, "EMP0001", ist_ms(2026, 7, 7, 9, 30))
    assert r.status_code == 200 and r.json()["work_hours"] == 24.0


def test_punch_out_errors(client, database):
    add_emp(database)
    assert pout(client, "EMP9999", ist_ms(2026, 7, 6, 18, 0)).status_code == 404  # unknown employee
    assert pout(client, "EMP0001", ist_ms(2026, 7, 6, 18, 0)).status_code == 404  # no punch-in at all
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 9, 30))
    assert pout(client, "EMP0001", ist_ms(2026, 7, 6, 9, 0)).status_code == 404  # before any punch-in
    assert pout(client, "EMP0001", ist_ms(2026, 7, 6, 9, 30)).status_code == 422  # equal to punch-in
    assert pout(client, "EMP0001", ist_ms(2026, 7, 7, 9, 30, 1)).status_code == 422  # > 24h
    assert pout(client, "EMP0001", 1783312500).status_code == 422  # seconds, not ms
    assert pout(client, "EMP0001", ist_ms(2026, 7, 6, 18, 30)).status_code == 200
    r = pout(client, "EMP0001", ist_ms(2026, 7, 6, 19, 0))
    assert r.status_code == 409 and "detail" in r.json()  # already closed


def test_punch_out_ignores_absent_records(client, database):
    add_emp(database)
    database.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-08", "status": "ABSENT",
                                         "punch_in": None, "punch_out": None})
    assert pout(client, "EMP0001", ist_ms(2026, 7, 8, 18, 0)).status_code == 404


def test_parallel_punch_outs_yield_one_200(client, database):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 9, 30))
    t = ist_ms(2026, 7, 6, 18, 30)
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: pout(client, "EMP0001", t).status_code, range(16)))
    assert codes.count(200) == 1 and codes.count(409) == 15


# --------------------------------- PATCH ---------------------------------- #
def seed_closed(client, database, code="EMP0001", day=(2026, 7, 7), pi=(10, 5), po=(18, 30)):
    add_emp(database, code)
    pin(client, code, ist_ms(*day, *pi))
    assert pout(client, code, ist_ms(*day, *po)).status_code == 200


def test_patch_punch_in_recomputes_and_audits_like_sample_8(client, database):
    seed_closed(client, database)
    r = patch(client, "EMP0001", "2026-07-07", punch_in=ist_ms(2026, 7, 7, 9, 28), reason="biometric glitch", regularized_by="hr.admin")
    assert r.status_code == 200
    b = r.json()
    assert b["late_minutes"] == 0 and b["work_hours"] == 9.03 and b["punch_in"] == ist_ms(2026, 7, 7, 9, 28)
    assert len(b["history"]) == 1
    h = b["history"][0]
    assert h["by"] == "hr.admin" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    # only fields that actually changed (overtime, half_day, status, punch_out did not)
    assert h["changes"] == {
        "punch_in": {"from": ist_ms(2026, 7, 7, 10, 5), "to": ist_ms(2026, 7, 7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    stored = database.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["punch_in"], datetime) and isinstance(stored["history"][0]["at"], datetime)
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)  # BSON dates, not ms


def test_patch_appends_and_never_overwrites_history(client, database):
    seed_closed(client, database)
    patch(client, "EMP0001", "2026-07-07", punch_in=ist_ms(2026, 7, 7, 9, 28))
    r = patch(client, "EMP0001", "2026-07-07", punch_out=ist_ms(2026, 7, 7, 19, 10), reason="second fix")
    b = r.json()
    assert len(b["history"]) == 2 and b["history"][1]["reason"] == "second fix"
    assert b["history"][0]["changes"]["punch_in"]["to"] == ist_ms(2026, 7, 7, 9, 28)  # first entry untouched
    assert b["overtime_minutes"] == 40 and set(b["history"][1]["changes"]) == {"punch_out", "work_hours", "overtime_minutes"}


def test_patch_status_absent_clears_punches(client, database):
    seed_closed(client, database)
    b = patch(client, "EMP0001", "2026-07-07", status="ABSENT").json()
    assert (b["status"], b["punch_in"], b["punch_out"], b["work_hours"]) == ("ABSENT", None, None, None)
    assert (b["late_minutes"], b["overtime_minutes"], b["half_day"]) == (0, 0, False)
    assert set(b["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}


def test_patch_absent_to_present_with_punch_in(client, database):
    add_emp(database)
    database.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-08", "status": "ABSENT",
                                         "punch_in": None, "punch_out": None, "work_hours": None,
                                         "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []})
    b = patch(client, "EMP0001", "2026-07-08", status="PRESENT", punch_in=ist_ms(2026, 7, 8, 10, 0)).json()
    assert b["status"] == "PRESENT" and b["late_minutes"] == 30 and b["work_hours"] is None
    # punch_out afterwards works through the normal endpoint
    assert pout(client, "EMP0001", ist_ms(2026, 7, 8, 18, 30)).status_code == 200


def test_patch_open_record_gets_punch_out_and_half_day(client, database):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 9, 30))
    b = patch(client, "EMP0001", "2026-07-06", punch_out=ist_ms(2026, 7, 6, 13, 0)).json()
    assert b["work_hours"] == 3.5 and b["half_day"] is True
    assert b["history"][0]["changes"]["punch_out"] == {"from": None, "to": ist_ms(2026, 7, 6, 13, 0)}
    assert b["history"][0]["changes"]["half_day"] == {"from": False, "to": True}


def test_patch_legacy_record_without_history_field(client, database):
    add_emp(database)
    database.attendance_logs.insert_one({
        "emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=m.UTC), "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=m.UTC),
        "work_hours": 8.98, "late_minutes": 0})  # no history, no half_day, no overtime
    b = patch(client, "EMP0001", "2026-07-14", status="WFH").json()
    assert b["status"] == "WFH" and len(b["history"]) == 1 and b["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}
    assert len(patch(client, "EMP0001", "2026-07-14", status="ON_DUTY").json()["history"]) == 2


def test_patch_ignores_derived_fields_from_client(client, database):
    seed_closed(client, database)
    b = patch(client, "EMP0001", "2026-07-07", punch_in=ist_ms(2026, 7, 7, 9, 28), work_hours=99, late_minutes=77, half_day=True).json()
    assert b["work_hours"] == 9.03 and b["late_minutes"] == 0 and b["half_day"] is False


def test_patch_overnight_record(client, database):
    add_emp(database, "EMP0005", start="22:00", end="06:00")
    pin(client, "EMP0005", ist_ms(2026, 7, 6, 21, 55))
    pout(client, "EMP0005", ist_ms(2026, 7, 7, 6, 40))
    b = patch(client, "EMP0005", "2026-07-06", punch_in=ist_ms(2026, 7, 6, 22, 30)).json()
    assert b["late_minutes"] == 30 and b["overtime_minutes"] == 40
    # an instant that belongs to the NEXT shift (07-07 22:30) is not allowed on this record
    assert patch(client, "EMP0005", "2026-07-06", punch_in=ist_ms(2026, 7, 7, 22, 30)).status_code == 422


def test_patch_404s(client, database):
    seed_closed(client, database)
    assert patch(client, "EMP9999", "2026-07-07", status="LEAVE").status_code == 404
    assert patch(client, "EMP0001", "2026-07-09", status="LEAVE").status_code == 404
    add_emp(database, "EMP0002")
    assert patch(client, "EMP0002", "2026-07-07", status="LEAVE").status_code == 404  # other employee's day


@pytest.mark.parametrize("body", [
    {"status": "ABSENT", "punch_in": ist_ms(2026, 7, 7, 9, 30)},    # punch time with ABSENT
    {"status": "LEAVE", "punch_out": ist_ms(2026, 7, 7, 18, 30)},   # punch time with LEAVE
    {"punch_in": ist_ms(2026, 7, 8, 9, 30)},                          # different attendance date
    {"punch_out": ist_ms(2026, 7, 7, 10, 5)},                         # not after punch_in
    {"punch_out": ist_ms(2026, 7, 8, 10, 6)},                         # more than 24h after punch_in
    {"punch_in": ist_ms(2026, 7, 7, 19, 0)},                          # now after the stored punch_out
    {"status": "PRESENT"},                                            # changes nothing
    {"punch_in": ist_ms(2026, 7, 7, 10, 5)},                          # same value: changes nothing
    {"reason": "abc"},                                                # reason too short
    {"punch_in": 1783312500},                                         # seconds
    {"punch_in": 1783312500000.5},                                    # float
])
def test_patch_validation_is_422(client, database, body):
    seed_closed(client, database)
    assert patch(client, "EMP0001", "2026-07-07", **body).status_code == 422
    assert database.attendance_logs.find_one({"emp_code": "EMP0001"})["history"] == []  # nothing written


def test_patch_missing_required_and_bad_path(client, database):
    seed_closed(client, database)
    assert client.patch("/attendance/EMP0001/2026-07-07", json={"status": "LEAVE"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-07", json={"status": "LEAVE", "reason": "valid reason"}).status_code == 422
    assert patch(client, "EMP0001", "2026-13-01", status="LEAVE").status_code == 422
    assert patch(client, "EMP0001", "07-07-2026", status="LEAVE").status_code == 422


def test_patch_absent_record_needs_punch_in_for_presence(client, database):
    add_emp(database)
    database.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-08", "status": "LEAVE", "punch_in": None, "punch_out": None})
    assert patch(client, "EMP0001", "2026-07-08", status="PRESENT").status_code == 422
    assert patch(client, "EMP0001", "2026-07-08", punch_out=ist_ms(2026, 7, 8, 18, 0)).status_code == 422  # still LEAVE


def test_parallel_patches_never_lose_history(client, database):
    seed_closed(client, database)
    times = [ist_ms(2026, 7, 7, 9, 0 + i) for i in range(12)]  # 12 different punch_in values
    def go(t):
        return patch(client, "EMP0001", "2026-07-07", punch_in=t, reason=f"fix {t}").status_code
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(go, times))
    ok = codes.count(200)
    assert ok >= 1 and set(codes) <= {200, 409}
    stored = database.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == ok  # one entry per success, none lost, none duplicated
    # the history forms an unbroken chain: each entry's "from" is the previous entry's "to"
    chain = [h["changes"]["punch_in"] for h in stored["history"]]
    for prev, nxt in zip(chain, chain[1:]):
        assert nxt["from"] == prev["to"]


# ------------- deterministic conflict tests (do not rely on thread timing) ------------- #
# A hook runs AFTER the endpoint has read the record and BEFORE it writes. It plays the
# part of "another request won the race", which is the interleaving a real race produces.
def test_patch_loses_to_a_concurrent_patch_with_409(client, database, monkeypatch):
    seed_closed(client, database)
    real_now = m.now_ms_precision

    def other_patch_lands_first():
        database.attendance_logs.update_one(
            {"emp_code": "EMP0001"},
            {"$push": {"history": {"at": datetime(2026, 7, 8, tzinfo=m.UTC), "by": "someone.else",
                                   "reason": "won the race", "changes": {}}}})
        return real_now()

    monkeypatch.setattr(m, "now_ms_precision", other_patch_lands_first)
    r = patch(client, "EMP0001", "2026-07-07", punch_in=ist_ms(2026, 7, 7, 9, 28))
    assert r.status_code == 409
    stored = database.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert [h["by"] for h in stored["history"]] == ["someone.else"]  # the winner's entry is intact
    assert stored["punch_in"] == m.ms_to_dt(ist_ms(2026, 7, 7, 10, 5))  # the loser changed nothing


def test_patch_loses_to_a_concurrent_punch_out_with_409(client, database, monkeypatch):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 9, 30))
    real_now = m.now_ms_precision

    def punch_out_lands_first():
        database.attendance_logs.update_one({"emp_code": "EMP0001"}, {"$set": {"punch_out": m.ms_to_dt(ist_ms(2026, 7, 6, 18, 0))}})
        return real_now()

    monkeypatch.setattr(m, "now_ms_precision", punch_out_lands_first)
    assert patch(client, "EMP0001", "2026-07-06", status="WFH").status_code == 409
    stored = database.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert stored["status"] == "PRESENT" and stored["history"] == []


def test_punch_out_loses_to_a_concurrent_punch_out_with_409(client, database, monkeypatch):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 9, 30))
    real = m.compute_half_day

    def other_punch_out_lands_first(a, b):
        database.attendance_logs.update_one({"emp_code": "EMP0001"}, {"$set": {"punch_out": m.ms_to_dt(ist_ms(2026, 7, 6, 17, 0)), "work_hours": 7.5}})
        return real(a, b)

    monkeypatch.setattr(m, "compute_half_day", other_punch_out_lands_first)
    assert pout(client, "EMP0001", ist_ms(2026, 7, 6, 18, 30)).status_code == 409
    assert database.attendance_logs.find_one({"emp_code": "EMP0001"})["work_hours"] == 7.5  # winner's values kept


def test_punch_out_loses_to_a_concurrent_punch_in_correction_with_409(client, database, monkeypatch):
    add_emp(database)
    pin(client, "EMP0001", ist_ms(2026, 7, 6, 9, 30))
    real = m.compute_half_day

    def punch_in_corrected_first(a, b):
        database.attendance_logs.update_one({"emp_code": "EMP0001"}, {"$set": {"punch_in": m.ms_to_dt(ist_ms(2026, 7, 6, 9, 0))}})
        return real(a, b)

    monkeypatch.setattr(m, "compute_half_day", punch_in_corrected_first)
    assert pout(client, "EMP0001", ist_ms(2026, 7, 6, 18, 30)).status_code == 409  # computed from a stale punch_in
    assert database.attendance_logs.find_one({"emp_code": "EMP0001"})["punch_out"] is None
