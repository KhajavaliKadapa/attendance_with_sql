# Employee Attendance & Analytics API

A FastAPI + MongoDB service for recording employee attendance (punch-in, punch-out, audited manual corrections) and
reporting on it (monthly employee summary, department summary, late leaderboard, department trend). All application code
is in `app/main.py`, in labelled sections: configuration, time helpers, business rules, models, indexes, endpoints,
analytics, explain, and a supplementary SQL employee mirror.

**MongoDB is the source of truth** for employees and the only store for attendance; all 12 original endpoints and every
analytics pipeline use MongoDB alone. In addition, employees are mirrored into a local **SQLite** database (through
SQLAlchemy) so they can be checked with ordinary SQL queries; see "SQL employee mirror" below.

## Prerequisites
- Python 3.11 or newer
- MongoDB 6.0 or newer (7.0 recommended). A local server, Docker, or Atlas free tier all work.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit if your MongoDB is not on localhost
```

### Environment variables
| Variable | Meaning | Example (placeholder) |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |
| `SQL_DATABASE_URL` | Optional. SQLite file for the employee mirror | `sqlite:///attendance_sql.db` (default) |

Real environment variables take precedence over `.env`. Never commit `.env` or real credentials.

### MongoDB
```bash
docker run -d -p 27017:27017 mongo:7        # a local server for development
```
Indexes are created automatically when the app starts. If MongoDB is not up yet at that moment, the app still starts,
`/health` returns 503, and the indexes are created on the first successful `/health` call, so no restart is needed.
Nothing has to be set up by hand.

## Run
```bash
uvicorn app.main:app --port 8000
```
Interactive API documentation: <http://localhost:8000/docs>. The machine-readable contract the app serves is at
<http://localhost:8000/openapi.json>.

## Tests
```bash
pip install -r requirements-dev.txt

python -m pytest tests -q                                   # no database needed; database tests are skipped
MONGO_URI=mongodb://localhost:27017 python -m pytest tests -q -s    # everything, against a real MongoDB
python -m pytest tests -q -m "not integration"              # only the tests that never need a MongoDB server
python -m pytest tests/test_sql_repository.py tests/test_sql_api.py tests/test_sql_sync.py tests/test_existing_api_unchanged.py -q   # just the SQL feature
```
Test files, by kind:
- **Unit (no database):** `test_rules_unit.py` (rules R1-R5, R8, request validation), `test_step5_unit.py` (calendar
  helpers, pipeline structure, reference answers checked by hand), `test_startup.py` (startup and readiness).
- **Model-based (no database):** `test_pipelines_model.py` runs the real pipeline builders and endpoint code on
  `tests/mini_agg.py`, a small executable model of the aggregation language. It is **not MongoDB**; it catches wiring
  mistakes only.
- **SQL (SQLite only, no MongoDB):** `test_sql_repository.py` (schema, constraints, every query, injection safety),
  `test_sql_api.py` (the `/sql` endpoints, status codes, shapes). Each test uses its own temporary SQLite file.
- **Synchronisation:** `test_sql_sync.py` covers what happens when SQL fails and how reconcile repairs it. MongoDB there
  is a mongomock stand-in used **only for basic document operations** (insert, find, upsert, delete); no aggregation
  runs in that file, and it says nothing about a real MongoDB.
- **Regression guard:** `test_existing_api_unchanged.py` compares the OpenAPI of all original routes with a snapshot
  taken before the SQL feature existed (`tests/openapi_original_snapshot.json`).
- **Integration (needs MongoDB):** `test_step3_integration.py`, `test_step4_integration.py`,
  `test_step5_integration.py`, `test_step6_explain.py`. They use a throw-away database and drop it afterwards. They skip
  (never silently pass) when no server answers. The tests marked `real_mongo` seed 100,000 records and check the explain
  plans.
- `USE_MONGOMOCK=1` swaps in `mongomock` for a quick, weaker check of the CRUD tests. It cannot run the analytics
  pipelines or explain, and it does not interleave requests, so it proves little about concurrency.

## SQL employee mirror (supplementary)

### What it is
A second copy of the employee table in SQLite, kept so employees can be checked with SQL. It is **read-only through the
API** (no `/sql` endpoint writes), and MongoDB remains the source of truth. Nothing in the original contract changed;
the OpenAPI of the 12 original operations is identical to before.

