"""SQL layer on a throw-away SQLite file (fixture isolated_sql in conftest). No MongoDB involved.
Run: pytest tests/test_sql_repository.py"""
import inspect as pyinspect
from datetime import datetime

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

import app.main as m


def E(code, name, dept, joined, email=None, start="09:30", end="18:30"):
    return dict(emp_code=code, name=name, email=email or f"{code.lower()}@example.com", department=dept,
                shift_start=start, shift_end=end, joined_on=joined,
                created_at=datetime(2026, 1, 5, 4, 30, tzinfo=m.UTC))


ASHA = E("EMP0001", "Asha Rao", "Engineering", "2026-01-05")
RAVI = E("EMP0002", "Ravi Kumar", "Engineering", "2026-07-13")
MEERA = E("EMP0003", "Meera Nair", "Engineering", "2026-02-01")
ARUN = E("EMP0004", "Arun Kumar", "Sales", "2026-02-01")
SARA = E("EMP0005", "Sara O'Neil", "Sales", "2026-07-01")
NIGHT = E("EMP0006", "100% Effort_Co", "Support", "2026-03-10", start="22:00", end="06:00")
ALL = [ASHA, RAVI, MEERA, ARUN, SARA, NIGHT]


@pytest.fixture
def repo(isolated_sql):
    return isolated_sql


def put(repo, *emps):
    assert repo.upsert_many([m.mongo_employee_to_sql_row(e) for e in emps]) == []


def codes(rows):
    return [r["emp_code"] for r in rows]


# ----------------------------- initialisation ------------------------------ #
def test_schema_has_the_columns_constraints_and_indexes(repo):
    repo.ensure_schema()
    insp = sa_inspect(repo.engine)
    assert "employees" in insp.get_table_names()
    assert [c["name"] for c in insp.get_columns("employees")] == [
        "id", "emp_code", "name", "email", "department", "joined_on", "shift_start", "shift_end", "created_at"]
    assert any(u["column_names"] == ["emp_code"] for u in insp.get_unique_constraints("employees"))
    assert {"ix_employees_department", "ix_employees_joined_on", "ix_employees_email"} <= {
        i["name"] for i in insp.get_indexes("employees")}


def test_every_data_column_is_not_null(repo):
    repo.ensure_schema()
    cols = {c["name"]: c for c in sa_inspect(repo.engine).get_columns("employees")}
    assert all(cols[n]["nullable"] is False for n in cols if n != "id")
    assert cols["id"]["primary_key"] == 1


def test_initialisation_is_idempotent_and_reusable_on_an_existing_file(repo, tmp_path):
    repo.ensure_schema()
    repo.ensure_schema()
    put(repo, ASHA)
    again = m.SqlEmployeeRepository(str(repo.engine.url))     # a new process opening the same file
    again.ensure_schema()
    assert again.count() == 1
    again.engine.dispose()


def test_nothing_is_created_until_first_use(tmp_path):
    path = tmp_path / "lazy.db"
    r = m.SqlEmployeeRepository(f"sqlite:///{path.as_posix()}")
    assert not path.exists()
    r.ensure_schema()
    assert path.exists()
    r.engine.dispose()


# ----------------------- insertion, retrieval, uniqueness ------------------- #
def test_insert_and_get_roundtrip(repo):
    put(repo, ASHA)
    row = repo.get_by_code("EMP0001")
    assert row == {"emp_code": "EMP0001", "name": "Asha Rao", "email": "emp0001@example.com",
                   "department": "Engineering", "joined_on": "2026-01-05", "shift_start": "09:30",
                   "shift_end": "18:30", "created_at": datetime(2026, 1, 5, 4, 30)}
    assert "id" not in row
    assert repo.get_by_code("EMP9999") is None
    assert m.sql_row_to_api(row)["created_at"] == m.dt_to_ms(datetime(2026, 1, 5, 4, 30, tzinfo=m.UTC))


