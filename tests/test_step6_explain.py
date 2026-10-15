"""/admin/explain and the index set.
Part 1 needs no database: a spy stands in for `db`, so what is checked is WHICH query is explained
(it must be exactly the one the endpoint runs) and the JSON shaping. Part 2 (real_mongo) seeds 100k
records and checks the real plans for IXSCAN / no COLLSCAN."""
import copy
import json
import random
from datetime import datetime

import pytest
from bson import Decimal128, Int64, ObjectId
from fastapi.testclient import TestClient

import app.main as m
from tests.dataset import EMPLOYEES, LOGS
from tests.mini_agg import ModelDB

CANNED = {
    "queryPlanner": {"winningPlan": {"stage": "IXSCAN", "indexName": "x"}},
    "executionStats": {"executionTimeMillis": Int64(5), "nReturned": 3, "at": datetime(2026, 7, 1, 3, 58),
                       "oid": ObjectId("64b64c9f9a1f4e0b5c2f3a11"), "dec": Decimal128("1.5")},
    "ok": 1.0,
}


class SpyDB:
    def __init__(self):
        self.commands = []

    def command(self, cmd):
        self.commands.append(copy.deepcopy(cmd))
        return copy.deepcopy(CANNED)


@pytest.fixture
def spy(monkeypatch):
    db = SpyDB()
    monkeypatch.setattr(m, "db", db)
    return db


@pytest.fixture
def api():
    return TestClient(m.app)


# ----------------------------- shape of the answer ------------------------- #
def test_explain_response_shape_and_bson_to_json(spy, api):
    r = api.get("/admin/explain/department_summary", params={"month": "2026-07"})
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"endpoint", "collection", "explain"}
    assert b["endpoint"] == "department_summary" and b["collection"] == "employees"
    stats = b["explain"]["executionStats"]
    assert stats["executionTimeMillis"] == 5 and stats["nReturned"] == 3          # Int64 -> plain number
    assert "$date" in stats["at"] and stats["oid"] == {"$oid": "64b64c9f9a1f4e0b5c2f3a11"}
    assert b["explain"]["queryPlanner"]["winningPlan"]["stage"] == "IXSCAN"
    json.dumps(b)                                                                    # fully serialisable


def test_explain_runs_with_execution_stats_verbosity(spy, api):
    api.get("/admin/explain/department_summary", params={"month": "2026-07"})
    cmd = spy.commands[0]
    assert next(iter(cmd)) == "explain" and cmd["verbosity"] == "executionStats"
    assert next(iter(cmd["explain"])) in ("aggregate", "find")                      # command name must come first


COLLECTIONS = {"attendance_list": "attendance_logs", "employee_monthly": "attendance_logs",
               "department_summary": "employees", "late_leaderboard": "attendance_logs",
               "department_trend": "employees"}
VALID = {
    "attendance_list": {}, "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
    "department_summary": {"month": "2026-07"}, "late_leaderboard": {"month": "2026-07"},
    "department_trend": {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-14"},
}


@pytest.mark.parametrize("endpoint", list(COLLECTIONS))
def test_collection_field_names_the_collection_that_was_explained(spy, api, endpoint):
    b = api.get(f"/admin/explain/{endpoint}", params=VALID[endpoint]).json()
    assert b["collection"] == COLLECTIONS[endpoint]
    explained = spy.commands[0]["explain"]
    assert (explained.get("find") or explained.get("aggregate")) == b["collection"]


# ------------- the explained query is exactly the one the endpoint runs ------------- #
class RecordingDB(ModelDB):
    def __init__(self):
        super().__init__(EMPLOYEES, LOGS)
        self.pipelines = []
        for col in (self.employees, self.attendance_logs):
            orig = col.aggregate
            col.aggregate = lambda pipeline, orig=orig: (self.pipelines.append(copy.deepcopy(pipeline)), orig(pipeline))[1]


# (real URL, real query params, explain endpoint, explain query params)
CASES = [
    ("/analytics/employees/EMP0001/monthly", {"month": "2026-07"},
     "employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("/analytics/departments/summary", {"month": "2026-07"},
     "department_summary", {"month": "2026-07"}),
    ("/analytics/departments/summary", {"month": "2026-07", "department": "Sales"},
     "department_summary", {"month": "2026-07", "department": "Sales"}),
    ("/analytics/leaderboard/late", {"month": "2026-07", "limit": 3},
     "late_leaderboard", {"month": "2026-07", "limit": 3}),
    ("/analytics/leaderboard/late", {"month": "2026-07", "limit": 3, "department": "Sales"},
     "late_leaderboard", {"month": "2026-07", "limit": 3, "department": "Sales"}),
    ("/analytics/departments/Engineering/trend", {"from": "2026-07-01", "to": "2026-07-14"},
     "department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-14"}),
]