### Initialise it
Nothing to install: SQLite ships with Python, and `pip install -r requirements.txt` adds SQLAlchemy.
- The table and indexes are created automatically when the app starts, in the file named by `SQL_DATABASE_URL`
  (default `attendance_sql.db` in the folder you start the app from). If that fails at start-up the app still runs, and
  the table is created on the first `/sql` request, so no restart is needed.
- **Copy existing MongoDB employees into SQL** (first-time migration, or any time they drift apart):
  ```bash
  curl -X POST http://localhost:8000/sql/sync/reconcile
  ```
  PowerShell: `Invoke-RestMethod -Method Post http://localhost:8000/sql/sync/reconcile`.
  It is idempotent: running it twice changes nothing the second time.
- Employees created through `POST /employees` are mirrored automatically.

### Endpoints (all under `/sql`, tagged `sql` in `/docs`)
| Endpoint | What it answers |
|---|---|
| `GET /sql/employees` | All employees (paginated: `page`, `page_size`) |
| `GET /sql/employees/{emp_code}` | One employee by code (404 if unknown) |
| `GET /sql/employees/search?q=...` | Name or employee-code contains the text (`name=` is accepted as an alias) |
| `GET /sql/departments/{department}/employees` | Employees of one department (exact, case-sensitive) |
| `GET /sql/analytics/department-counts` | Headcount per department |
| `GET /sql/analytics/department-summary` | Per department: headcount, earliest/latest `joined_on`, overnight-shift count |
| `GET /sql/analytics/recent-joiners?from_date=...&to_date=...` | Joined between two dates, inclusive (`to_date` optional) |
| `GET /sql/analytics/joined-after?date=...` | Joined strictly after a date |
| `GET /sql/analytics/duplicate-emails` | Emails used by more than one employee (ignoring case and spaces) |
| `GET /sql/employees/without-attendance` | Employees with no attendance record (attendance is read from MongoDB) |
| `GET /sql/sync/status` | MongoDB vs SQL employee counts and number of pending retries |
| `POST /sql/sync/reconcile?prune=false` | Repair SQL from MongoDB (see below) |

List responses use the same envelope as `GET /employees`: `{"items": [...], "total": n, "page": 1, "page_size": 20}`;
employee objects have the same fields as `GET /employees`, with `created_at` in epoch milliseconds.

```bash
curl "http://localhost:8000/sql/employees/search?q=kumar&page_size=1"
# {"items":[{"emp_code":"EMP0002","name":"Ravi Kumar","email":"ravi@example.com","department":"Engineering",
#            "shift_start":"09:30","shift_end":"18:30","joined_on":"2026-07-13","created_at":1783917000000}],
#  "total":1,"page":1,"page_size":1}

curl http://localhost:8000/sql/analytics/department-counts
# {"items":[{"department":"Engineering","headcount":2}]}

curl "http://localhost:8000/sql/analytics/recent-joiners?from_date=2026-07-01"
curl http://localhost:8000/sql/analytics/duplicate-emails
# {"items":[{"email":"meera@example.com","count":2,"emp_codes":["EMP0003","EMP0007"]}]}
```
Invalid input returns 422 (for example a bad date, or `to_date` before `from_date`); an unknown employee code returns 404;
if the SQL database cannot be used, `/sql` endpoints return 503 with `{"detail": "SQL database error"}`.

### The same questions as plain SQL
The API runs parameterised versions of these through SQLAlchemy. You can also run them yourself on the file:
```sql
SELECT department, COUNT(*) FROM employees GROUP BY department ORDER BY department;           -- headcount
SELECT * FROM employees WHERE joined_on BETWEEN '2026-07-01' AND '2026-07-31';               -- joined in July
SELECT * FROM employees WHERE joined_on > '2026-07-01';                                      -- joined after a date
SELECT lower(trim(email)), COUNT(*) FROM employees GROUP BY 1 HAVING COUNT(*) > 1;          -- duplicate emails
SELECT * FROM employees WHERE name LIKE '%kumar%' OR emp_code LIKE '%kumar%';                -- search
```
Without the `sqlite3` tool installed, run them from Python:
`python -c "import sqlite3; print(sqlite3.connect('attendance_sql.db').execute('SELECT department, COUNT(*) FROM employees GROUP BY 1').fetchall())"`

### How synchronisation works, and what it cannot do
1. `POST /employees` commits to **MongoDB first**.
2. Then it upserts the row into SQL (idempotent, keyed by `emp_code`).
3. If step 2 fails for any reason, the request **still returns 201** with the same body as always (MongoDB has the
   employee), the response header says `X-SQL-Sync: pending` instead of `ok`, and a retry record is stored in the MongoDB
   collection `sql_sync_outbox` (one per employee, with the error and an attempt counter).

