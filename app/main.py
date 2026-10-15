"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Everything lives in this one file, in labelled sections:
  1. Configuration and database
  2. Time helpers
  3. Business rules (R1-R5, R8)      <- pure functions, no database access
  4. Request models
  5. Startup: indexes
  6. API helpers (serialisation, validation)
  7. Endpoints
  8. Analytics: pipeline builders and endpoints
  9. Admin: explain
 10. SQL employee mirror (SQLite + SQLAlchemy): supplementary, read-only queries
"""
import calendar
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from sqlalchemy import (
    CheckConstraint, Column, DateTime, Index, Integer, MetaData, String, Table, UniqueConstraint,
    and_, case, create_engine, func, or_, select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from pymongo.errors import AutoReconnect, DuplicateKeyError, PyMongoError, ServerSelectionTimeoutError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration and database
# --------------------------------------------------------------------------- #
# load_dotenv() does not override variables that already exist, so real
# environment variables win over a local .env file.
load_dotenv()

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones, so
# every datetime in this program carries its time zone.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


# --------------------------------------------------------------------------- #
# 2. Time helpers
#    Rule of thumb: datetimes inside the program are UTC-aware; the API speaks
#    epoch milliseconds; shift times and attendance dates are IST strings.
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Bounds from the OpenAPI EpochMillis schema (rejects seconds-looking values).
MS_MIN = 100_000_000_000
MS_MAX = 4_102_444_800_000


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> epoch milliseconds using integer arithmetic (no float error).
    A naive datetime is assumed to be UTC, which is what MongoDB stores."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    """Current time, UTC-aware, truncated to whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift ends on the next day."""
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def round_half_up(value: float, places: int) -> float:
    """R8: round half-up. Python's round() and MongoDB's $round both round half
    to even, so they are not used for reported numbers. Decimal(str(x)) uses the
    shortest repr of the float, i.e. the number as a human would write it."""
    quantum = Decimal(1).scaleb(-places)
    # round(.., 12) first strips float noise from sums and divisions: (8.00 + 8.01) / 2
    # is 8.004999999999999 in floating point but is an exact tie (8.005) in real numbers.
    # 12 places is far below any real tie-adjacent gap and far above double-precision noise.
    return float(Decimal(str(round(value, 12))).quantize(quantum, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# 3. Business rules (pure functions: easy to unit test)
#    Every function takes UTC-aware datetimes that are already whole seconds.
# --------------------------------------------------------------------------- #
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
LATE_GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_BELOW_HUNDREDTHS = 450  # R5: rounded work_hours < 4.50


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to.
    For an overnight shift, a punch-in earlier than shift_end (in IST) belongs
    to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    """The instant the shift of attendance day `date_str` starts."""
    d = date.fromisoformat(date_str)
    return datetime.combine(d, parse_hhmm(shift_start), tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The instant the shift of attendance day `date_str` ends (next day for overnight)."""
    d = date.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        d += timedelta(days=1)
    return datetime.combine(d, parse_hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then the
    whole minutes (floor) since shift_start, not since the end of the grace period."""
    seconds = int((punch_in - shift_start_dt(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > LATE_GRACE_SECONDS else 0


def compute_overtime_minutes(
    punch_out: datetime, date_str: str, shift_start: str, shift_end: str
) -> int:
    """R3: whole minutes (floor) after shift_end, counted only if >= 30."""
    seconds = int((punch_out - shift_end_dt(date_str, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60  # floor; a negative value (left early) is below 30 anyway
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def work_hours_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    """R4 in integer hundredths of an hour, rounded half-up.
    hours*100 = seconds*100/3600; adding half (3600/2) before flooring is half-up.
    Integer maths means there is no float rounding to go wrong."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (seconds * 200 + 3600) // 7200


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return work_hours_hundredths(punch_in, punch_out) / 100


def compute_half_day(punch_in: datetime, punch_out: datetime) -> bool:
    """R5: decided on the ROUNDED work hours (4.495h rounds to 4.50 -> not a half day)."""
    return work_hours_hundredths(punch_in, punch_out) < HALF_DAY_BELOW_HUNDREDTHS


# --------------------------------------------------------------------------- #
# 4. Request models (strict: the contract says bad input is a 422, never coerced)
# --------------------------------------------------------------------------- #
# strict=True: rejects floats (even 1.0), strings ("123") and booleans.
EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # rejects 2026-02-30; ValueError becomes a 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    # Extra keys (e.g. a client sending work_hours) are ignored, as the contract says.
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 5. Startup: indexes (idempotent: create_index is a no-op if it already exists)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # emp_code uniqueness is enforced by the database, not by check-then-insert.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    # GET /employees: filter by department, sorted by emp_code.
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code"
    )
    # Analytics: headcount = employees with joined_on <= end of period (R9), per department
    # or for all of them. Without these the first $match of department_summary and the
    # department filter of department_trend would scan the whole collection.
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on"
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")

    # One record per (employee, date): this is what makes punch-in race-safe.
    # It also serves per-employee lists (equality on emp_code, date read in order)
    # and the monthly employee summary.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique"
    )
    # GET /attendance default sort (date desc, emp_code asc) and month-wide scans.
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp"
    )
    # Punch-out: latest record of an employee with punch_in <= t.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in_desc"
    )