def test_unique_emp_code_is_enforced_by_the_database(repo):
    repo.ensure_schema()
    row = m.mongo_employee_to_sql_row(ASHA)
    with repo.engine.begin() as conn:
        conn.execute(m._t.insert().values(**row))
    with pytest.raises(IntegrityError):
        with repo.engine.begin() as conn:
            conn.execute(m._t.insert().values(**{**row, "name": "Someone Else"}))   # plain INSERT, same code
    assert repo.count() == 1


def test_upsert_updates_in_place_instead_of_duplicating(repo):
    put(repo, ASHA)
    put(repo, {**ASHA, "name": "Asha R. Rao", "department": "Sales"})
    assert repo.count() == 1
    assert repo.get_by_code("EMP0001")["name"] == "Asha R. Rao" and repo.get_by_code("EMP0001")["department"] == "Sales"


@pytest.mark.parametrize("patch", [
    {"joined_on": "2026-7-1"}, {"joined_on": "yesterday"}, {"shift_start": "9:30"}, {"shift_end": "24:99x"}, {"emp_code": ""},
])
def test_check_constraints_reject_malformed_rows(repo, patch):
    repo.ensure_schema()
    with pytest.raises(IntegrityError):
        repo.upsert_employee(m.mongo_employee_to_sql_row({**ASHA, **patch}))
    assert repo.count() == 0


def test_email_is_deliberately_not_unique(repo):
    put(repo, E("EMP0001", "A", "X", "2026-01-01", email="same@x.io"), E("EMP0002", "B", "X", "2026-01-01", email="same@x.io"))
    assert repo.count() == 2


def test_upsert_many_isolates_one_bad_row(repo):
    rows = [m.mongo_employee_to_sql_row(e) for e in (ASHA, {**RAVI, "joined_on": "bad"}, MEERA)]
    failures = repo.upsert_many(rows)
    assert [c for c, _ in failures] == ["EMP0002"] and "constraint" in failures[0][1].lower()
    assert sorted(repo.all_codes()) == ["EMP0001", "EMP0003"]       # the good rows were not lost


def test_delete_codes_and_get_many_beyond_the_batch_size(repo):
    rows = [m.mongo_employee_to_sql_row(E(f"EMP{i:05d}", f"N{i}", "D", "2026-01-01")) for i in range(1200)]
    assert repo.upsert_many(rows) == []
    assert len(repo.get_many([r["emp_code"] for r in rows])) == 1200           # 3 batches of <= 500
    assert repo.delete_codes([r["emp_code"] for r in rows[:700]]) == 700
    assert repo.count() == 500 and repo.delete_codes([]) == 0


# --------------------------- lookups and pagination ------------------------- #
def test_list_all_is_ordered_and_paginated(repo):
    put(repo, *reversed(ALL))                                                  # inserted in reverse order
    rows, total = repo.list_all(2, 0)
    assert total == 6 and codes(rows) == ["EMP0001", "EMP0002"]
    assert codes(repo.list_all(2, 2)[0]) == ["EMP0003", "EMP0004"] and codes(repo.list_all(2, 4)[0]) == ["EMP0005", "EMP0006"]
    assert repo.list_all(2, 6)[0] == []


def test_by_department_is_exact_and_case_sensitive(repo):
    put(repo, *ALL)
    rows, total = repo.by_department("Engineering", 20, 0)
    assert codes(rows) == ["EMP0001", "EMP0002", "EMP0003"] and total == 3
    assert repo.by_department("engineering", 20, 0) == ([], 0)
    assert repo.by_department("Eng", 20, 0) == ([], 0)
    assert repo.by_department("Engineering", 2, 2) == ([ {**rows[2]} ], 3)      # total ignores paging


# ---------------------------------- search -------------------------------- #
def test_search_matches_name_or_code_case_insensitively(repo):
    put(repo, *ALL)
    assert codes(repo.search("KUMAR", 20, 0)[0]) == ["EMP0002", "EMP0004"]      # name, any case
    assert codes(repo.search("0003", 20, 0)[0]) == ["EMP0003"]                  # employee code
    assert codes(repo.search("emp000", 20, 0)[0]) == [r["emp_code"] for r in ALL]
    assert codes(repo.search("o'neil", 20, 0)[0]) == ["EMP0005"]                # apostrophe is just a character
    assert repo.search("nobody", 20, 0) == ([], 0)
    assert repo.search("Kumar", 1, 1)[1] == 2