**Limitations, stated plainly**
- The two databases are **not updated in one transaction**; that is not possible here. For a short time, or after a
  failure, SQL can be behind MongoDB. A crash between steps 1 and 2 leaves a MongoDB employee missing from SQL (and
  possibly without a retry record), which `reconcile` also finds, because it compares every employee.
- Synchronisation is **one way** (MongoDB to SQL) and **manual to repair**: nothing retries in the background. The API has
  no employee-update endpoint, so changes made directly in MongoDB are picked up only by `reconcile`.
- `/sql/sync/status` compares counts only. Only `reconcile` compares every field.
- SQLite has one writer at a time and ignores case for ASCII letters only: a search for `kumar` finds `Kumar`, but
  non-ASCII letters must match exactly. `created_at` is stored in UTC.
- The `employees without attendance` query needs MongoDB (that is where attendance lives), so it fails with 503 if
  MongoDB is down; the other `/sql` queries do not need MongoDB at all.

**Recovery steps**
1. `GET /sql/sync/status`: look at `counts_match` and `pending_retries`.
2. `POST /sql/sync/reconcile`: inserts missing employees, updates changed ones, and clears the retry records of everything
   that is now in sync. Read the response: `failed` lists employees SQL refused (for example a malformed date, which the
   table rejects with a CHECK constraint) with the reason, and their retry records stay. Fix the data in MongoDB and run
   it again.
3. `orphans_in_sql` lists SQL rows that no longer exist in MongoDB. They are only deleted if you pass `?prune=true`.
4. If the SQLite file is damaged or lost, delete it and run `reconcile`: it is rebuilt completely from MongoDB.

## Implemented
All 12 operations of `openapi.yaml` (plus the supplementary `/sql` endpoints above): `GET /health`, `POST /employees`, `GET /employees`, `POST /attendance/punch-in`,
`POST /attendance/punch-out`, `PATCH /attendance/{emp_code}/{date}`, `GET /attendance`,
`GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`,
`GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend`, `GET /admin/explain/{endpoint}`.
The route set, parameter names and request-body presence were compared with the contract mechanically; no differences.
After the SQL feature was added, the OpenAPI output of the 12 original operations was compared with a snapshot taken
before it: identical (`test_existing_api_unchanged.py`).

Design notes:
- **Time:** instants are UTC-aware datetimes inside the program and epoch milliseconds at the API; shift times and
  attendance dates are IST strings. Timestamps are truncated to whole seconds.
- **Concurrency:** punch-in is a unique-index insert; punch-out and corrections are single conditional updates, so the
  database decides every race (the loser gets 409). No transactions are needed.
- **Rounding:** reported numbers are rounded half-up in Python (MongoDB's `$round` rounds half to even).
- **Analytics:** MongoDB aggregation pipelines; the explain endpoint explains the very pipeline each endpoint runs.

## Verification status (please read)
- **Verified by running:** the business rules and request validation (unit tests); the CRUD, punch-in/out and correction
  logic including conflict handling (against mongomock, plus deterministic conflict tests); the pipeline builders and
  endpoint code against the reference answers (on the model above).
- **SQL feature, verified by running:** every query, constraint and endpoint on real SQLite files (including a real
  server start-up with MongoDB down); the failure-handling and reconcile logic, with mongomock used only for basic
  document operations.
- **Not verified:** nothing has been run against a real MongoDB in the environment this was written in. In particular:
  the aggregation pipelines on a real server, the explain plans (`IXSCAN`, no `COLLSCAN`), real concurrency, and
  performance. The tests that check them exist (`pytest -m integration` with `MONGO_URI` set) but have not been run.
- **Open interpretation:** the trend's `moving_avg_7d` averages the unrounded daily rates and rounds only the result.
  If the intended reading is to average the already-rounded 4-decimal rates, last digits can differ.

## Repository layout
```
app/main.py            the whole application
tests/                 tests, a small analytics dataset, an independent Python reference, the aggregation model
requirements.txt       runtime dependencies (FastAPI, PyMongo, SQLAlchemy, ...)
requirements-dev.txt   adds pytest, httpx, mongomock
REVIEW.md              defects found in the starter
DECISIONS.md           design answers
.env.example           placeholder configuration (no secrets)
```