_index_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes_once() -> None:
    """Create the indexes the first time it succeeds; a no-op afterwards.
    Safe to call from many threads. It is called at startup and again from /health, so an
    app that started before MongoDB was up still gets its unique indexes (and with them the
    race guarantees) as soon as the database answers, without a restart."""
    global _indexes_ready
    if _indexes_ready:
        return
    with _index_lock:
        if not _indexes_ready:
            ensure_indexes()
            _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes_once()
    except (ServerSelectionTimeoutError, AutoReconnect) as exc:
        # MongoDB is not up yet: start anyway; /health reports 503 and finishes the job later.
        # Any other error (for example a unique index that cannot be built) is NOT swallowed.
        log.warning("MongoDB not reachable at startup, indexes will be created later: %s", exc)
    try:
        sql_repo.ensure_schema()
    except Exception as exc:  # noqa: BLE001 - the SQL mirror is supplementary; it must never stop the app
        log.error("SQL schema setup failed (the /sql endpoints will return 503 until it works): %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 6. API helpers
# --------------------------------------------------------------------------- #
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


def _check_date(v):
    if v is not None:
        date.fromisoformat(v)  # rejects 2026-02-30; the ValueError becomes a 422
    return v


# A YYYY-MM-DD query parameter that must be a real calendar date.
OptDate = Annotated[Optional[str], Query(pattern=DATE_RE), AfterValidator(_check_date)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]  # R10


def unprocessable(loc: tuple, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's standard body, for checks that need more than one field."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def now_ms_precision() -> datetime:
    """Current UTC time truncated to milliseconds (BSON dates have ms precision, so
    the value we return is exactly the value that is stored)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def _api_value(v):
    """Datetime -> epoch ms; everything else unchanged (used inside history.changes)."""
    return dt_to_ms(v) if isinstance(v, datetime) else v


def employee_to_api(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(created) if created else None,
    }


def history_to_api(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": {
            field: {"from": _api_value(c.get("from")), "to": _api_value(c.get("to"))}
            for field, c in (entry.get("changes") or {}).items()
        },
    }


def record_to_api(doc: dict) -> dict:
    """Stored attendance document -> API record. Never exposes _id. Legacy documents
    may lack half_day / history / the derived numbers: missing means 0 / false / []."""
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(pin) if pin else None,
        "punch_out": dt_to_ms(pout) if pout else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_to_api(h) for h in (doc.get("history") or [])],
    }


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, __: PyMongoError):
    # A database failure is "service unavailable", not an unhandled 500.
    return JSONResponse(status_code=503, content={"detail": "database error"})


@app.exception_handler(SQLAlchemyError)
async def sql_error_handler(_: Request, __: SQLAlchemyError):
    # Only the supplementary /sql endpoints can raise this; the original endpoints never touch SQL
    # in a way that can fail (the sync hook catches everything).
    return JSONResponse(status_code=503, content={"detail": "SQL database error"})


# --------------------------------------------------------------------------- #
# 7. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes_once()  # no-op once done; completes a startup that happened before MongoDB was up
    except PyMongoError:
        log.exception("health check failed")
        raise HTTPException(503, "MongoDB is not reachable or index setup failed")
    return {"status": "ok"}


# ----------------------------- employees ---------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate, response: Response):
    doc = body.model_dump()
    doc["created_at"] = now_ms_precision()
    try:
        # No "find first": the unique index on emp_code decides, so concurrent
        # creates of the same code give exactly one 201 and the rest 409.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    # MongoDB is the source of truth and has already committed. Mirroring into SQL is best
    # effort and NEVER changes the status or body of this response; its outcome is only
    # reported in a header ("ok" or "pending", see section 10).
    response.headers["X-SQL-Sync"] = sync_employee_to_sql(doc)
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)  # same filter as the page: R10
    items = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_to_api(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ----------------------------- attendance --------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1}
    )
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()  # R1: whole seconds
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])  # R1
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),  # R2
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Atomic: the unique (emp_code, date) index lets exactly one insert win.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_api(doc)


StatusName = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]


def attendance_list_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    """The one definition of the list query (filter, sort, paging). The endpoint runs it and
    /admin/explain explains it, so the two cannot drift apart."""
    if date_from and date_to and date_from > date_to:  # ISO dates compare correctly as strings
        raise unprocessable(("query", "date_from"), "date_from must not be after date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return {
        "filter": q,
        # Always (date desc, emp_code asc): date_desc_emp provides this order for ANY filter,
        # so an index plan always exists and no in-memory sort is needed. (emp_code, date) is
        # unique, so the order is total and paging is stable.
        "sort": {"date": DESCENDING, "emp_code": ASCENDING},
        "projection": {"_id": 0},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: OptDate = None,
    date_to: OptDate = None,
    status: Optional[StatusName] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
    q = spec["filter"]
    # With no filter the total is the collection size: use the O(1) metadata count
    # instead of scanning 100k documents. With filters count_documents uses an index.
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    items = (
        db.attendance_logs.find(q, spec["projection"])
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
    )
    return {
        "items": [record_to_api(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


MAX_PUNCH_SPAN = timedelta(hours=24)  # a punch-out may be at most 24h after the punch-in
DateParam = Annotated[str, Path(pattern=DATE_RE), AfterValidator(_check_date)]


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1}
    )
    if emp is None:
        raise HTTPException(404, "employee not found")

    t = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # The employee's most recent record whose punch_in is at or before t. Looking it
    # up by instant (not by calendar date) is what makes overnight shifts work.
    # {"$lte": datetime} never matches null/missing, so ABSENT/LEAVE records are skipped.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": t}},
        {"_id": 0},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pin = rec["punch_in"]
    if t <= pin:
        raise unprocessable(("body", "punched_at"), "punched_at must be after the punch-in")
    if t - pin > MAX_PUNCH_SPAN:
        raise unprocessable(("body", "punched_at"), "punched_at is more than 24 hours after the punch-in")

    values = {
        "punch_out": t,
        "work_hours": compute_work_hours(pin, t),  # R4
        "overtime_minutes": compute_overtime_minutes(t, rec["date"], emp["shift_start"], emp["shift_end"]),  # R3
        "half_day": compute_half_day(pin, t),  # R5
    }
    # Atomic compare-and-set: the update only matches while the record is still open
    # (punch_out is null) and its punch_in is the one we computed from. Of two
    # simultaneous punch-outs exactly one matches; the other gets None -> 409.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": rec["date"], "punch_in": pin, "punch_out": None},
        {"$set": values},
        return_document=ReturnDocument.AFTER,  # record_to_api() never exposes _id
    )
    if updated is None:
        raise HTTPException(409, "already punched out or modified concurrently")
    return record_to_api(updated)


HISTORY_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def derived_values(status: str, pin, pout, day: str, shift_start: str, shift_end: str) -> dict:
    """The four derived fields for a final state (DATA_MODEL.md table)."""
    if status not in PRESENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {"late_minutes": compute_late_minutes(pin, day, shift_start)}
    if pout is None:
        out.update(work_hours=None, overtime_minutes=0, half_day=False)
    else:
        out.update(
            work_hours=compute_work_hours(pin, pout),
            overtime_minutes=compute_overtime_minutes(pout, day, shift_start, shift_end),
            half_day=compute_half_day(pin, pout),
        )
    return out


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateParam, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # ---- 1. the final state the record would be in ----
    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    if new_status in PRESENCE_STATUSES:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:  # R1
            raise unprocessable(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + MAX_PUNCH_SPAN):
            raise unprocessable(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")
    else:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(("body", "status"), f"punch times cannot be supplied with status {new_status}")
        new_in = new_out = None

    # "Changes nothing" means none of the three editable fields differs.
    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise unprocessable(("body",), "the request changes nothing")

    # ---- 2. recompute derived fields (R2-R5) and diff old vs new ----
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new.update(derived_values(new_status, new_in, new_out, date, emp["shift_start"], emp["shift_end"]))
    old = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,        # missing means 0
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),         # missing means false
    }
    changes = {f: {"from": old[f], "to": new[f]} for f in HISTORY_FIELDS if old[f] != new[f]}
    entry = {"at": now_ms_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # ---- 3. one atomic, guarded write ----
    # The filter pins everything we read: the editable fields and the history length.
    # If anything touched the record since (another PATCH, or a punch-out), nothing
    # matches and the loser gets 409, so no history entry is ever lost or duplicated.
    guard = {"emp_code": emp_code, "date": date, "status": old_status, "punch_in": old_in, "punch_out": old_out}
    old_history = rec.get("history")
    if old_history is None:  # legacy record: the field is missing (or null)
        guard["history"] = None
        update = {"$set": {**new, "history": [entry]}}
    else:
        guard["history"] = {"$size": len(old_history)}
        update = {"$set": new, "$push": {"history": entry}}
    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    return record_to_api(updated)


# --------------------------------------------------------------------------- #
# 8. Analytics
#    Each endpoint has a pipeline BUILDER (a pure function returning a list of
#    stages). The endpoint runs it, and /admin/explain reuses the same builder, so
#    what is explained is exactly what is served. Dates are IST strings, so month
#    and range filters are plain string comparisons that an index can use.
# --------------------------------------------------------------------------- #
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"


def _check_month(v):
    if v is not None:
        date.fromisoformat(v + "-01")  # rejects year 0000
    return v


MonthParam = Annotated[str, Query(pattern=MONTH_RE), AfterValidator(_check_month)]
OptMonth = Annotated[Optional[str], Query(pattern=MONTH_RE), AfterValidator(_check_month)]


def month_bounds(month: str) -> tuple[str, str]:
    """('2026-07-01', '2026-07-31') for '2026-07'."""
    year, mon = int(month[:4]), int(month[5:])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def count_weekdays(start: date, end: date) -> int:
    """Number of Mon-Fri days in [start, end] inclusive (R7: no holiday calendar)."""
    if end < start:
        return 0
    total = (end - start).days + 1
    weeks, extra = divmod(total, 7)
    count = weeks * 5
    first_partial = start + timedelta(days=weeks * 7)
    count += sum(1 for i in range(extra) if (first_partial + timedelta(days=i)).weekday() < 5)
    return count


def working_days(month: str, joined_on: str) -> int:
    """R7: Mon-Fri days of the month, counted from joined_on for a mid-month joiner."""
    first, last = month_bounds(month)
    start = max(date.fromisoformat(first), date.fromisoformat(joined_on))
    return count_weekdays(start, date.fromisoformat(last))


# --- small expression builders; `p` is a field prefix such as "log." inside a nested document ---
def _is_present(p: str = "") -> dict:  # R6
    return {"$in": [f"${p}status", list(PRESENCE_STATUSES)]}


def _is_late(p: str = "") -> dict:  # late_minutes > 0 (missing counts as 0)
    return {"$gt": [{"$ifNull": [f"${p}late_minutes", 0]}, 0]}


def _presence_weight(p: str = "") -> dict:  # present day = 1, half day = 0.5, otherwise 0
    return {"$cond": [_is_present(p), {"$cond": [{"$eq": [f"${p}half_day", True]}, 0.5, 1]}, 0]}


# Mon-Fri test from the stored IST date string. dateFromString reads it as UTC midnight,
# which has the same weekday as the IST date, so no time zone is involved.
_IS_WEEKDAY = {
    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]
}
_HAS_WORK_HOURS = {"$and": [_is_present(), {"$isNumber": "$work_hours"}]}


def _ones(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


# ----------------------------- employee monthly ---------------------------- #
def employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    """On attendance_logs. $match uses the unique (emp_code, date) index; $group folds the month."""
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [_IS_WEEKDAY, _presence_weight(), 0]}},  # R5-R7
                "leave_days": _ones({"$eq": ["$status", "LEAVE"]}),
                "late_count": _ones(_is_late()),
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    stats = next(iter(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last))), {})
    wd = working_days(month, emp["joined_on"])
    present = stats.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": round_half_up(present, 2),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(present / wd * 100, 2) if wd else None,  # R8
    }


# ---------------------------- department summary --------------------------- #
def department_summary_pipeline(first: str, last: str, department: Optional[str] = None) -> list:
    """On employees (not on logs), so employees with zero logs are in the headcount by
    construction. The $lookup joins on emp_code (localField/foreignField) and its sub-pipeline
    filters the month, so it can use the (emp_code, date) index; it folds each employee's month into one small
    document, and the outer $group sums those per department."""
    match: dict = {"joined_on": {"$lte": last}}  # R9
    if department is not None:
        match["department"] = department
    month_of_employee = [
        # the join (emp_code equality) and this range become one (emp_code, date) index lookup
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [_IS_WEEKDAY, _presence_weight(), 0]}},
            "wh_sum": {"$sum": {"$cond": [_HAS_WORK_HOURS, "$work_hours", 0]}},
            "wh_n": _ones(_HAS_WORK_HOURS),
            "late_count": _ones(_is_late()),
            "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": _ones({"$eq": ["$status", "LEAVE"]}),
            "on_duty_count": _ones({"$eq": ["$status", "ON_DUTY"]}),
        }},
    ]

    def total(field: str) -> dict:  # sum a field over the 0-or-1 documents in "s"
        return {"$sum": {"$sum": f"$s.{field}"}}

    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": month_of_employee, "as": "s"}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": total("present_days"),
            "wh_sum": total("wh_sum"),
            "wh_n": total("wh_n"),
            "late_count": total("late_count"),
            "late_minutes": total("late_minutes"),
            "leave_count": total("leave_count"),
            "on_duty_count": total("on_duty_count"),
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for g in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        n = g["wh_n"]
        items.append({
            "department": g["_id"],
            "headcount": int(g["headcount"]),
            "present_days": round_half_up(g["present_days"], 2),
            # mean over RECORDS (sum of hours / number of records), not a mean of per-employee means
            "avg_work_hours": round_half_up(g["wh_sum"] / n, 2) if n else None,
            "late_count": int(g["late_count"]),
            "total_late_minutes": int(g["late_minutes"]),
            "leave_count": int(g["leave_count"]),
            "on_duty_count": int(g["on_duty_count"]),
        })
    return {"month": month, "items": items}


# ------------------------------ late leaderboard --------------------------- #
def late_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str] = None) -> list:
    """On attendance_logs. The month filter is a range on `date` (index date_desc_emp).
    Rank comes from $setWindowFields/$rank, which is standard competition ranking
    (1, 2, 2, 4), and `limit` is applied to the rank, never to the row position."""
    stages: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": _ones(_is_late()),
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees have no match and are dropped
    ]
    if department is not None:
        stages.append({"$match": {"e.department": department}})  # BEFORE ranking: rank inside the department
    stages += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return stages


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthParam,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department))
    return {
        "month": month,
        "items": [
            {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
             "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
            for r in rows
        ],
    }


# ------------------------------- department trend -------------------------- #
MAX_TREND_DAYS = 92


def department_trend_pipeline(department: str, date_from: str, date_to: str) -> list:
    """On employees. Returns exactly one row per calendar day in [date_from, date_to].
    1. employees of the department -> $lookup their logs in range (indexed on emp_code, date)
    2. $unwind with preserveNullAndEmptyArrays keeps employees without logs, so there is
       always at least one row; $group by date folds the counts per day
    3. one document holds the per-day counts; $range + $map then generates ONE entry per
       calendar day (so empty days and weekends exist) and merges the counts in
    4. $unwind, then $setWindowFields computes the 7-row moving average."""
    d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
    start = datetime.combine(d0, dtime(0, 0), tzinfo=UTC)
    n_days = (d1 - d0).days + 1

    per_day = {"$let": {
        "vars": {"dt": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}},
        "in": {"$let": {
            "vars": {
                "ds": {"$dateToString": {"date": "$$dt", "format": "%Y-%m-%d"}},
                "wd": {"$lte": [{"$isoDayOfWeek": "$$dt"}, 5]},  # Mon-Fri
            },
            "in": {"$let": {
                "vars": {
                    "row": {"$arrayElemAt": [
                        {"$filter": {"input": "$rows", "as": "r", "cond": {"$eq": ["$$r.date", "$$ds"]}}}, 0]},
                    # R9: employees whose joined_on <= this day
                    "hc": {"$reduce": {
                        "input": "$joins", "initialValue": 0,
                        "in": {"$cond": [{"$lte": ["$$this._id", "$$ds"]}, {"$add": ["$$value", "$$this.n"]}, "$$value"]},
                    }},
                },
                "in": {"$let": {
                    "vars": {"pc": {"$ifNull": ["$$row.present", 0]}, "lc": {"$ifNull": ["$$row.late", 0]}},
                    "in": {
                        "date": "$$ds",
                        "is_working_day": "$$wd",
                        "headcount": "$$hc",
                        "present_count": "$$pc",
                        "late_count": "$$lc",
                        # only on working days with headcount > 0; otherwise null
                        "attendance_rate": {"$cond": [
                            {"$and": ["$$wd", {"$gt": ["$$hc", 0]}]}, {"$divide": ["$$pc", "$$hc"]}, None]},
                    },
                }},
            }},
        }},
    }}

    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "log",
        }},
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$log.date",  # null for employees without logs; never equals a day string
            "present": {"$sum": _presence_weight("log.")},
            "late": _ones(_is_late("log.")),
        }},
        {"$group": {"_id": None, "rows": {"$push": {"date": "$_id", "present": "$present", "late": "$late"}}}},
        # headcount inputs: how many employees joined on each date (uncorrelated, runs once)
        {"$lookup": {"from": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$group": {"_id": "$joined_on", "n": {"$sum": 1}}},
        ], "as": "joins"}},
        {"$project": {"_id": 0, "days": {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": per_day}}}},
        {"$unwind": "$days"},
        {"$replaceRoot": {"newRoot": "$days"}},
        # current row + up to 6 preceding rows inside the range; $avg ignores null rates
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
    ]


def validate_trend_range(date_from: str, date_to: str) -> None:
    if date_to < date_from:
        raise unprocessable(("query", "to"), "to must not be before from")
    if (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[str, Query(alias="from", pattern=DATE_RE), AfterValidator(_check_date)],
    date_to: Annotated[str, Query(alias="to", pattern=DATE_RE), AfterValidator(_check_date)],
):
    validate_trend_range(date_from, date_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")

    items = []
    for r in db.employees.aggregate(department_trend_pipeline(department, date_from, date_to)):
        rate, avg = r.get("attendance_rate"), r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if rate is None else round_half_up(rate, 4),  # R8
            "moving_avg_7d": None if avg is None else round_half_up(avg, 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 9. Admin: explain
#    Explains the SAME query/pipeline an endpoint runs, built by the same builder
#    function (attendance_list_spec, *_pipeline), with executionStats verbosity.
# --------------------------------------------------------------------------- #
ExplainEndpoint = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def _required(name: str, value):
    if value is None:
        raise unprocessable(("query", name), "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: OptMonth = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: OptDate = None,
    date_to: OptDate = None,
    status: Optional[StatusName] = None,
    # `from` and `to` are Python keywords, so they are read through aliases
    range_from: Annotated[Optional[str], Query(alias="from", pattern=DATE_RE), AfterValidator(_check_date)] = None,
    range_to: Annotated[Optional[str], Query(alias="to", pattern=DATE_RE), AfterValidator(_check_date)] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, **spec}  # "find" must be the first key
    else:
        if endpoint == "employee_monthly":
            first, last = month_bounds(_required("month", month))
            collection = "attendance_logs"
            pipeline = employee_monthly_pipeline(_required("emp_code", emp_code), first, last)
        elif endpoint == "department_summary":
            first, last = month_bounds(_required("month", month))
            collection = "employees"
            pipeline = department_summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            first, last = month_bounds(_required("month", month))
            collection = "attendance_logs"
            pipeline = late_leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            dept = _required("department", department)
            d_from, d_to = _required("from", range_from), _required("to", range_to)
            validate_trend_range(d_from, d_to)
            collection = "employees"
            pipeline = department_trend_pipeline(dept, d_from, d_to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    # The explain document holds BSON types (Int64, dates, ObjectId...): convert to plain JSON.
    plain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": plain}


# --------------------------------------------------------------------------- #
# 10. SQL employee mirror (SQLite via SQLAlchemy)
#
#   SUPPLEMENTARY. MongoDB stays the source of truth for employees and the ONLY store for
#   attendance; every original endpoint and all analytics pipelines still use MongoDB alone.
#   This section keeps a read-only SQL copy of the employee table so it can be checked with
#   ordinary SQL queries.
#
#   Synchronisation is one-way (MongoDB -> SQL) and is NOT one atomic transaction across the
#   two databases; that is impossible here. Order: (1) MongoDB commits, (2) the row is
#   upserted into SQL. If (2) fails, a retry record is written to the MongoDB collection
#   `sql_sync_outbox` and the original request still succeeds (header X-SQL-Sync: pending).
#   POST /sql/sync/reconcile repairs everything: it compares every employee, upserts what is
#   missing or different, and clears the retry records. The upsert is idempotent, so running
#   it any number of times is safe. All SQL queries use bound parameters; no SQL text is ever
#   built from user input.
# --------------------------------------------------------------------------- #
SQL_DATABASE_URL = os.getenv("SQL_DATABASE_URL", "sqlite:///attendance_sql.db")

sql_metadata = MetaData()
sql_employees = Table(
    "employees",
    sql_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),  # internal; never returned by the API
    Column("emp_code", String(20), nullable=False),
    Column("name", String(100), nullable=False),
    Column("email", String(120), nullable=False),  # deliberately NOT unique: duplicates must be findable
    Column("department", String(50), nullable=False),
    Column("joined_on", String(10), nullable=False),  # "YYYY-MM-DD", an IST date, same as MongoDB
    Column("shift_start", String(5), nullable=False),  # "HH:MM"
    Column("shift_end", String(5), nullable=False),
    Column("created_at", DateTime, nullable=False),  # UTC (SQLite has no time zones; stored naive)
    UniqueConstraint("emp_code", name="uq_employees_emp_code"),
    CheckConstraint("length(emp_code) > 0", name="ck_employees_emp_code_not_empty"),
    CheckConstraint("joined_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", name="ck_employees_joined_on_format"),
    CheckConstraint(
        "shift_start GLOB '[0-2][0-9]:[0-5][0-9]' AND shift_end GLOB '[0-2][0-9]:[0-5][0-9]'",
        name="ck_employees_shift_format",
    ),
    Index("ix_employees_department", "department"),
    Index("ix_employees_joined_on", "joined_on"),
    Index("ix_employees_email", "email"),
)
_t = sql_employees
SQL_UPDATABLE = ("name", "email", "department", "joined_on", "shift_start", "shift_end", "created_at")
_OUT_COLUMNS = [c for c in _t.c if c.name != "id"]  # what queries return (no internal id)


def mongo_employee_to_sql_row(doc: dict) -> dict:
    """A MongoDB employee document as a SQL row. created_at becomes naive UTC; a document
    without one gets a fixed fallback (the epoch) so reconciling it twice stays a no-op."""
    created = doc.get("created_at") or EPOCH
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "joined_on": doc["joined_on"],
        "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
        "created_at": created.astimezone(UTC).replace(tzinfo=None),
    }


def sql_row_to_api(row: dict) -> dict:
    """Same shape as employee_to_api (the MongoDB-backed /employees)."""
    return {**{k: row[k] for k in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")},
            "created_at": dt_to_ms(row["created_at"])}


class SqlEmployeeRepository:
    """Every SQL query the feature needs, as parameterised SQLAlchemy Core statements.
    Methods return plain dicts/lists. Self-contained on purpose, so it can be moved to its own
    module unchanged if the one-file rule of the assignment is ever lifted."""

    def __init__(self, url: str):
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 10}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, **kwargs)  # lazy: no file is touched until first use
        self._ready = False
        self._lock = threading.Lock()

    # ---- schema ----
    def ensure_schema(self) -> None:
        """Create the table and indexes if missing (idempotent, thread-safe, retried on the
        next call if it failed)."""
        if self._ready:
            return
        with self._lock:
            if not self._ready:
                sql_metadata.create_all(self.engine)
                self._ready = True

    # ---- helpers ----
    def _page(self, where, limit: int, offset: int):
        self.ensure_schema()
        q = select(*_OUT_COLUMNS).order_by(_t.c.emp_code).limit(limit).offset(offset)
        c = select(func.count()).select_from(_t)
        if where is not None:
            q, c = q.where(where), c.where(where)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()], conn.execute(c).scalar_one()

    # ---- 1-4, 6-7: lookups and searches ----
    def get_by_code(self, emp_code: str):  # 1
        self.ensure_schema()
        with self.engine.connect() as conn:
            row = conn.execute(select(*_OUT_COLUMNS).where(_t.c.emp_code == emp_code)).mappings().first()
        return dict(row) if row else None

    def list_all(self, limit: int, offset: int):  # 2
        return self._page(None, limit, offset)

    def by_department(self, department: str, limit: int, offset: int):  # 3 (exact match, case-sensitive like MongoDB)
        return self._page(_t.c.department == department, limit, offset)

    def search(self, term: str, limit: int, offset: int):  # 4 (substring of name or code)
        # SQLite's LIKE ignores case for ASCII letters on its own. Do NOT lower() either side: SQLite's
        # lower() only folds ASCII, so lower-casing the term in Python would make a name such as
        # "Ñandú" impossible to find by typing it. Non-ASCII letters therefore match with exact case.
        # autoescape=True makes "%" and "_" in the user's text literal characters, not wildcards.
        where = or_(_t.c.name.contains(term, autoescape=True), _t.c.emp_code.contains(term, autoescape=True))
        return self._page(where, limit, offset)

    def joined_between(self, date_from: str, date_to, limit: int, offset: int):  # 6 (inclusive; to is optional)
        where = _t.c.joined_on >= date_from
        if date_to is not None:
            where = and_(where, _t.c.joined_on <= date_to)
        return self._page(where, limit, offset)

    def joined_after(self, after: str, limit: int, offset: int):  # 7 (strictly after)
        return self._page(_t.c.joined_on > after, limit, offset)

    # ---- 5, 8, 10: aggregates ----
    def department_counts(self):  # 5
        self.ensure_schema()
        q = select(_t.c.department, func.count().label("headcount")).group_by(_t.c.department).order_by(_t.c.department)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def duplicate_emails(self):  # 8 (same mailbox ignoring case and surrounding spaces)
        self.ensure_schema()
        key = func.lower(func.trim(_t.c.email))
        q = (select(key.label("email"), func.count().label("count"), func.group_concat(_t.c.emp_code).label("codes"))
             .group_by(key).having(func.count() > 1).order_by(key))
        with self.engine.connect() as conn:
            rows = conn.execute(q).mappings().all()
        return [{"email": r["email"], "count": r["count"], "emp_codes": sorted(r["codes"].split(","))} for r in rows]

    def department_summary(self):  # 10
        self.ensure_schema()
        overnight = func.sum(case((_t.c.shift_end <= _t.c.shift_start, 1), else_=0))  # R1: end <= start is overnight
        q = (select(_t.c.department, func.count().label("headcount"),
                    func.min(_t.c.joined_on).label("earliest_joined_on"),
                    func.max(_t.c.joined_on).label("latest_joined_on"),
                    overnight.label("overnight_shift_count"))
             .group_by(_t.c.department).order_by(_t.c.department))
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # ---- 9: employees with no attendance ----
    def without_attendance(self, codes_with_attendance):
        """Attendance lives in MongoDB, not SQL, so the caller passes the employee codes that
        HAVE attendance. They go into a TEMPORARY table (one connection, dropped afterwards) and
        an anti-join finds everyone else. A temp table has no limit on the number of codes,
        unlike `NOT IN (?, ?, ...)`, which hits SQLite's bound-variable limit."""
        self.ensure_schema()
        tmp = Table("tmp_codes_with_attendance", MetaData(), Column("emp_code", String(20), primary_key=True),
                    prefixes=["TEMPORARY"])
        with self.engine.connect() as conn:
            tmp.create(conn)
            try:
                codes = sorted(set(codes_with_attendance))
                for i in range(0, len(codes), 5000):
                    conn.execute(tmp.insert(), [{"emp_code": c} for c in codes[i:i + 5000]])
                q = (select(*_OUT_COLUMNS).select_from(_t.outerjoin(tmp, _t.c.emp_code == tmp.c.emp_code))
                     .where(tmp.c.emp_code.is_(None)).order_by(_t.c.emp_code))
                return [dict(r) for r in conn.execute(q).mappings()]
            finally:
                tmp.drop(conn, checkfirst=True)
                conn.commit()

    # ---- synchronisation support ----
    def count(self) -> int:
        self.ensure_schema()
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(_t)).scalar_one()

    def all_codes(self) -> list:
        self.ensure_schema()
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(select(_t.c.emp_code))]

    def get_many(self, codes) -> dict:
        self.ensure_schema()
        out: dict = {}
        codes = list(codes)
        with self.engine.connect() as conn:
            for i in range(0, len(codes), 500):  # stay well under the bound-variable limit
                q = select(*_OUT_COLUMNS).where(_t.c.emp_code.in_(codes[i:i + 500]))
                out.update({r["emp_code"]: dict(r) for r in conn.execute(q).mappings()})
        return out

    @staticmethod
    def _upsert_statement():
        stmt = sqlite_insert(_t)
        return stmt.on_conflict_do_update(index_elements=["emp_code"], set_={c: stmt.excluded[c] for c in SQL_UPDATABLE})

    def upsert_employee(self, row: dict) -> None:
        """Insert or update by emp_code. Idempotent, so a retry can never duplicate anything."""
        self.ensure_schema()
        with self.engine.begin() as conn:
            conn.execute(self._upsert_statement(), [row])

    def upsert_many(self, rows: list) -> list:
        """One transaction for the whole batch. If it fails, retry row by row so one bad row
        cannot block the others. Returns [(emp_code, error message), ...] for the rows that failed."""
        if not rows:
            return []
        self.ensure_schema()
        try:
            with self.engine.begin() as conn:
                conn.execute(self._upsert_statement(), rows)
            return []
        except SQLAlchemyError:
            pass
        failures = []
        for row in rows:
            try:
                self.upsert_employee(row)
            except SQLAlchemyError as exc:
                failures.append((row["emp_code"], str(getattr(exc, "orig", exc))[:200]))
        return failures

    def delete_codes(self, codes) -> int:
        codes = list(codes)
        if not codes:
            return 0
        self.ensure_schema()
        removed = 0
        with self.engine.begin() as conn:
            for i in range(0, len(codes), 500):
                removed += conn.execute(_t.delete().where(_t.c.emp_code.in_(codes[i:i + 500]))).rowcount
        return removed


