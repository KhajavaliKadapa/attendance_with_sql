"""Startup and readiness: the real lifespan hook and /health (no database needed)."""
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect, OperationFailure, ServerSelectionTimeoutError

import app.main as m


class FakeAdmin:
    def __init__(self, fail=False):
        self.fail, self.pings = fail, 0

    def command(self, name):
        self.pings += 1
        if self.fail:
            raise ServerSelectionTimeoutError("down")
        return {"ok": 1}


class FakeClient:
    def __init__(self, fail=False):
        self.admin = FakeAdmin(fail)


@pytest.fixture(autouse=True)
def fresh_flag(monkeypatch):
    monkeypatch.setattr(m, "_indexes_ready", False)


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(m, "ensure_indexes", lambda: log.append(1))
    return log


def test_indexes_are_created_once_then_never_again(calls):
    for _ in range(5):
        m.ensure_indexes_once()
    assert len(calls) == 1


def test_concurrent_callers_create_indexes_exactly_once(monkeypatch):
    n = []
    monkeypatch.setattr(m, "ensure_indexes", lambda: (time.sleep(0.05), n.append(1)))
    threads = [threading.Thread(target=m.ensure_indexes_once) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(n) == 1


def test_failed_attempt_is_retried_next_time(monkeypatch):
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise AutoReconnect("down")

    monkeypatch.setattr(m, "ensure_indexes", flaky)
    with pytest.raises(AutoReconnect):
        m.ensure_indexes_once()
    m.ensure_indexes_once()            # second call tries again and succeeds
    m.ensure_indexes_once()            # third call is a no-op
    assert len(attempts) == 2 and m._indexes_ready is True


def test_startup_with_mongodb_down_still_starts_and_health_is_503(monkeypatch):
    monkeypatch.setattr(m, "client", FakeClient(fail=True))
    monkeypatch.setattr(m, "ensure_indexes", lambda: (_ for _ in ()).throw(ServerSelectionTimeoutError("down")))
    with TestClient(m.app) as c:       # `with` runs the real lifespan hook
        assert c.get("/health").status_code == 503
    assert m._indexes_ready is False


def test_health_completes_index_creation_once_mongodb_comes_up(monkeypatch, calls):
    """The app started while MongoDB was down; later MongoDB answers: indexes must appear without a restart."""
    monkeypatch.setattr(m, "client", FakeClient(fail=False))
    c = TestClient(m.app)              # no lifespan: nothing was created at startup
    assert calls == []
    assert c.get("/health").json() == {"status": "ok"}
    assert len(calls) == 1
    c.get("/health"); c.get("/health")
    assert len(calls) == 1             # idempotent


def test_health_is_503_and_creates_nothing_while_ping_fails(monkeypatch, calls):
    monkeypatch.setattr(m, "client", FakeClient(fail=True))
    assert TestClient(m.app).get("/health").status_code == 503
    assert calls == []


def test_health_reports_503_when_index_setup_fails_and_retries_later(monkeypatch):
    monkeypatch.setattr(m, "client", FakeClient(fail=False))
    outcomes = [OperationFailure("E11000 duplicate key"), None]

    def ensure():
        o = outcomes.pop(0)
        if o:
            raise o

    monkeypatch.setattr(m, "ensure_indexes", ensure)
    c = TestClient(m.app)
    assert c.get("/health").status_code == 503      # not "ok" while the guarantees are missing
    assert c.get("/health").status_code == 200      # next call succeeds


def test_unexpected_startup_error_is_not_swallowed(monkeypatch):
    """A unique index that cannot be built must stop the app, not be hidden."""
    monkeypatch.setattr(m, "ensure_indexes", lambda: (_ for _ in ()).throw(OperationFailure("duplicate key")))
    with pytest.raises(OperationFailure):
        with TestClient(m.app):
            pass
