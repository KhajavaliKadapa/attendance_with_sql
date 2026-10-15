"""MongoDB -> SQL synchronisation: what happens when the SQL side fails, and how it is repaired.

MongoDB here is a mongomock stand-in used ONLY for basic document operations (insert, find, update with
upsert, delete, distinct). No aggregation runs in this file, and none of it says anything about how a real
MongoDB behaves; the real-server tests are the *_integration.py files and test_step6_explain.py.
SQL is a temporary SQLite file (conftest fixture isolated_sql).
"""
import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.main as m
from tests.dataset import emp
from tests.test_sql_repository import E, put

BODY = dict(emp_code="EMP0001", name="Asha Rao", email="asha@example.com", department="Engineering", joined_on="2026-01-05")


@pytest.fixture
def mongo(monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    cl = mongomock.MongoClient(tz_aware=True)
    d = cl["sync_test"]
    monkeypatch.setattr(m, "client", cl)
    monkeypatch.setattr(m, "db", d)
    m.ensure_indexes()
    return d


@pytest.fixture
def api(mongo):
    return TestClient(m.app)


@pytest.fixture
def sql_down(monkeypatch):
    def boom(*a, **k):
        raise OperationalError("INSERT", {}, Exception("database is locked"))
    monkeypatch.setattr(m.sql_repo, "upsert_employee", boom)


def mongo_doc(code, name="Asha Rao", dept="Engineering", joined="2026-01-05", **extra):
    return {**E(code, name, dept, joined), **extra}


# ------------------------------- the happy path ------------------------------ #
def test_create_mirrors_the_employee_into_sql(api, mongo, isolated_sql):
    r = api.post("/employees", json=BODY)
    assert r.status_code == 201 and r.headers["X-SQL-Sync"] == "ok"
    row = isolated_sql.get_by_code("EMP0001")
    stored = mongo.employees.find_one({"emp_code": "EMP0001"})
    assert {k: row[k] for k in ("name", "email", "department", "joined_on", "shift_start", "shift_end")} == \
           {k: stored[k] for k in ("name", "email", "department", "joined_on", "shift_start", "shift_end")}
    assert m.sql_row_to_api(row) == r.json()                          # SQL and the API response agree, created_at included
    assert api.get("/sql/employees/EMP0001").json() == r.json()       # and so does the SQL endpoint


def test_duplicate_create_is_409_and_touches_nothing_in_sql(api, mongo, isolated_sql):
    api.post("/employees", json=BODY)
    r = api.post("/employees", json={**BODY, "name": "Impostor"})
    assert r.status_code == 409 and "X-SQL-Sync" not in r.headers
    assert isolated_sql.get_by_code("EMP0001")["name"] == "Asha Rao"
    assert mongo.employees.count_documents({}) == 1 and "sql_sync_outbox" not in mongo.list_collection_names()


def test_invalid_create_is_422_and_touches_nothing(api, mongo, isolated_sql):
    assert api.post("/employees", json={**BODY, "joined_on": "2026-02-30"}).status_code == 422
    assert isolated_sql.count() == 0 and mongo.employees.count_documents({}) == 0


def test_parallel_creates_of_one_code_yield_one_201_and_one_sql_row(api, isolated_sql):
    with __import__("concurrent.futures").futures.ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: api.post("/employees", json=BODY).status_code, range(8)))
    assert codes.count(201) == 1 and codes.count(409) == 7 and isolated_sql.count() == 1


# --------------------- SQL failure must not change the API --------------------- #
def test_sql_failure_does_not_change_the_create_response(api, mongo, isolated_sql, monkeypatch):
    ok = api.post("/employees", json={**BODY, "emp_code": "EMP0001"})
    monkeypatch.setattr(m.sql_repo, "upsert_employee", lambda row: (_ for _ in ()).throw(OperationalError("x", {}, Exception("locked"))))
    bad = api.post("/employees", json={**BODY, "emp_code": "EMP0002"})
    assert (bad.status_code, set(bad.json())) == (ok.status_code, set(ok.json())) == (201, set(ok.json()))
    assert bad.json()["emp_code"] == "EMP0002" and bad.headers["X-SQL-Sync"] == "pending"
    assert mongo.employees.find_one({"emp_code": "EMP0002"}) is not None          # MongoDB committed
    assert isolated_sql.get_by_code("EMP0002") is None                            # SQL did not


def test_failure_is_recorded_for_retry_with_the_reason(api, mongo, sql_down):
    assert api.post("/employees", json=BODY).headers["X-SQL-Sync"] == "pending"
    rec = mongo.sql_sync_outbox.find_one({"emp_code": "EMP0001"})
    assert rec["status"] == "pending" and rec["attempts"] == 1 and "database is locked" in rec["last_error"]
    assert isinstance(rec["created_at"], datetime) and isinstance(rec["last_attempt_at"], datetime)
    s = api.get("/sql/sync/status").json()
    assert (s["mongo_employees"], s["sql_employees"], s["counts_match"], s["pending_retries"]) == (1, 0, False, 1)


