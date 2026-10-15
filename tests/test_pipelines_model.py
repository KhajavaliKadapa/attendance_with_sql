"""Run the REAL pipeline builders and the REAL endpoint code on top of tests/mini_agg.py
(an executable model of the aggregation language, NOT MongoDB) and compare with the oracle.

Evidence value: catches wiring mistakes (variable names, $let nesting, field shapes, stage order,
post-processing). It does not prove real-MongoDB semantics; that is what
`pytest -m integration` against a real server (tests/test_step5_integration.py) is for."""
import pytest
from fastapi.testclient import TestClient

import app.main as m
from tests import oracle
from tests.dataset import EMPLOYEES, LOGS
from tests.mini_agg import MISSING, Model, ModelDB
from tests.test_step5_integration import TREND_CASES


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(m, "db", ModelDB(EMPLOYEES, LOGS))
    return TestClient(m.app)


# ---------------- the model itself, against documented behaviour I am sure of ---------------- #
def run(docs, pipeline, scope=None):
    return Model({}).run(docs, pipeline, scope)


def test_model_rank_is_standard_competition_ranking():
    docs = [{"v": 60}, {"v": 180}, {"v": 60}, {"v": 20}, {"v": 62}]
    out = run(docs, [{"$setWindowFields": {"sortBy": {"v": -1}, "output": {"rank": {"$rank": {}}}}}])
    assert [(d["v"], d["rank"]) for d in out] == [(180, 1), (62, 2), (60, 3), (60, 3), (20, 5)]


def test_model_window_avg_uses_current_plus_six_preceding_and_ignores_null():
    docs = [{"d": i, "x": x} for i, x in enumerate([1.0, None, 3.0, None, None, None, None, 9.0])]
    out = run(docs, [{"$setWindowFields": {"sortBy": {"d": 1}, "output": {
        "a": {"$avg": "$x", "window": {"documents": [-6, 0]}}}}}])
    assert [d["a"] for d in out] == [1.0, 1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 6.0]  # last window = rows 1..7: non-null 3 and 9 -> 6


def test_model_group_unwind_and_match_semantics():
    docs = [{"k": "a", "n": 1}, {"k": "a", "n": 2}, {"n": 5}]
    out = run(docs, [{"$group": {"_id": "$k", "t": {"$sum": "$n"}, "all": {"$push": "$n"}}}])
    assert {d["_id"]: (d["t"], d["all"]) for d in out} == {"a": (3, [1, 2]), None: (5, [5])}  # missing key groups as null
    assert run([{"a": [1, 2]}, {"a": []}, {}], [{"$unwind": "$a"}]) == [{"a": 1}, {"a": 2}]
    assert len(run([{"a": []}, {}], [{"$unwind": {"path": "$a", "preserveNullAndEmptyArrays": True}}])) == 2
    assert run([{"x": None}, {"x": 5}, {}], [{"$match": {"x": {"$gt": 0}}}]) == [{"x": 5}]  # null never matches $gt


def test_model_complains_about_unknown_things():
    with pytest.raises(NotImplementedError):
        run([{}], [{"$densify": {}}])
    with pytest.raises(NotImplementedError):
        run([{}], [{"$project": {"a": {"$nonsense": 1}}}])
    with pytest.raises(KeyError):                       # a misspelt $$variable is an error, not a silent null
        run([{}], [{"$project": {"a": {"$let": {"vars": {"x": 1}, "in": "$$y"}}}}])
    assert MISSING is not None


# ---------------- the real endpoints on the model, compared with the oracle ---------------- #
@pytest.mark.parametrize("month", ["2026-07", "2026-08", "2026-02", "2026-06"])
@pytest.mark.parametrize("code", [e["emp_code"] for e in EMPLOYEES])
def test_monthly(api, code, month):
    r = api.get(f"/analytics/employees/{code}/monthly", params={"month": month})
    assert r.status_code == 200 and r.json() == oracle.employee_monthly(EMPLOYEES, LOGS, code, month)


@pytest.mark.parametrize("month", ["2026-07", "2026-08", "2026-02", "2026-01", "2026-12"])
@pytest.mark.parametrize("department", [None, "Engineering", "Sales", "Legal", "Nope"])
def test_summary(api, month, department):
    params = {"month": month, **({"department": department} if department else {})}
    r = api.get("/analytics/departments/summary", params=params)
    assert r.status_code == 200 and r.json() == oracle.department_summary(EMPLOYEES, LOGS, month, department)


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 6, 10, 50])
@pytest.mark.parametrize("department", [None, "Sales", "Engineering", "Support", "Nope"])
def test_leaderboard(api, limit, department):
    params = {"month": "2026-07", "limit": limit, **({"department": department} if department else {})}
    r = api.get("/analytics/leaderboard/late", params=params)
    assert r.status_code == 200 and r.json() == oracle.late_leaderboard(EMPLOYEES, LOGS, "2026-07", limit, department)


@pytest.mark.parametrize("department,d_from,d_to", TREND_CASES)
def test_trend(api, department, d_from, d_to):
    r = api.get(f"/analytics/departments/{department}/trend", params={"from": d_from, "to": d_to})
    assert r.status_code == 200 and r.json() == oracle.department_trend(EMPLOYEES, LOGS, department, d_from, d_to)


def test_trend_errors_and_404(api):
    url = "/analytics/departments/Engineering/trend"
    assert api.get(url, params={"from": "2026-07-14", "to": "2026-07-01"}).status_code == 422
    assert api.get(url, params={"from": "2026-06-01", "to": "2026-09-01"}).status_code == 422
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404


def test_model_lookup_joins_then_runs_the_sub_pipeline_and_rejects_unknown_keys():
    m_ = Model({"f": [{"k": 1, "d": "a"}, {"k": 1, "d": "z"}, {"k": 2, "d": "a"}]})
    out = m_.run([{"id": 1}, {"id": 3}], [{"$lookup": {
        "from": "f", "localField": "id", "foreignField": "k",
        "pipeline": [{"$match": {"d": {"$lte": "m"}}}], "as": "x"}}])
    assert out == [{"id": 1, "x": [{"k": 1, "d": "a"}]}, {"id": 3, "x": []}]   # join, then filter; no match -> []
    with pytest.raises(NotImplementedError):
        m_.run([{"id": 1}], [{"$lookup": {"from": "f", "localField": "id", "foreignField": "k", "as": "x", "bogus": 1}}])
