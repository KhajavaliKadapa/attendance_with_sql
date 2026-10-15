"""Shared fixtures.

Integration tests use a throw-away database on the server in MONGO_URI (default
mongodb://localhost:27017) and drop it afterwards. If no server answers they are
skipped, never silently passed. USE_MONGOMOCK=1 swaps in mongomock: handy for quick
checks, but it is NOT MongoDB (no real concurrency, no $setWindowFields/$densify,
no explain), so it proves less than a real run.
"""
import os
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import app.main as m

USE_MOCK = os.getenv("USE_MONGOMOCK") == "1"


@pytest.fixture(scope="session")
def mongo_client():
    """One connection check for the whole run (a dead server costs one timeout, not one per test)."""
    if USE_MOCK:
        import mongomock

        return mongomock.MongoClient(tz_aware=True)
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    cl = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True,
                     serverSelectionTimeoutMS=2000)
    try:
        cl.admin.command("ping")
    except PyMongoError:
        pytest.skip("MongoDB not reachable: set MONGO_URI (or USE_MONGOMOCK=1 for a weak check)")
    return cl


@pytest.fixture
def database(monkeypatch, mongo_client):
    cl = mongo_client
    name = f"attendance_test_{uuid.uuid4().hex[:8]}"
    d = cl[name]
    monkeypatch.setattr(m, "client", cl)
    monkeypatch.setattr(m, "db", d)
    m.ensure_indexes()
    yield d
    cl.drop_database(name)


@pytest.fixture
def client(database):
    return TestClient(m.app)  # no `with`: indexes are created by the fixture above


def ist_ms(y, mo, d, h, mi, s=0, ms=0):
    """Epoch milliseconds of an IST wall-clock time."""
    return m.dt_to_ms(datetime(y, mo, d, h, mi, s, tzinfo=m.IST)) + ms


def add_emp(db, code="EMP0001", dept="Engineering", start="09:30", end="18:30", joined="2026-01-05", **extra):
    doc = dict(emp_code=code, name=f"Name {code}", email=f"{code.lower()}@example.com", department=dept,
               shift_start=start, shift_end=end, joined_on=joined,
               created_at=datetime(2026, 1, 5, 4, 30, tzinfo=m.UTC))
    doc.update(extra)
    db.employees.insert_one(doc)
    return doc


def pytest_collection_modifyitems(config, items):
    """Tests marked real_mongo exercise features mongomock cannot run; never run them on it."""
    if USE_MOCK:
        skip = pytest.mark.skip(reason="needs a real MongoDB (USE_MONGOMOCK=1 is active)")
        for item in items:
            if "real_mongo" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_sql(monkeypatch, tmp_path):
    """Every test gets its own temporary SQLite file, so no test ever creates or reuses
    attendance_sql.db, and POST /employees in the older tests mirrors into a throw-away database."""
    repo = m.SqlEmployeeRepository(f"sqlite:///{(tmp_path / 'sql_test.db').as_posix()}")
    monkeypatch.setattr(m, "sql_repo", repo)
    yield repo
    repo.engine.dispose()