@pytest.mark.parametrize("hostile", ["x'; DROP TABLE employees; --", "' OR '1'='1", "\" OR 1=1 --", "a%' UNION SELECT * FROM employees --"])
def test_search_input_is_bound_as_data_never_executed(repo, hostile):
    put(repo, *ALL)
    assert repo.search(hostile, 20, 0) == ([], 0)       # an injection would return rows or drop the table
    assert repo.count() == 6 and "employees" in sa_inspect(repo.engine).get_table_names()


def test_like_wildcards_in_the_search_term_are_literal(repo):
    put(repo, *ALL)
    assert codes(repo.search("%", 20, 0)[0]) == ["EMP0006"]        # only the name that contains a real "%"
    assert codes(repo.search("_", 20, 0)[0]) == ["EMP0006"]        # only the name that contains a real "_"
    assert codes(repo.search("a_u", 20, 0)[0]) == []               # "_" is not "any one character"


def test_repository_builds_no_sql_text_from_strings():
    """Static guard: the repository uses SQLAlchemy expressions and bound parameters only."""
    src = pyinspect.getsource(m.SqlEmployeeRepository)
    for forbidden in ("text(", ".format(", 'f"', "f'", "% ("):
        assert forbidden not in src, f"found {forbidden!r}: build queries with bound parameters"


# ---------------------------------- aggregates ------------------------------ #
def test_department_counts(repo):
    assert repo.department_counts() == []
    put(repo, *ALL)
    assert repo.department_counts() == [{"department": "Engineering", "headcount": 3},
                                        {"department": "Sales", "headcount": 2},
                                        {"department": "Support", "headcount": 1}]


def test_department_summary(repo):
    put(repo, *ALL)
    assert repo.department_summary() == [
        {"department": "Engineering", "headcount": 3, "earliest_joined_on": "2026-01-05", "latest_joined_on": "2026-07-13", "overnight_shift_count": 0},
        {"department": "Sales", "headcount": 2, "earliest_joined_on": "2026-02-01", "latest_joined_on": "2026-07-01", "overnight_shift_count": 0},
        {"department": "Support", "headcount": 1, "earliest_joined_on": "2026-03-10", "latest_joined_on": "2026-03-10", "overnight_shift_count": 1},
    ]


# ------------------------------- date filtering ----------------------------- #
def test_joined_between_is_inclusive_on_both_ends(repo):
    put(repo, *ALL)
    assert codes(repo.joined_between("2026-02-01", "2026-03-10", 20, 0)[0]) == ["EMP0003", "EMP0004", "EMP0006"]  # both boundaries in
    assert codes(repo.joined_between("2026-02-02", "2026-03-09", 20, 0)[0]) == []                                 # just inside: none
    assert codes(repo.joined_between("2026-07-01", "2026-07-01", 20, 0)[0]) == ["EMP0005"]                        # a single day
    assert repo.joined_between("2026-12-01", "2026-12-31", 20, 0) == ([], 0)


def test_joined_between_without_an_upper_bound(repo):
    put(repo, *ALL)
    assert codes(repo.joined_between("2026-07-01", None, 20, 0)[0]) == ["EMP0002", "EMP0005"]


def test_joined_after_is_strict(repo):
    put(repo, *ALL)
    assert codes(repo.joined_after("2026-07-01", 20, 0)[0]) == ["EMP0002"]          # EMP0005 joined ON the date: excluded
    assert codes(repo.joined_after("2026-06-30", 20, 0)[0]) == ["EMP0002", "EMP0005"]
    assert repo.joined_after("2026-07-13", 20, 0) == ([], 0)