def test_repeated_failures_keep_one_record_and_count_attempts(mongo, sql_down):
    doc = mongo_doc("EMP0001")
    for _ in range(3):
        assert m.sync_employee_to_sql(doc) == "pending"
    assert mongo.sql_sync_outbox.count_documents({}) == 1
    assert mongo.sql_sync_outbox.find_one({})["attempts"] == 3


@pytest.mark.parametrize("error", [RuntimeError("anything at all"), OSError("disk full"), ValueError("weird")])
def test_any_exception_from_sql_is_contained(api, mongo, monkeypatch, error):
    monkeypatch.setattr(m.sql_repo, "upsert_employee", lambda row: (_ for _ in ()).throw(error))
    r = api.post("/employees", json=BODY)
    assert r.status_code == 201 and r.headers["X-SQL-Sync"] == "pending"


def test_even_a_failing_outbox_write_cannot_break_the_request(api, mongo, sql_down, monkeypatch):
    """SQL is down AND the retry record cannot be written. The employee is already safe in MongoDB, and
    reconcile compares every employee, so it is still found later: the request must still succeed."""
    class BrokenOutbox:
        def create_index(self, *a, **k):
            raise RuntimeError("mongo hiccup")

        def update_one(self, *a, **k):
            raise RuntimeError("mongo hiccup")

    class Proxy:
        sql_sync_outbox = BrokenOutbox()

        def __getattr__(self, name):
            return getattr(mongo, name)

    monkeypatch.setattr(m, "db", Proxy())
    r = api.post("/employees", json=BODY)
    assert r.status_code == 201 and r.headers["X-SQL-Sync"] == "pending"
    assert mongo.employees.find_one({"emp_code": "EMP0001"}) is not None


# ------------------------------- reconcile / migrate ------------------------- #
def test_reconcile_migrates_existing_mongodb_employees_into_an_empty_sql_database(api, mongo, isolated_sql):
    mongo.employees.insert_many([mongo_doc(f"EMP000{i}", f"Name {i}") for i in range(1, 4)])    # existed before SQL did
    assert api.get("/sql/sync/status").json()["counts_match"] is False
    r = api.post("/sql/sync/reconcile").json()
    assert (r["mongo_employees"], r["inserted"], r["updated"], r["unchanged"], r["failed_count"]) == (3, 3, 0, 0, 0)
    assert isolated_sql.count() == 3 and api.get("/sql/sync/status").json()["counts_match"] is True
    again = api.post("/sql/sync/reconcile").json()                                              # idempotent
    assert (again["inserted"], again["updated"], again["unchanged"]) == (0, 0, 3) and isolated_sql.count() == 3


def test_reconcile_picks_up_changes_made_directly_in_mongodb(api, mongo, isolated_sql):
    mongo.employees.insert_one(mongo_doc("EMP0001"))
    api.post("/sql/sync/reconcile")
    mongo.employees.update_one({"emp_code": "EMP0001"}, {"$set": {"department": "Sales", "email": "new@x.io"}})
    r = api.post("/sql/sync/reconcile").json()
    assert (r["inserted"], r["updated"], r["unchanged"]) == (0, 1, 0)
    row = isolated_sql.get_by_code("EMP0001")
    assert (row["department"], row["email"]) == ("Sales", "new@x.io")


def test_reconcile_repairs_a_failed_create_and_clears_the_retry_record(api, mongo, isolated_sql, monkeypatch):
    with monkeypatch.context() as mp:
        mp.setattr(m.sql_repo, "upsert_employee", lambda row: (_ for _ in ()).throw(OperationalError("x", {}, Exception("locked"))))
        assert api.post("/employees", json=BODY).headers["X-SQL-Sync"] == "pending"
    assert mongo.sql_sync_outbox.count_documents({}) == 1 and isolated_sql.count() == 0
    r = api.post("/sql/sync/reconcile").json()                      # SQL is healthy again
    assert (r["inserted"], r["failed_count"], r["retry_records_cleared"]) == (1, 0, 1)
    assert mongo.sql_sync_outbox.count_documents({}) == 0 and isolated_sql.get_by_code("EMP0001") is not None
    s = api.get("/sql/sync/status").json()
    assert (s["counts_match"], s["pending_retries"]) == (True, 0)


