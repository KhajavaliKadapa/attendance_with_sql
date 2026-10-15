# REVIEW.md

Defects found in the starter `app/main.py`. Line numbers refer to the original starter file.

**How I found them:** I read the code against `openapi.yaml` and `DATA_MODEL.md`, then ran the original starter on an
in-memory mock database (mongomock) and recorded what it did. "Observed" means I saw it happen. "By inspection" means it
follows from the code but I could not reproduce it (for example races: the mock handles one request at a time).
Nothing was run against a real MongoDB.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` L22 | Client is not timezone-aware, so PyMongo returns naive datetimes that the helpers then mix with aware ones. No timeouts, so an unreachable database blocks each request for the driver default (30 s). | Observed: `compute_overtime` with a naive punch-out raises `TypeError: can't subtract offset-naive and offset-aware datetimes`. Timeout is by inspection. | `tz_aware=True` and `serverSelectionTimeoutMS=3000`. Every datetime in the program is UTC-aware. |
| 2 | `compute_late_minutes` L36-37 | Floors to whole minutes and then tests `> 10`. R2 says late only if strictly more than 10:00 after shift start, so 09:40:01 must count as 10 minutes late and does not. | Observed: punch-in 09:40:01 and 09:40:59 against a 09:30 shift both returned 0 (expected 10). | `seconds > 600`, then `seconds // 60`. |
| 3 | `compute_late_minutes` L35 | The shift start is built from the punch-in's own date and clock (server-local or UTC), not from the IST attendance date. Overnight shifts are never late. | Observed: `04:10Z` (09:40 IST) returned 0. Punch-in 01:00 for a 22:00 shift returned 0 (expected 180). | `attendance_date()` implements R1; late is measured from `shift_start` on that IST date. |
| 4 | `compute_work_hours` L41 | Python `round()` on a float, not half-up (R4). Seconds are not truncated first (R1). | Observed: a 54 second shift returned 0.01; R4 gives 0.02 (0.015 rounds up). | Integer arithmetic in hundredths of an hour, half-up. Instants are truncated to whole seconds. |
| 5 | `compute_overtime` L44-47 | No 30-minute minimum (R3). The shift end is always on `date_str`, so an overnight shift's end falls a day early. Subtracting a naive `punch_out` from an aware end raises. Never called by any endpoint. | Observed: punch-out 18:35 against 18:30 returned 5 (R3: 0). Overnight 06:40 returned 1480 (expected 40). Naive input raised `TypeError`. | `compute_overtime_minutes`: floor minutes, counted only when at least 30; shift end moves to the next day for overnight shifts. |
| 6 | `EmployeeIn` L53-60 | No validation at all: `emp_code` format, email, `HH:MM` shifts, a real `joined_on` date, `shift_start != shift_end`. | Observed: `emp_code "zzz"`, email `"nope"`, `joined_on "2026-02-30"`, shift `"25:99"` returned 201 (contract: 422). | `EmployeeCreate` with patterns, a real-date check and a model validator. |
| 7 | `PunchInIn` L63-66 | `status` is a free string. `punched_at` is a lax int, and the code tests it with `if body.punched_at`, so 0 means "now". | Observed: `status: ABSENT` returned 201; `punched_at: 0` returned 201 dated today; `punched_at: 1783312500` (seconds, not ms) returned 201 dated 1970-01-21. Floats and numeric strings are also coerced. | `Literal` status limited to the three presence statuses. Strict epoch-milliseconds integer with bounds. Test uses `is not None`. |
| 8 | `health` L72-74 | Never touches the database, so it reports healthy when MongoDB is down. | By inspection (there is no database call). | Pings MongoDB, returns 503 on failure. Observed in the fixed app: 503 with no server. |
| 9 | `create_employee` L79-85 | Check-then-insert race: two simultaneous creates can both pass `find_one`. No unique index exists. `datetime.now()` is naive local time. `created_at` is returned as an ISO string, not epoch milliseconds. | Observed: `list_indexes()` shows only `_id_`; `created_at` came back as `'2026-10-10T15:42:10.169625'`. The race is by inspection. | Unique index on `emp_code`, catch `DuplicateKeyError` and return 409. `created_at` is UTC and returned as epoch ms. |
| 10 | `list_employees` L93-96 | `skip = page * page_size` skips the first page. `total` counts the whole collection, ignoring the department filter. No sort, so paging is not stable. No validation of `page`/`page_size`. | Observed: `page=1,page_size=2` returned the 3rd and 4th employees. `department=Sales` reported `total=6` (all employees) although only 2 were Sales. `page=0`, `page_size=0` and `page_size=100000` all returned 200. | `skip = (page-1)*page_size`, `count_documents` with the same filter, sort by `emp_code`, `ge`/`le` bounds (422). |
| 11 | `punch_in` L101, L113 | `emp` can be `None`, then `emp["shift_start"]` raises. | Observed: punch-in for an unknown employee returned 500 (contract: 404). | Explicit 404 before anything else. |
| 12 | `punch_in` L102 | `datetime.fromtimestamp(ms / 1000)` uses the server's local time, keeps fractional seconds, and produces a naive value that MongoDB stores as if it were UTC. | Observed: the same instant prints `04:35` with `TZ=UTC` and `10:05` with `TZ=Asia/Kolkata`, so stored times and lateness depend on the server's time zone. | `ms_to_dt`: UTC-aware, truncated to whole seconds. |
| 13 | `punch_in` L103 | The date is the naive local date, not the IST attendance date (R1). An overnight punch-in after midnight lands on the wrong day. | By inspection (follows from #12 and the code). | `attendance_date(punch_in, shift_start, shift_end)`. |
| 14 | `punch_in` L104-118 | Same check-then-insert race as #9, with no unique `(emp_code, date)` index, so duplicate records for one day are possible under concurrency. | Observed: only `_id_` exists. The race itself is by inspection (the mock cannot interleave requests). | Unique `(emp_code, date)` index; one `insert_one`; `DuplicateKeyError` returns 409. No read-then-write remains. |
| 15 | `punch_in` L119-121 | Response exposes an internal `id` (the ObjectId) which the contract forbids, and returns `punch_in` as a naive ISO string instead of epoch ms. | Observed: response keys include `id`; `punch_in` was `'2026-07-06T04:35:00'`. | `record_to_api()` builds the response from a fixed field list, never includes `_id`, converts instants to epoch ms. |
| 16 | `list_attendance` L144-147 | `list(db.attendance_logs.find(q))` loads every matching document into Python, then sorts and slices there. Memory and time grow with the data (the grader uses about 100k records). | By inspection. | Sort, `skip` and `limit` run in MongoDB, `count_documents` for the total, and an index that provides the sort. |
| 17 | `list_attendance` L145 | Sorts by `date` only. Same-date rows keep storage order instead of `emp_code` ascending. | By inspection (Python's stable sort keeps MongoDB's natural order for ties). | Sort `(date desc, emp_code asc)`; the pair is unique, so paging is stable. |
| 18 | `list_attendance` L127-143 | No validation of dates, status or paging. | Observed: `date_from > date_to`, `date_from=yesterday` and `status=BOGUS` all returned 200 (contract: 422). | Real-date validators, `Literal` status, bounds on `page` and `page_size`. |
| 19 | `list_attendance` L148-149 | Leaks `id`, returns naive ISO datetimes, and passes legacy documents through unchanged. | Observed: a legacy document (sample 9 shape) came back without `half_day`, `history` and `overtime_minutes`. | `record_to_api()` applies the defaults (`false`, `[]`, `0`). |
| 20 | whole file | No index is created anywhere, so every query scans the collection and nothing enforces uniqueness. | Observed: `list_indexes()` shows only `_id_`. | `ensure_indexes()` at startup (see DECISIONS.md for the list). |

**Missing work, not a defect:** L154-162 lists seven endpoints as TODO (punch-out, PATCH correction, four analytics,
explain). They are implemented.

## Looked at and decided not to be defects

- `load_dotenv()` (L20) does not override variables that are already set, so real environment variables win over a
  local `.env`. That is the right order. Kept.
- Handlers are plain `def` and use the synchronous PyMongo client. FastAPI runs them in a thread pool, so they do not
  block the event loop. Kept.
- `IST = timezone(timedelta(hours=5, minutes=30))` (L25) is correct: India has no daylight saving, so a fixed offset is
  exact.
- `doc.pop("_id", None)` after `insert_one` (L84) is right: `insert_one` adds `_id` to the dict, and popping stops it
  leaking. The same idea survives in the fixed code (responses are built from a field list).
- `status_code=201` on both create endpoints matches the contract.
- Filtering `date` with `$gte`/`$lte` on `"YYYY-MM-DD"` strings (L139-141) is correct, because ISO dates sort
  chronologically as strings. Kept, and it is index-friendly.
- `{"_id": 0}` projection in `list_employees` (L95) is correct.
- `int(total_seconds() / 60)` floors for positive values, so the flooring itself is fine; the problems in #2-#5 are the
  threshold, the anchoring and the date logic.
- The fallback `mongodb://localhost:27017` (L22) is a local-development default only; the environment variable takes
  precedence. Kept, but it is a judgement call.