sql_repo = SqlEmployeeRepository(SQL_DATABASE_URL)


# ------------------------------ synchronisation ---------------------------- #
def record_sync_failure(emp_code: str, exc: Exception) -> None:
    """Remember that this employee still has to reach SQL. One record per employee (unique
    index, created on first use so a healthy system never creates the collection)."""
    try:
        db.sql_sync_outbox.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
        now = now_ms_precision()
        db.sql_sync_outbox.update_one(
            {"emp_code": emp_code},
            {"$set": {"status": "pending", "last_error": str(exc)[:300], "last_attempt_at": now},
             "$inc": {"attempts": 1}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        # Even this failed. Nothing is lost: reconcile compares every employee, so it will find it.
        log.exception("could not record the SQL sync failure for %s; reconcile will repair it", emp_code)


def sync_employee_to_sql(doc: dict) -> str:
    """Mirror one employee into SQL. NEVER raises. Returns "ok" or "pending"."""
    try:
        sql_repo.upsert_employee(mongo_employee_to_sql_row(doc))
        return "ok"
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the MongoDB write already succeeded
        log.warning("SQL sync of %s failed, queued for retry: %s", doc.get("emp_code"), exc)
        record_sync_failure(doc.get("emp_code", "?"), exc)
        return "pending"


def reconcile_sql(prune: bool = False) -> dict:
    """Make SQL match MongoDB: insert missing employees (this is also the first-time migration),
    update changed ones, optionally delete SQL rows that no longer exist in MongoDB, and clear
    the retry records of everything that is now in sync. Safe to run repeatedly."""
    inserted = updated = unchanged = 0
    failures: list = []
    mongo_codes: set = set()

    def flush(batch: list) -> None:
        nonlocal inserted, updated, unchanged
        existing = sql_repo.get_many([d["emp_code"] for d in batch])
        pending, kind = [], {}
        for d in batch:
            row = mongo_employee_to_sql_row(d)
            current = existing.get(row["emp_code"])
            if current is None:
                pending.append(row)
                kind[row["emp_code"]] = "inserted"
            elif any(current[k] != row[k] for k in SQL_UPDATABLE):
                pending.append(row)
                kind[row["emp_code"]] = "updated"
            else:
                unchanged += 1
        failed = sql_repo.upsert_many(pending)
        failures.extend(failed)
        failed_codes = {c for c, _ in failed}
        for code, what in kind.items():
            if code not in failed_codes:
                inserted += what == "inserted"
                updated += what == "updated"

    batch: list = []
    for doc in db.employees.find({}, {"_id": 0}).sort("emp_code", ASCENDING):
        mongo_codes.add(doc["emp_code"])
        batch.append(doc)
        if len(batch) >= 500:
            flush(batch)
            batch = []
    if batch:
        flush(batch)

    orphans = sorted(set(sql_repo.all_codes()) - mongo_codes)
    removed = sql_repo.delete_codes(orphans) if prune else 0
    still_failing = sorted({c for c, _ in failures})
    cleared = db.sql_sync_outbox.delete_many({"emp_code": {"$nin": still_failing}}).deleted_count
    return {
        "mongo_employees": len(mongo_codes),
        "inserted": inserted, "updated": updated, "unchanged": unchanged,
        "failed_count": len(still_failing),
        "failed": [{"emp_code": c, "error": e} for c, e in failures[:50]],
        "orphans_in_sql": orphans[:200], "orphans_removed": removed,
        "retry_records_cleared": cleared,
    }


# --------------------------------- endpoints -------------------------------- #
ReqDate = Annotated[str, Query(pattern=DATE_RE), AfterValidator(_check_date)]


def _sql_page(result, page: int, page_size: int) -> dict:
    rows, total = result
    return {"items": [sql_row_to_api(r) for r in rows], "total": total, "page": page, "page_size": page_size}


# NOTE: fixed paths (search, without-attendance) are registered BEFORE /sql/employees/{emp_code},
# otherwise "search" would be read as an employee code.
@app.get("/sql/employees", tags=["sql"], summary="List employees stored in SQL")
def sql_list_employees(page: Page = 1, page_size: PageSize = 20):
    return _sql_page(sql_repo.list_all(page_size, (page - 1) * page_size), page, page_size)


@app.get("/sql/employees/search", tags=["sql"], summary="Search by name or employee code (case-insensitive)")
def sql_search_employees(
    q: Annotated[Optional[str], Query(min_length=1, max_length=100)] = None,
    name: Annotated[Optional[str], Query(min_length=1, max_length=100)] = None,  # alias of q
    page: Page = 1,
    page_size: PageSize = 20,
):
    term = q if q is not None else name
    if term is None:
        raise unprocessable(("query", "q"), "provide q (or name)")
    return _sql_page(sql_repo.search(term, page_size, (page - 1) * page_size), page, page_size)


@app.get("/sql/employees/without-attendance", tags=["sql"],
         summary="Employees with no attendance record at all (attendance comes from MongoDB)")
def sql_employees_without_attendance(page: Page = 1, page_size: PageSize = 20):
    rows = sql_repo.without_attendance(db.attendance_logs.distinct("emp_code"))
    start = (page - 1) * page_size
    return {"items": [sql_row_to_api(r) for r in rows[start:start + page_size]], "total": len(rows),
            "page": page, "page_size": page_size}


@app.get("/sql/employees/{emp_code}", tags=["sql"], summary="One employee by code")
def sql_get_employee(emp_code: str):
    row = sql_repo.get_by_code(emp_code)
    if row is None:
        raise HTTPException(404, "employee not found")
    return sql_row_to_api(row)


@app.get("/sql/departments/{department}/employees", tags=["sql"], summary="Employees of one department")
def sql_department_employees(department: str, page: Page = 1, page_size: PageSize = 20):
    return _sql_page(sql_repo.by_department(department, page_size, (page - 1) * page_size), page, page_size)


@app.get("/sql/analytics/department-counts", tags=["sql"], summary="Employees per department")
def sql_department_counts():
    return {"items": sql_repo.department_counts()}


@app.get("/sql/analytics/department-summary", tags=["sql"], summary="Department-wise employee summary")
def sql_department_summary():
    return {"items": sql_repo.department_summary()}


@app.get("/sql/analytics/recent-joiners", tags=["sql"], summary="Employees who joined between two dates (inclusive)")
def sql_recent_joiners(from_date: ReqDate, to_date: OptDate = None, page: Page = 1, page_size: PageSize = 20):
    if to_date is not None and to_date < from_date:
        raise unprocessable(("query", "to_date"), "to_date must not be before from_date")
    return _sql_page(sql_repo.joined_between(from_date, to_date, page_size, (page - 1) * page_size), page, page_size)


@app.get("/sql/analytics/joined-after", tags=["sql"], summary="Employees who joined strictly after a date")
def sql_joined_after(date_after: Annotated[str, Query(alias="date", pattern=DATE_RE), AfterValidator(_check_date)],
                     page: Page = 1, page_size: PageSize = 20):
    return _sql_page(sql_repo.joined_after(date_after, page_size, (page - 1) * page_size), page, page_size)


@app.get("/sql/analytics/duplicate-emails", tags=["sql"], summary="Email addresses used by more than one employee")
def sql_duplicate_emails():
    return {"items": sql_repo.duplicate_emails()}


@app.get("/sql/sync/status", tags=["sql"], summary="How far SQL is behind MongoDB")
def sql_sync_status():
    mongo_n, sql_n = db.employees.count_documents({}), sql_repo.count()
    pending = db.sql_sync_outbox.count_documents({"status": "pending"})
    return {"mongo_employees": mongo_n, "sql_employees": sql_n, "counts_match": mongo_n == sql_n,
            "pending_retries": pending,
            "note": "counts_match is a quick check only; POST /sql/sync/reconcile compares every field"}


@app.post("/sql/sync/reconcile", tags=["sql"], summary="Copy/repair employees from MongoDB into SQL (idempotent)")
def sql_reconcile(prune: bool = False):
    return reconcile_sql(prune=prune)
