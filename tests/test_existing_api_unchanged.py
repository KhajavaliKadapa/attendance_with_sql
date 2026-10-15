"""The SQL feature is supplementary: nothing that existed before may have changed.
tests/openapi_original_snapshot.json was captured from the app BEFORE the SQL feature was added."""
import json
import pathlib
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import app.main as m

ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT = json.loads((pathlib.Path(__file__).parent / "openapi_original_snapshot.json").read_text())


def test_openapi_of_every_original_route_is_byte_identical_to_before():
    now = m.app.openapi()
    original_paths = {p: v for p, v in now["paths"].items() if not p.startswith("/sql")}
    assert original_paths == SNAPSHOT["paths"]
    assert now.get("components", {}) == SNAPSHOT["components"]          # no new schemas leaked into the contract


def test_the_twelve_original_operations_are_all_still_there():
    ops = {(meth.upper(), path) for path, item in m.app.openapi()["paths"].items() for meth in item if not path.startswith("/sql")}
    assert len(ops) == 12 and ("POST", "/employees") in ops and ("GET", "/admin/explain/{endpoint}") in ops


def test_every_new_route_lives_under_sql_and_is_tagged():
    new = {p: item for p, item in m.app.openapi()["paths"].items() if p not in SNAPSHOT["paths"]}
    assert new and all(p.startswith("/sql/") for p in new)
    assert all(op["tags"] == ["sql"] for item in new.values() for op in item.values())


def test_importing_the_app_creates_no_database_file(tmp_path):
    code = "import app.main; import os; print(sorted(f for f in os.listdir('.') if f.endswith('.db')))"
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_startup_creates_the_default_database_file_only_when_the_app_starts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "sql_repo", m.SqlEmployeeRepository("sqlite:///attendance_sql.db"))   # the default URL
    monkeypatch.setattr(m, "ensure_indexes", lambda: None)
    with TestClient(m.app):
        pass
    assert (tmp_path / "attendance_sql.db").exists()


def test_a_sql_startup_failure_cannot_stop_the_app(monkeypatch):
    monkeypatch.setattr(m, "ensure_indexes", lambda: None)
    monkeypatch.setattr(m.sql_repo, "ensure_schema", lambda: (_ for _ in ()).throw(OSError("read-only file system")))
    with TestClient(m.app) as c:                  # `with` runs the real lifespan hook
        assert c.get("/openapi.json").status_code == 200


def test_health_does_not_depend_on_sql(monkeypatch):
    class Admin:
        def command(self, name):
            return {"ok": 1}

    class Fake:
        admin = Admin()

    monkeypatch.setattr(m, "client", Fake())
    monkeypatch.setattr(m, "ensure_indexes", lambda: None)
    monkeypatch.setattr(m, "_indexes_ready", False)
    monkeypatch.setattr(m.sql_repo, "ensure_schema", lambda: (_ for _ in ()).throw(OSError("sql is broken")))
    assert TestClient(m.app).get("/health").json() == {"status": "ok"}


def test_gitignore_keeps_database_files_and_secrets_out_of_git():
    patterns = {line.strip() for line in (ROOT / ".gitignore").read_text().splitlines()}
    assert {".env", "*.db", "*.sqlite", "*.sqlite3"} <= patterns


def test_requirements_list_sqlalchemy_and_no_driver_is_needed():
    reqs = (ROOT / "requirements.txt").read_text().lower()
    assert "sqlalchemy>=2" in reqs and "pysqlite" not in reqs      # sqlite3 ships with Python