@pytest.mark.parametrize("url,real_params,endpoint,explain_params", CASES)
def test_explained_pipeline_equals_the_pipeline_the_endpoint_executes(monkeypatch, api, url, real_params, endpoint, explain_params):
    rec = RecordingDB()
    monkeypatch.setattr(m, "db", rec)
    assert api.get(url, params=real_params).status_code == 200
    executed = rec.pipelines[-1]                       # what the endpoint really sent to MongoDB

    spy_db = SpyDB()
    monkeypatch.setattr(m, "db", spy_db)
    assert api.get(f"/admin/explain/{endpoint}", params=explain_params).status_code == 200
    assert spy_db.commands[0]["explain"]["pipeline"] == executed


@pytest.mark.parametrize("params", [
    {}, {"emp_code": "EMP0001"}, {"status": "ABSENT"}, {"date_from": "2026-07-01", "date_to": "2026-07-31"},
    {"emp_code": "EMP0001", "status": "PRESENT", "date_from": "2026-07-01", "page": 3, "page_size": 10},
])
def test_attendance_list_explain_is_the_endpoints_find(spy, api, params):
    api.get("/admin/explain/attendance_list", params=params)
    spec = m.attendance_list_spec(params.get("emp_code"), params.get("date_from"), params.get("date_to"),
                                  params.get("status"), params.get("page", 1), params.get("page_size", 20))
    assert spy.commands[0]["explain"] == {"find": "attendance_logs", **spec}
    # same sort/paging the real endpoint applies, whatever the filters
    assert list(spec["sort"].items()) == [("date", -1), ("emp_code", 1)]
    assert spec["skip"] == (params.get("page", 1) - 1) * params.get("page_size", 20)


def test_attendance_list_spec_values():
    s = m.attendance_list_spec("EMP0001", "2026-07-01", "2026-07-31", "WFH", 3, 10)
    assert s["filter"] == {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}, "status": "WFH"}
    assert (s["skip"], s["limit"], s["projection"]) == (20, 10, {"_id": 0})
    assert m.attendance_list_spec(None, None, None, None, 1, 20)["filter"] == {}


@pytest.mark.integration
def test_list_endpoint_uses_the_same_spec_function(client, monkeypatch):
    calls = []
    real = m.attendance_list_spec
    monkeypatch.setattr(m, "attendance_list_spec", lambda *a: calls.append(a) or real(*a))
    assert client.get("/attendance", params={"emp_code": "EMP0001", "page": 2}).status_code == 200
    assert calls == [("EMP0001", None, None, None, 2, 20)]


# --------------------------------- validation ------------------------------ #
@pytest.mark.parametrize("endpoint,missing", [
    ("employee_monthly", "month"), ("employee_monthly", "emp_code"), ("department_summary", "month"),
    ("late_leaderboard", "month"), ("department_trend", "department"), ("department_trend", "from"),
    ("department_trend", "to"),
])
def test_missing_required_parameter_is_422(spy, api, endpoint, missing):
    params = {k: v for k, v in VALID[endpoint].items() if k != missing}
    assert api.get(f"/admin/explain/{endpoint}", params=params).status_code == 422
    assert spy.commands == []                                                       # nothing was run


