"""The /sql endpoints on a temporary SQLite file. No MongoDB involved (the two endpoints that
need MongoDB are tested in test_sql_sync.py).   Run: pytest tests/test_sql_api.py"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.main as m
from tests.test_sql_repository import ALL, E, put

EMPLOYEE_KEYS = {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}


@pytest.fixture
def api(isolated_sql):
    put(isolated_sql, *ALL)
    return TestClient(m.app)


def codes(body):
    return [i["emp_code"] for i in body["items"]]


# ---------------------------------- list / one ------------------------------ #
def test_list_shape_matches_the_existing_employees_endpoint(api):
    r = api.get("/sql/employees")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"items", "total", "page", "page_size"}                  # same envelope as GET /employees
    assert (b["total"], b["page"], b["page_size"]) == (6, 1, 20) and codes(b) == [e["emp_code"] for e in ALL]
    assert all(set(i) == EMPLOYEE_KEYS for i in b["items"])                  # no internal id
    assert all(isinstance(i["created_at"], int) and i["created_at"] > 1_000_000_000_000 for i in b["items"])


def test_list_pagination(api):
    b = api.get("/sql/employees", params={"page": 2, "page_size": 4}).json()
    assert codes(b) == ["EMP0005", "EMP0006"] and b["total"] == 6 and b["page"] == 2 and b["page_size"] == 4
    assert api.get("/sql/employees", params={"page": 9}).json()["items"] == []


@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_list_pagination_validation_is_422(api, params):
    assert api.get("/sql/employees", params=params).status_code == 422


def test_get_one_and_404(api):
    r = api.get("/sql/employees/EMP0002")
    assert r.status_code == 200 and set(r.json()) == EMPLOYEE_KEYS and r.json()["name"] == "Ravi Kumar"
    r = api.get("/sql/employees/EMP9999")
    assert r.status_code == 404 and "detail" in r.json()


def test_fixed_paths_are_not_swallowed_by_the_emp_code_route(api):
    """/sql/employees/search must be the search endpoint, not employee code "search"."""
    r = api.get("/sql/employees/search", params={"q": "kumar"})
    assert r.status_code == 200 and "items" in r.json()


# ----------------------------------- search -------------------------------- #
def test_search_by_name_code_and_alias(api):
    assert codes(api.get("/sql/employees/search", params={"q": "kumar"}).json()) == ["EMP0002", "EMP0004"]
    assert codes(api.get("/sql/employees/search", params={"q": "0003"}).json()) == ["EMP0003"]
    assert codes(api.get("/sql/employees/search", params={"name": "meera"}).json()) == ["EMP0003"]   # ?name= alias
    b = api.get("/sql/employees/search", params={"q": "kumar", "page_size": 1, "page": 2}).json()
    assert codes(b) == ["EMP0004"] and b["total"] == 2


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"name": ""}, {"q": "x" * 101}, {"q": "a", "page_size": 0}])
def test_search_validation_is_422(api, params):
    assert api.get("/sql/employees/search", params=params).status_code == 422


def test_search_input_is_data_not_sql(api, isolated_sql):
    for hostile in ["x'; DROP TABLE employees; --", "' OR '1'='1", "%"]:
        r = api.get("/sql/employees/search", params={"q": hostile})
        assert r.status_code == 200
        assert r.json()["total"] == (1 if hostile == "%" else 0)      # "%" only matches the one name containing "%"
    assert isolated_sql.count() == 6


# -------------------------------- departments ------------------------------ #
def test_department_employees(api):
    b = api.get("/sql/departments/Engineering/employees").json()
    assert codes(b) == ["EMP0001", "EMP0002", "EMP0003"] and b["total"] == 3
    assert api.get("/sql/departments/engineering/employees").json()["total"] == 0        # case-sensitive, like MongoDB
    b = api.get("/sql/departments/Nowhere/employees")
    assert b.status_code == 200 and b.json()["items"] == []                              # same as GET /employees?department=
    assert api.get("/sql/departments/Engineering/employees", params={"page_size": 2, "page": 2}).json()["items"][0]["emp_code"] == "EMP0003"


def test_department_counts_and_summary_shapes(api):
    assert api.get("/sql/analytics/department-counts").json() == {"items": [
        {"department": "Engineering", "headcount": 3}, {"department": "Sales", "headcount": 2},
        {"department": "Support", "headcount": 1}]}
    items = api.get("/sql/analytics/department-summary").json()["items"]
    assert items[2] == {"department": "Support", "headcount": 1, "earliest_joined_on": "2026-03-10",
                        "latest_joined_on": "2026-03-10", "overnight_shift_count": 1}


# ---------------------------------- dates ---------------------------------- #
def test_recent_joiners_range_is_inclusive(api):
    r = api.get("/sql/analytics/recent-joiners", params={"from_date": "2026-02-01", "to_date": "2026-03-10"})
    assert r.status_code == 200 and codes(r.json()) == ["EMP0003", "EMP0004", "EMP0006"] and r.json()["total"] == 3
    assert codes(api.get("/sql/analytics/recent-joiners", params={"from_date": "2026-07-01"}).json()) == ["EMP0002", "EMP0005"]
    assert api.get("/sql/analytics/recent-joiners", params={"from_date": "2027-01-01"}).json()["items"] == []


@pytest.mark.parametrize("params", [
    {}, {"to_date": "2026-07-01"}, {"from_date": "2026-02-30"}, {"from_date": "07/01/2026"},
    {"from_date": "2026-07-09", "to_date": "2026-07-01"}, {"from_date": "2026-07-01", "to_date": "nope"},
])
def test_recent_joiners_validation_is_422(api, params):
    assert api.get("/sql/analytics/recent-joiners", params=params).status_code == 422


def test_joined_after_is_strict(api):
    assert codes(api.get("/sql/analytics/joined-after", params={"date": "2026-07-01"}).json()) == ["EMP0002"]
    assert api.get("/sql/analytics/joined-after", params={"date": "2026-13-01"}).status_code == 422
    assert api.get("/sql/analytics/joined-after").status_code == 422


def test_duplicate_emails_endpoint(api, isolated_sql):
    assert api.get("/sql/analytics/duplicate-emails").json() == {"items": []}
    put(isolated_sql, E("EMP0007", "Twin", "Sales", "2026-01-01", email="EMP0001@example.com"))
    assert api.get("/sql/analytics/duplicate-emails").json() == {
        "items": [{"email": "emp0001@example.com", "count": 2, "emp_codes": ["EMP0001", "EMP0007"]}]}


# ------------------------------- failure behaviour -------------------------- #
def test_sql_failure_is_503_not_500(api, monkeypatch):
    def boom(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("disk I/O error"))
    monkeypatch.setattr(m.sql_repo, "list_all", boom)
    r = api.get("/sql/employees")
    assert r.status_code == 503 and r.json() == {"detail": "SQL database error"}


def test_unreachable_database_file_is_503(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "sql_repo", m.SqlEmployeeRepository(f"sqlite:///{(tmp_path / 'missing_dir' / 'x.db').as_posix()}"))
    c = TestClient(m.app)
    assert c.get("/sql/employees").status_code == 503
    assert c.get("/sql/analytics/department-counts").status_code == 503


def test_schema_is_created_lazily_if_startup_could_not_do_it(monkeypatch, tmp_path):
    """Same lesson as the MongoDB indexes: a failed start-up must not leave the feature broken forever."""
    d = tmp_path / "later"
    monkeypatch.setattr(m, "sql_repo", m.SqlEmployeeRepository(f"sqlite:///{(d / 'x.db').as_posix()}"))
    c = TestClient(m.app)
    assert c.get("/sql/employees").status_code == 503      # directory does not exist yet
    d.mkdir()
    r = c.get("/sql/employees")                            # now it can be created: no restart needed
    assert r.status_code == 200 and r.json()["items"] == []
