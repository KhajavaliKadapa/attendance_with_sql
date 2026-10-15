"""Employees, punch-in and attendance listing.   Run: pytest -m integration"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

import app.main as m
from tests.conftest import add_emp, ist_ms

pytestmark = pytest.mark.integration

EMP = dict(emp_code="EMP0001", name="Asha Rao", email="asha@example.com", department="Engineering",
           joined_on="2026-01-05")
RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes",
               "overtime_minutes", "half_day", "history"}


# ------------------------------- indexes ---------------------------------- #
def test_indexes_are_created_and_idempotent(database):
    m.ensure_indexes()  # second call must not fail
    emp = database.employees.index_information()
    att = database.attendance_logs.index_information()
    assert any(v.get("unique") and v["key"] == [("emp_code", 1)] for v in emp.values())
    assert any(v.get("unique") and v["key"] == [("emp_code", 1), ("date", 1)] for v in att.values())


# ------------------------------ employees --------------------------------- #
def test_create_employee(client):
    r = client.post("/employees", json=EMP)
    assert r.status_code == 201
    body = r.json()
    assert body["emp_code"] == "EMP0001" and body["shift_start"] == "09:30" and body["shift_end"] == "18:30"
    assert isinstance(body["created_at"], int) and body["created_at"] > 1_000_000_000_000
    assert "_id" not in body and "id" not in body


def test_create_employee_ignores_client_created_at(client):
    r = client.post("/employees", json={**EMP, "created_at": 1})
    assert r.status_code == 201 and r.json()["created_at"] > 1_000_000_000_000


def test_duplicate_emp_code_is_409(client):
    assert client.post("/employees", json=EMP).status_code == 201
    r = client.post("/employees", json={**EMP, "name": "Other"})
    assert r.status_code == 409 and "detail" in r.json()


def test_parallel_creates_yield_one_201(client):
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: client.post("/employees", json=EMP).status_code, range(16)))
    assert codes.count(201) == 1 and codes.count(409) == 15


@pytest.mark.parametrize("patch", [{"emp_code": "X1"}, {"email": "bad"}, {"joined_on": "2026-02-30"},
                                   {"shift_start": "09:00", "shift_end": "09:00"}, {"name": ""}])
def test_create_employee_validation_is_422(client, patch):
    assert client.post("/employees", json={**EMP, **patch}).status_code == 422


def test_list_employees_sorted_filtered_paginated(client, database):
    for code, dept in [("EMP0003", "Sales"), ("EMP0001", "Engineering"), ("EMP0002", "Engineering"),
                       ("EMP0004", "Sales"), ("EMP0005", "Engineering")]:
        add_emp(database, code, dept)
    r = client.get("/employees").json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0005"]
    assert r["total"] == 5 and r["page"] == 1 and r["page_size"] == 20
    eng = client.get("/employees", params={"department": "Engineering", "page_size": 2, "page": 2}).json()
    assert [e["emp_code"] for e in eng["items"]] == ["EMP0005"]  # page 2 of EMP0001, 0002 | 0005
    assert eng["total"] == 3  # the filtered total, not 5
    assert client.get("/employees", params={"department": "engineering"}).json()["total"] == 0  # case-sensitive


@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_list_employees_pagination_validation(client, params):
    assert client.get("/employees", params=params).status_code == 422


# ------------------------------- punch-in --------------------------------- #
def test_punch_in_unknown_employee_is_404(client):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404


def test_punch_in_creates_record(client, database):
    add_emp(database)
    t = ist_ms(2026, 7, 6, 10, 5)  # 35 minutes after a 09:30 start
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t})
    assert r.status_code == 201
    b = r.json()
    assert set(b) == RECORD_KEYS
    assert b["date"] == "2026-07-06" and b["status"] == "PRESENT" and b["punch_in"] == t
    assert b["punch_out"] is None and b["work_hours"] is None
    assert b["late_minutes"] == 35 and b["overtime_minutes"] == 0 and b["half_day"] is False and b["history"] == []
    stored = database.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["punch_in"], datetime) and stored["punch_out"] is None


def test_punch_in_defaults_to_now_and_pending_status(client, database):
    add_emp(database)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "WFH"})
    assert r.status_code == 201 and r.json()["status"] == "WFH" and r.json()["punch_in"] > 1_000_000_000_000


def test_punch_in_truncates_to_whole_seconds(client, database):
    add_emp(database)
    t = ist_ms(2026, 7, 6, 9, 40, 1, ms=900)
    b = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t}).json()
    assert b["punch_in"] == t - 900 and b["late_minutes"] == 10  # 09:40:01 -> 10 (R2)


def test_punch_in_grace_boundary(client, database):
    add_emp(database)
    b = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 40, 0, ms=900)}).json()
    assert b["late_minutes"] == 0  # 09:40:00.900 counts as 09:40:00


def test_punch_in_date_is_ist_not_utc(client, database):
    add_emp(database)
    t = ist_ms(2026, 7, 7, 0, 30)  # 2026-07-06 19:00 UTC
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t}).json()["date"] == "2026-07-07"


def test_overnight_punch_in_after_midnight_belongs_to_previous_day(client, database):
    add_emp(database, "EMP0005", start="22:00", end="06:00")
    b = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ist_ms(2026, 7, 7, 1, 0)}).json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 180
    # the evening shift of the 6th is the same record -> conflict
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ist_ms(2026, 7, 6, 21, 55)}).status_code == 409


def test_second_punch_in_same_date_is_409(client, database):
    add_emp(database)
    p = {"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 30)}
    assert client.post("/attendance/punch-in", json=p).status_code == 201
    r = client.post("/attendance/punch-in", json={**p, "punched_at": ist_ms(2026, 7, 6, 11, 0)})
    assert r.status_code == 409 and "detail" in r.json()
    assert client.post("/attendance/punch-in", json={**p, "punched_at": ist_ms(2026, 7, 7, 9, 30)}).status_code == 201


def test_punch_in_over_existing_absent_record_is_409(client, database):
    add_emp(database)
    database.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-08", "status": "ABSENT",
                                         "punch_in": None, "punch_out": None})
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 8, 9, 30)})
    assert r.status_code == 409


def test_parallel_punch_ins_yield_one_201(client, database):
    add_emp(database)
    t = ist_ms(2026, 7, 6, 9, 30)
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t}).status_code, range(16)))
    assert codes.count(201) == 1 and codes.count(409) == 15
    assert database.attendance_logs.count_documents({}) == 1


@pytest.mark.parametrize("body", [
    {"emp_code": "EMP0001", "status": "ABSENT"}, {"emp_code": "EMP0001", "status": "LEAVE"},
    {"emp_code": "EMP0001", "status": "bogus"}, {"emp_code": "EMP0001", "punched_at": 1783312500},
    {"emp_code": "EMP0001", "punched_at": 1783312500000.5}, {"emp_code": "EMP0001", "punched_at": "1783312500000"},
    {"emp_code": "EMP0001", "punched_at": 0}, {"emp_code": "EMP0001", "punched_at": True}, {},
])
def test_punch_in_validation_is_422(client, database, body):
    add_emp(database)
    assert client.post("/attendance/punch-in", json=body).status_code == 422


# ----------------------------- list attendance ---------------------------- #
def seed_logs(database):
    add_emp(database, "EMP0001")
    add_emp(database, "EMP0002")
    base = {"status": "PRESENT", "punch_in": None, "punch_out": None}
    for code, day, st in [("EMP0002", "2026-07-06", "PRESENT"), ("EMP0001", "2026-07-06", "WFH"),
                          ("EMP0001", "2026-07-07", "PRESENT"), ("EMP0002", "2026-07-08", "LEAVE"),
                          ("EMP0001", "2026-07-08", "ABSENT")]:
        database.attendance_logs.insert_one({**base, "emp_code": code, "date": day, "status": st})


def test_list_attendance_sort_filters_and_total(client, database):
    seed_logs(database)
    r = client.get("/attendance").json()
    order = [(i["date"], i["emp_code"]) for i in r["items"]]
    assert order == [("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"),
                     ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    assert r["total"] == 5
    r = client.get("/attendance", params={"emp_code": "EMP0001"}).json()
    assert r["total"] == 3 and [i["date"] for i in r["items"]] == ["2026-07-08", "2026-07-07", "2026-07-06"]
    r = client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-08"}).json()
    assert r["total"] == 3  # inclusive on both ends
    r = client.get("/attendance", params={"status": "ABSENT"}).json()
    assert r["total"] == 1 and r["items"][0]["status"] == "ABSENT"
    r = client.get("/attendance", params={"page": 2, "page_size": 2}).json()
    assert r["total"] == 5 and [(i["date"], i["emp_code"]) for i in r["items"]] == [("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001")]


def test_list_attendance_shapes_and_legacy_defaults(client, database):
    add_emp(database)
    t = datetime(2026, 7, 14, 4, 2, tzinfo=m.UTC)
    # legacy document: no history, no half_day, no overtime
    database.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-14", "status": "PRESENT",
                                         "punch_in": t, "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=m.UTC),
                                         "work_hours": 8.98, "late_minutes": 0})
    item = client.get("/attendance").json()["items"][0]
    assert set(item) == RECORD_KEYS
    assert item["history"] == [] and item["half_day"] is False and item["overtime_minutes"] == 0
    assert item["punch_in"] == m.dt_to_ms(t) and isinstance(item["punch_in"], int)


def test_history_times_are_epoch_ms_in_the_api(client, database):
    add_emp(database)
    a, b = datetime(2026, 7, 7, 4, 35, tzinfo=m.UTC), datetime(2026, 7, 7, 3, 58, tzinfo=m.UTC)
    database.attendance_logs.insert_one({
        "emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "punch_in": b, "punch_out": None,
        "history": [{"at": datetime(2026, 7, 8, 5, 0, tzinfo=m.UTC), "by": "hr", "reason": "glitch",
                     "changes": {"punch_in": {"from": a, "to": b}, "late_minutes": {"from": 35, "to": 0}}}]})
    h = client.get("/attendance").json()["items"][0]["history"][0]
    assert h["at"] == m.dt_to_ms(datetime(2026, 7, 8, 5, 0, tzinfo=m.UTC))
    assert h["changes"]["punch_in"] == {"from": m.dt_to_ms(a), "to": m.dt_to_ms(b)}
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}


@pytest.mark.parametrize("params", [{"date_from": "2026-07-09", "date_to": "2026-07-01"}, {"date_from": "2026-02-30"},
                                    {"date_to": "07/01/2026"}, {"status": "NOPE"}, {"page": 0}, {"page_size": 500}])
def test_list_attendance_validation_is_422(client, params):
    assert client.get("/attendance", params=params).status_code == 422