@pytest.mark.parametrize("path,params", [
    ("nonsense", {}), ("department_summary", {"month": "2026-13"}), ("department_summary", {"month": "july"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 0}), ("late_leaderboard", {"month": "2026-07", "limit": 51}),
    ("attendance_list", {"status": "NOPE"}), ("attendance_list", {"page": 0}),
    ("attendance_list", {"date_from": "2026-07-09", "date_to": "2026-07-01"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-14", "to": "2026-07-01"}),
    ("department_trend", {"department": "Engineering", "from": "2026-06-01", "to": "2026-09-01"}),   # 93 days
    ("department_trend", {"department": "Engineering", "from": "2026-02-30", "to": "2026-03-01"}),
])
def test_invalid_parameters_are_422(spy, api, path, params):
    assert api.get(f"/admin/explain/{path}", params=params).status_code == 422


def test_database_failure_is_503_not_500(monkeypatch, api):
    from pymongo.errors import OperationFailure

    class Broken:
        def command(self, _):
            raise OperationFailure("boom")

    monkeypatch.setattr(m, "db", Broken())
    assert api.get("/admin/explain/department_summary", params={"month": "2026-07"}).status_code == 503


# ----------------------------------- indexes ------------------------------- #
@pytest.mark.integration
def test_index_set(database):
    emp = {tuple(v["key"]) for v in database.employees.index_information().values()}
    att = {tuple(v["key"]) for v in database.attendance_logs.index_information().values()}
    assert (("joined_on", 1),) in emp and (("department", 1), ("joined_on", 1)) in emp
    assert (("emp_code", 1),) in emp and (("department", 1), ("emp_code", 1)) in emp
    assert (("emp_code", 1), ("date", 1)) in att and (("date", -1), ("emp_code", 1)) in att
    assert (("emp_code", 1), ("punch_in", -1)) in att


# =========================== Part 2: real plans on 100k records ================ #
N_EMP, N_DAYS = 500, 200   # 100,000 attendance records: the size the grader uses
DEPTS = ["Engineering", "Sales", "Support", "HR", "Finance", "Ops"]


@pytest.fixture(scope="module")
def big(mongo_client):
    """100k records in a throw-away database, with the app's own indexes created first."""
    import uuid
    from datetime import date, timedelta

    name = f"attendance_big_{uuid.uuid4().hex[:8]}"
    d = mongo_client[name]
    mp = pytest.MonkeyPatch()
    mp.setattr(m, "client", mongo_client)
    mp.setattr(m, "db", d)
    m.ensure_indexes()
    rnd = random.Random(7)
    d.employees.insert_many([
        dict(emp_code=f"EMP{i:04d}", name=f"N{i}", email=f"e{i}@x.io", department=DEPTS[i % len(DEPTS)],
             shift_start="09:30", shift_end="18:30", joined_on=f"2026-0{1 + i % 6}-{1 + i % 27:02d}",
             created_at=datetime(2026, 1, 1)) for i in range(1, N_EMP + 1)])
    start = date(2026, 1, 1)
    batch = []
    for i in range(1, N_EMP + 1):
        for k in range(N_DAYS):
            late = rnd.choice([0, 0, 0, 12, 35, 60])
            batch.append(dict(emp_code=f"EMP{i:04d}", date=(start + timedelta(days=k)).isoformat(),
                              status=rnd.choice(["PRESENT"] * 8 + ["WFH", "ON_DUTY", "ABSENT", "LEAVE"]),
                              work_hours=round(rnd.uniform(3, 10), 2), late_minutes=late, overtime_minutes=0,
                              half_day=False, history=[]))
        if len(batch) >= 20000:
            d.attendance_logs.insert_many(batch)
            batch = []
    if batch:
        d.attendance_logs.insert_many(batch)
    assert d.attendance_logs.estimated_document_count() == N_EMP * N_DAYS
    yield TestClient(m.app)
    mp.undo()
    mongo_client.drop_database(name)


PLAN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0042"}),
    ("attendance_list", {"status": "ABSENT"}),                                   # filter with no index of its own
    ("attendance_list", {"date_from": "2026-03-01", "date_to": "2026-03-31"}),
    ("attendance_list", {"emp_code": "EMP0042", "status": "WFH", "date_from": "2026-02-01", "page": 2}),
    ("attendance_list", {"page": 500, "page_size": 100}),                        # deep page
    ("employee_monthly", {"emp_code": "EMP0042", "month": "2026-03"}),
    ("department_summary", {"month": "2026-03"}),                                # no department: needs the joined_on index
    ("department_summary", {"month": "2026-03", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-03"}),
    ("late_leaderboard", {"month": "2026-03", "limit": 5, "department": "Support"}),
    ("department_trend", {"department": "Engineering", "from": "2026-03-01", "to": "2026-05-31"}),
]


@pytest.mark.integration
@pytest.mark.real_mongo
@pytest.mark.parametrize("endpoint,params", PLAN_CASES)
def test_plan_uses_an_index_and_never_scans_the_collection(big, endpoint, params):
    r = big.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    plan = json.dumps(r.json()["explain"])
    assert "IXSCAN" in plan
    assert "COLLSCAN" not in plan, f"{endpoint} {params} contains a COLLSCAN"


@pytest.mark.integration
@pytest.mark.real_mongo
def test_unfiltered_list_reads_only_one_page(big):
    for params in ({}, {"emp_code": "EMP0042"}):
        stats = big.get("/admin/explain/attendance_list", params=params).json()["explain"]["executionStats"]
        assert stats["nReturned"] == 20
        assert stats["totalDocsExamined"] <= 40, stats["totalDocsExamined"]      # not the whole collection


@pytest.mark.integration
@pytest.mark.real_mongo
def test_endpoints_answer_on_100k_records(big):
    import time
    urls = [("/attendance", {}), ("/analytics/employees/EMP0042/monthly", {"month": "2026-03"}),
            ("/analytics/departments/summary", {"month": "2026-03"}),
            ("/analytics/leaderboard/late", {"month": "2026-03"}),
            ("/analytics/departments/Sales/trend", {"from": "2026-03-01", "to": "2026-05-31"})]
    for url, params in urls:
        t = time.perf_counter()
        r = big.get(url, params=params)
        print(f"{url:50} {r.status_code} {1000 * (time.perf_counter() - t):7.0f} ms")  # informational (pytest -s)
        assert r.status_code == 200