def test_a_row_sql_rejects_is_reported_and_keeps_its_retry_record(api, mongo, isolated_sql):
    good, bad = mongo_doc("EMP0001"), mongo_doc("EMP0002", joined="2026-7-1")     # bad date format: CHECK constraint
    mongo.employees.insert_many([good, bad])
    assert m.sync_employee_to_sql(bad) == "pending"                                # queued, because SQL refuses it
    assert m.sync_employee_to_sql(good) == "ok"
    r = api.post("/sql/sync/reconcile").json()
    assert r["failed_count"] == 1 and r["failed"][0]["emp_code"] == "EMP0002" and "constraint" in r["failed"][0]["error"].lower()
    assert (r["inserted"], r["updated"], r["unchanged"]) == (0, 0, 1)   # EMP0001 was already mirrored; the rejected row is NOT counted as inserted
    assert isolated_sql.get_by_code("EMP0001") and isolated_sql.get_by_code("EMP0002") is None   # the good row was not blocked
    assert mongo.sql_sync_outbox.find_one({"emp_code": "EMP0002"}) is not None     # still visible, not silently dropped
    assert api.get("/sql/sync/status").json()["pending_retries"] == 1


def test_a_rejected_row_is_never_reported_as_inserted(api, mongo, isolated_sql):
    mongo.employees.insert_many([mongo_doc("EMP0001"), mongo_doc("EMP0002", joined="not-a-date"), mongo_doc("EMP0003")])
    r = api.post("/sql/sync/reconcile").json()
    assert (r["inserted"], r["failed_count"]) == (2, 1)                 # only the two good rows
    assert sorted(isolated_sql.all_codes()) == ["EMP0001", "EMP0003"]


def test_orphans_in_sql_are_reported_and_only_removed_on_request(api, mongo, isolated_sql):
    mongo.employees.insert_one(mongo_doc("EMP0001"))
    put(isolated_sql, E("EMP0777", "Ghost", "X", "2026-01-01"))
    r = api.post("/sql/sync/reconcile").json()
    assert r["orphans_in_sql"] == ["EMP0777"] and r["orphans_removed"] == 0 and isolated_sql.count() == 2
    r = api.post("/sql/sync/reconcile", params={"prune": "true"}).json()
    assert r["orphans_removed"] == 1 and isolated_sql.all_codes() == ["EMP0001"]


def test_reconcile_in_batches_over_more_than_one_batch(api, mongo, isolated_sql):
    mongo.employees.insert_many([mongo_doc(f"EMP{i:05d}", f"N{i}") for i in range(1200)])       # 3 batches of <= 500
    r = api.post("/sql/sync/reconcile").json()
    assert (r["mongo_employees"], r["inserted"], r["failed_count"]) == (1200, 1200, 0) and isolated_sql.count() == 1200
    assert api.post("/sql/sync/reconcile").json()["unchanged"] == 1200


def test_reconcile_when_sql_is_down_is_503(api, mongo, monkeypatch, tmp_path):
    mongo.employees.insert_one(mongo_doc("EMP0001"))
    monkeypatch.setattr(m, "sql_repo", m.SqlEmployeeRepository(f"sqlite:///{(tmp_path / 'nope' / 'x.db').as_posix()}"))
    assert api.post("/sql/sync/reconcile").status_code == 503
    assert api.get("/sql/sync/status").status_code == 503


def test_employee_without_created_at_reconciles_to_a_stable_value(api, mongo, isolated_sql):
    doc = mongo_doc("EMP0001"); doc.pop("created_at")
    mongo.employees.insert_one(doc)
    assert api.post("/sql/sync/reconcile").json()["inserted"] == 1
    assert api.post("/sql/sync/reconcile").json()["unchanged"] == 1                # not "updated" every time


# ---------------------------- employees without attendance ------------------- #
def test_employees_without_attendance_uses_mongodb_attendance(api, mongo, isolated_sql):
    put(isolated_sql, E("EMP0001", "A", "X", "2026-01-01"), E("EMP0002", "B", "X", "2026-01-01"), E("EMP0003", "C", "X", "2026-01-01"))
    mongo.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT"},
        {"emp_code": "EMP0001", "date": "2026-07-02", "status": "LEAVE"},
        {"emp_code": "EMP0099", "date": "2026-07-01", "status": "PRESENT"}])                  # unknown to SQL: ignored
    b = api.get("/sql/employees/without-attendance").json()
    assert [i["emp_code"] for i in b["items"]] == ["EMP0002", "EMP0003"] and b["total"] == 2
    assert [i["emp_code"] for i in api.get("/sql/employees/without-attendance", params={"page_size": 1, "page": 2}).json()["items"]] == ["EMP0003"]
    assert api.get("/sql/employees/without-attendance").status_code == 200           # not read as employee code "without-attendance"


def test_status_shape(api, mongo):
    s = api.get("/sql/sync/status").json()
    assert set(s) == {"mongo_employees", "sql_employees", "counts_match", "pending_retries", "note"}
    assert s["counts_match"] is True and s["pending_retries"] == 0