def test_duplicate_emails_ignore_case_and_spaces(repo):
    put(repo, E("EMP0001", "A", "X", "2026-01-01", email="Dup@X.io"), E("EMP0002", "B", "X", "2026-01-01", email=" dup@x.io"),
        E("EMP0003", "C", "X", "2026-01-01", email="DUP@x.IO"), E("EMP0004", "D", "X", "2026-01-01", email="solo@x.io"),
        E("EMP0005", "E", "X", "2026-01-01", email="other@x.io"), E("EMP0006", "F", "X", "2026-01-01", email="other@x.io"))
    assert repo.duplicate_emails() == [{"email": "dup@x.io", "count": 3, "emp_codes": ["EMP0001", "EMP0002", "EMP0003"]},
                                       {"email": "other@x.io", "count": 2, "emp_codes": ["EMP0005", "EMP0006"]}]


def test_duplicate_emails_none(repo):
    put(repo, *ALL)
    assert repo.duplicate_emails() == []


# ---------------------------- employees without attendance ------------------ #
def test_without_attendance(repo):
    put(repo, *ALL)
    assert codes(repo.without_attendance(["EMP0001", "EMP0004", "EMP0999"])) == ["EMP0002", "EMP0003", "EMP0005", "EMP0006"]
    assert codes(repo.without_attendance([])) == [r["emp_code"] for r in ALL]          # nobody has attendance
    assert repo.without_attendance([r["emp_code"] for r in ALL]) == []                 # everybody does
    assert codes(repo.without_attendance(["EMP0001", "EMP0001"])) == ["EMP0002", "EMP0003", "EMP0004", "EMP0005", "EMP0006"]


def test_without_attendance_handles_far_more_codes_than_sqlite_bound_variables(repo):
    put(repo, *ALL)
    many = [f"EMP{i:06d}" for i in range(100000, 106000)] + ["EMP0001"]                 # 6001 codes, SQLite's old limit is 999
    assert codes(repo.without_attendance(many)) == ["EMP0002", "EMP0003", "EMP0004", "EMP0005", "EMP0006"]


def test_without_attendance_can_run_repeatedly_and_leaves_nothing_behind(repo):
    put(repo, *ALL)
    for _ in range(3):
        assert len(repo.without_attendance(["EMP0001"])) == 5           # a leftover temp table would raise "already exists"
    assert repo.count() == 6 and sa_inspect(repo.engine).get_table_names() == ["employees"]


def test_equal_start_and_end_counts_as_overnight_like_rule_r1(repo):
    """R1: shift_end <= shift_start is overnight. The API refuses to create 09:00-09:00, but seeded
    MongoDB data could contain it, so the SQL summary must agree with the rule on the boundary."""
    put(repo, E("EMP0001", "A", "Ops", "2026-01-01", start="09:00", end="09:00"),
        E("EMP0002", "B", "Ops", "2026-01-01", start="09:00", end="17:00"),
        E("EMP0003", "C", "Ops", "2026-01-01", start="22:00", end="06:00"))
    assert repo.department_summary()[0]["overnight_shift_count"] == 2


def test_total_counts_all_matches_not_just_the_current_page(repo):
    put(repo, *ALL)
    rows, total = repo.list_all(2, 0)
    assert len(rows) == 2 and total == 6
    rows, total = repo.search("emp000", 2, 2)
    assert len(rows) == 2 and total == 6
    rows, total = repo.by_department("Engineering", 1, 0)
    assert len(rows) == 1 and total == 3


def test_search_ascii_is_case_insensitive_and_non_ascii_letters_match_exactly(repo):
    """Documented limitation: SQLite folds the case of ASCII letters only (no ICU). The important
    property: a name can always be found by typing it exactly as stored."""
    put(repo, E("EMP0001", "Ñandú Pérez", "X", "2026-01-01"), E("EMP0002", "ÉMILE ZOLA", "X", "2026-01-01"))
    assert codes(repo.search("Ñandú Pérez", 20, 0)[0]) == ["EMP0001"]     # its own name, exactly as stored
    assert codes(repo.search("ÉMILE ZOLA", 20, 0)[0]) == ["EMP0002"]
    assert codes(repo.search("pérez", 20, 0)[0]) == ["EMP0001"]           # ASCII letters fold: P/p
    assert codes(repo.search("émile", 20, 0)[0]) == []                    # known limitation: É vs é differ
