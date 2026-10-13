"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, NoReturn, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()
log = logging.getLogger("attendance")

# tz_aware makes PyMongo return UTC-aware datetimes instead of naive ones
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees_col = db["employees"]
logs_col = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_LIMIT_HOURS = 4.5
MAX_SHIFT_LENGTH = timedelta(hours=24)
MAX_TREND_DAYS = 92
REGULARIZE_RETRIES = 5
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# Indexes

INDEXES = [
    (employees_col, [("emp_code", ASCENDING)], {"name": "emp_code_unique", "unique": True}),
    (employees_col, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
    (employees_col, [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
    (employees_col, [("joined_on", ASCENDING)], {"name": "joined_on"}),
    (logs_col, [("emp_code", ASCENDING), ("date", ASCENDING)], {"name": "emp_date_unique", "unique": True}),
    (logs_col, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
    (
        logs_col,
        [("date", DESCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)],
        {"name": "date_desc_status_emp"},
    ),
]
indexes_ready = False


def ensure_indexes() -> bool:
    """Create every index; safe to call repeatedly."""
    global indexes_ready
    ok = True
    for collection, keys, options in INDEXES:
        try:
            collection.create_index(keys, **options)
        except ConnectionFailure as exc:
            log.warning("MongoDB unreachable, skipping index creation: %s", exc)
            ok = False
            break  # health() retries later; do not wait for a timeout per index
        except PyMongoError as exc:
            ok = False
            log.warning("index %s failed: %s", options["name"], exc)
    indexes_ready = ok
    return ok


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error_handler(_request, _exc):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# Time helpers

def as_utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC and convert aware ones to UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def from_millis(ms: int) -> datetime:
    """Epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def to_millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return (as_utc(value) - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis_precision() -> datetime:
    """Current time at millisecond precision, which is what MongoDB stores."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def shift_moment(date_str: str, hhmm: str) -> datetime:
    """The IST datetime of a shift time on the given attendance date."""
    return datetime.combine(date_type.fromisoformat(date_str), parse_hhmm(hhmm), tzinfo=IST)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """IST attendance date of a punch-in (R1), including the overnight exception."""
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    overnight = shift_end <= shift_start
    if overnight and local.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def round_half_up(value: Any, places: int) -> float:
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# Attendance calculations (R2 to R5)

def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None) -> int:
    """Whole minutes late, or 0 inside the 10 minute grace (R2)."""
    day = date_str or as_utc(punch_in).astimezone(IST).date().isoformat()
    late_seconds = int((as_utc(punch_in) - shift_moment(day, shift_start)).total_seconds())
    return late_seconds // 60 if late_seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None) -> int:
    """Overtime minutes after shift end, counted only from 30 minutes (R3)."""
    end = shift_moment(date_str, shift_end)
    if shift_start is not None and shift_end <= shift_start:
        end += timedelta(days=1)  # overnight shift ends the next day
    minutes = int((as_utc(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive_fields(
    emp: dict, date_str: str, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]
) -> dict:
    """The four stored derived fields for a record in its final state."""
    fields = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return fields
    start, end = emp["shift_start"], emp["shift_end"]
    fields["late_minutes"] = compute_late_minutes(punch_in, start, date_str)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        fields["work_hours"] = hours
        fields["overtime_minutes"] = compute_overtime(punch_out, end, date_str, start)
        fields["half_day"] = hours < HALF_DAY_LIMIT_HOURS
    return fields


# Validation helpers

def check_date(value: str) -> str:
    try:
        date_type.fromisoformat(value)
    except ValueError:
        raise ValueError("not a valid calendar date")
    return value


def check_month(value: str) -> str:
    check_date(value + "-01")
    return value


DateStr = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(check_date)
]
MonthStr = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(check_month)
]
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def invalid(message: str, *loc: str) -> NoReturn:
    """Raise a 422 in FastAPI's standard validation format."""
    raise RequestValidationError(
        [{"type": "value_error", "loc": loc or ("body",), "msg": message, "input": None}]
    )


def require(params: dict) -> None:
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing]
        )


# Request models

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


# Serialization

def serialize_employee(doc: dict) -> dict:
    keys = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    return {**{key: doc[key] for key in keys}, "created_at": to_millis(doc.get("created_at"))}


def serialize_history(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        changes[field] = {
            side: to_millis(change.get(side)) if isinstance(change.get(side), datetime) else change.get(side)
            for side in ("from", "to")
        }
    return {"at": to_millis(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def serialize_attendance(doc: dict) -> dict:
    """API shape of a record; tolerates legacy documents with missing fields."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_millis(doc.get("punch_in")),
        "punch_out": to_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(entry) for entry in doc.get("history") or []],
    }


def get_employee(emp_code: str) -> dict:
    emp = employees_col.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# System and employees

@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    if not indexes_ready:
        ensure_indexes()  # covers a database that was down at startup
    return {"status": "ok"}


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis_precision()
    try:
        employees_col.insert_one(doc)  # the unique index decides duplicates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = employees_col.count_documents(query)
    skip = (page - 1) * page_size
    items = []
    if skip < total:
        cursor = employees_col.find(query).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
        items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# Attendance

ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    if date_from and date_to and date_from > date_to:
        invalid("date_from must not be after date_to", "query", "date_from")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    dates = {}
    if date_from:
        dates["$gte"] = date_from
    if date_to:
        dates["$lte"] = date_to
    if dates:
        query["date"] = dates
    if status:
        query["status"] = status
    return query


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    punched = from_millis(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date_for(punched, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        **derive_fields(emp, day, body.status, punched, None),
        "history": [],
    }
    try:
        logs_col.insert_one(doc)  # unique (emp_code, date) index settles races
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    out_at = from_millis(body.punched_at) if body.punched_at is not None else now_utc()
    record = logs_col.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_at}}, sort=[("punch_in", DESCENDING)]
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    in_at = as_utc(record["punch_in"])
    if out_at <= in_at or out_at - in_at > MAX_SHIFT_LENGTH:
        invalid("punched_at must be after punch_in and within 24 hours of it", "body", "punched_at")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    fields = derive_fields(emp, record["date"], record["status"], in_at, out_at)
    # Only one request can match punch_out: null, so the others get None back
    updated = logs_col.find_one_and_update(
        {"_id": record["_id"], "punch_out": None, "punch_in": record["punch_in"]},
        {"$set": {"punch_out": out_at, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


def same_instant(a: Optional[datetime], b: Optional[datetime]) -> bool:
    if a is None or b is None:
        return a is b
    return as_utc(a) == as_utc(b)


def build_correction(emp: dict, record: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Return (guard filter, update) for one correction attempt."""
    history = record.get("history") or []
    old_status = record["status"]
    old_in, old_out = record.get("punch_in"), record.get("punch_out")
    times_given = body.punch_in is not None or body.punch_out is not None

    new_status = body.status or old_status
    if new_status in PRESENCE:
        new_in = from_millis(body.punch_in) if body.punch_in is not None else old_in
        new_out = from_millis(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            invalid("a presence status needs a punch_in", "body", "punch_in")
    else:
        if times_given:
            invalid("ABSENT and LEAVE clear punch times; do not send them", "body", "status")
        new_in = new_out = None

    if body.punch_in is not None:
        if attendance_date_for(new_in, emp["shift_start"], emp["shift_end"]) != record["date"]:
            invalid("punch_in must stay on the record's attendance date", "body", "punch_in")
    if times_given and new_out is not None:
        if as_utc(new_out) <= as_utc(new_in) or as_utc(new_out) - as_utc(new_in) > MAX_SHIFT_LENGTH:
            invalid("punch_out must be after punch_in and within 24 hours", "body", "punch_out")

    requested_change = (
        new_status != old_status or not same_instant(new_in, old_in) or not same_instant(new_out, old_out)
    )
    if not requested_change:
        invalid("the request changes nothing", "body")

    new_fields = derive_fields(emp, record["date"], new_status, new_in, new_out)
    before = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day", False)),
    }
    after = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **new_fields}
    changes = {
        name: {"from": before[name], "to": after[name]}
        for name in after
        if (not same_instant(before[name], after[name]) if name in ("punch_in", "punch_out") else before[name] != after[name])
    }
    entry = {"at": now_millis_precision(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # The write only applies if nobody changed the record since it was read
    guard = {
        "_id": record["_id"],
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        f"history.{len(history)}": {"$exists": False},
    }
    update = {"$set": after, "$push": {"history": entry}}
    return guard, update


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee(emp_code)
    for _ in range(REGULARIZE_RETRIES):
        record = logs_col.find_one({"emp_code": emp_code, "date": date})
        if record is None:
            raise HTTPException(404, "no attendance record for that date")
        guard, update = build_correction(emp, record, body)
        updated = logs_col.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return serialize_attendance(updated)
    raise HTTPException(409, "record was modified concurrently, please retry")


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_query(emp_code, date_from, date_to, status)
    total = logs_col.estimated_document_count() if not query else logs_col.count_documents(query)
    skip = (page - 1) * page_size
    items = []
    if skip < total:
        cursor = logs_col.find(query).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)
        items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# Aggregation expressions shared by the analytics pipelines

LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
IS_WEEKDAY = {
    "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]
}
# A present record is worth 1, or 0.5 when it is a half day (R5, R6)
PRESENT_WEIGHT = {
    "$cond": [IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]
}
WEEKDAY_PRESENT_WEIGHT = {"$cond": [IS_WEEKDAY, PRESENT_WEIGHT, 0]}
LATE_FLAG = {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}


def STATUS_FLAG(name: str) -> dict:
    return {"$cond": [{"$eq": ["$status", name]}, 1, 0]}


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = (int(part) for part in month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def count_weekdays(start: str, end: str) -> int:
    first, last = date_type.fromisoformat(start), date_type.fromisoformat(end)
    days = (last - first).days + 1
    if days <= 0:
        return 0
    weeks, extra = divmod(days, 7)
    leftover = sum(1 for i in range(extra) if (first + timedelta(days=i)).weekday() < 5)
    return weeks * 5 + leftover


# Employee monthly analytics

def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": WEEKDAY_PRESENT_WEIGHT},
                "leave_days": {"$sum": STATUS_FLAG("LEAVE")},
                "late_count": {"$sum": LATE_FLAG},
                "total_late_minutes": {"$sum": LATE_MINUTES},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: MonthStr):
    emp = get_employee(emp_code)
    start, end = month_bounds(month)
    working_days = count_weekdays(max(start, emp["joined_on"]), end)
    stats = next(logs_col.aggregate(monthly_pipeline(emp_code, start, end)), {})
    present_days = Decimal(str(stats.get("present_days", 0)))
    pct = None if working_days == 0 else round_half_up(present_days * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# Department summary

WORK_HOURS_SCALE = 1_000_000  # sum work hours as integers so the average stays exact


def summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    employee_filter: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_filter["department"] = department
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    # Start from employees so people without logs still count (R9)
    return [
        {"$match": employee_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": WEEKDAY_PRESENT_WEIGHT},
                            "hours_scaled": {
                                "$sum": {
                                    "$cond": [
                                        has_hours,
                                        {"$toLong": {"$round": [{"$multiply": ["$work_hours", WORK_HOURS_SCALE]}, 0]}},
                                        0,
                                    ]
                                }
                            },
                            "hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
                            "late_count": {"$sum": LATE_FLAG},
                            "total_late_minutes": {"$sum": LATE_MINUTES},
                            "leave_count": {"$sum": STATUS_FLAG("LEAVE")},
                            "on_duty_count": {"$sum": STATUS_FLAG("ON_DUTY")},
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$stats.present_days"},
                "hours_scaled": {"$sum": "$stats.hours_scaled"},
                "hours_count": {"$sum": "$stats.hours_count"},
                "late_count": {"$sum": "$stats.late_count"},
                "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
                "leave_count": {"$sum": "$stats.leave_count"},
                "on_duty_count": {"$sum": "$stats.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def format_summary_row(row: dict) -> dict:
    avg = None
    if row["hours_count"]:
        avg = round_half_up(Decimal(int(row["hours_scaled"])) / Decimal(int(row["hours_count"])) / WORK_HOURS_SCALE, 2)
    return {
        "department": row["_id"],
        "headcount": int(row["headcount"]),
        "present_days": round_half_up(row["present_days"], 2),
        "avg_work_hours": avg,
        "late_count": int(row["late_count"]),
        "total_late_minutes": int(row["total_late_minutes"]),
        "leave_count": int(row["leave_count"]),
        "on_duty_count": int(row["on_duty_count"]),
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: MonthStr, department: Optional[str] = None):
    start, end = month_bounds(month)
    rows = employees_col.aggregate(summary_pipeline(start, end, department))
    return {"month": month, "items": [format_summary_row(row) for row in rows]}


# Late leaderboard

def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees drop out here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank gives ties the same rank and skips the next one (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: MonthStr,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    rows = logs_col.aggregate(leaderboard_pipeline(start, end, limit, department))
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


# Department trend

def validate_trend_range(start: str, end: str) -> None:
    first, last = date_type.fromisoformat(start), date_type.fromisoformat(end)
    if last < first:
        invalid("'to' must not be before 'from'", "query", "to")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        invalid(f"range must not exceed {MAX_TREND_DAYS} days", "query", "to")


def ensure_department_exists(department: str) -> None:
    if employees_col.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")


def day_start(value: str) -> datetime:
    return datetime.combine(date_type.fromisoformat(value), time.min, tzinfo=UTC)


def trend_pipeline(department: str, start: str, end: str) -> list:
    first, last = day_start(start), day_start(end)
    in_range = {"$and": [{"$gte": ["$joined_on", start]}, {"$lte": ["$joined_on", end]}]}
    working_day = {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}
    window_7 = {"documents": [-6, 0]}
    # Every employee emits one join event, plus one event per log in the range.
    # This also guarantees $densify always has at least one document.
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "joins": {"$literal": 0},
                            "present": PRESENT_WEIGHT,
                            "late": LATE_FLAG,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "events": {
                    "$concatArrays": [
                        [
                            {
                                "date": {"$cond": [in_range, "$joined_on", start]},
                                "joins": {"$cond": [{"$lte": ["$joined_on", end]}, 1, 0]},
                                "present": 0,
                                "late": 0,
                            }
                        ],
                        "$logs",
                    ]
                },
            }
        },
        {"$unwind": "$events"},
        {"$replaceRoot": {"newRoot": "$events"}},
        {
            "$group": {
                "_id": "$date",
                "joins": {"$sum": "$joins"},
                "present": {"$sum": "$present"},
                "late": {"$sum": "$late"},
            }
        },
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"day": 1}},
        # Gap filling: one row per calendar day; the upper bound is exclusive
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [first, last + timedelta(days=1)]},
            }
        },
        {"$match": {"day": {"$gte": first, "$lte": last}}},
        {
            "$set": {
                "joins": {"$ifNull": ["$joins", 0]},
                "present": {"$ifNull": ["$present", 0]},
                "late": {"$ifNull": ["$late", 0]},
                "dow": {"$dayOfWeek": "$day"},
            }
        },
        # Running total of joins is the headcount on each day (R9)
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        # Rate in ten-thousandths, rounded half up with exact arithmetic
        {
            "$set": {
                "is_working_day": working_day,
                "rate_r": {
                    "$cond": [
                        {"$and": [working_day, {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$present", 20000]}, "$headcount"]},
                                    {"$multiply": ["$headcount", 2]},
                                ]
                            }
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "ma_sum": {"$sum": "$rate_r", "window": window_7},
                    "ma_n": {"$sum": {"$cond": [{"$isNumber": "$rate_r"}, 1, 0]}, "window": window_7},
                },
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": "$present",
                "late_count": "$late",
                "attendance_rate": {"$cond": [{"$isNumber": "$rate_r"}, {"$divide": ["$rate_r", 10000]}, None]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$ma_n", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": ["$ma_sum", 2]}, "$ma_n"]},
                                            {"$multiply": ["$ma_n", 2]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    date_from: Annotated[DateStr, Query(alias="from")],
    date_to: Annotated[DateStr, Query(alias="to")],
):
    validate_trend_range(date_from, date_to)
    ensure_department_exists(department)
    rows = employees_col.aggregate(trend_pipeline(department, date_from, date_to))
    items = [
        {
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": row["attendance_rate"],
            "moving_avg_7d": row["moving_avg_7d"],
        }
        for row in rows
    ]
    return {"department": department, "items": items}


# Explain

ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def run_explain(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


def aggregate_command(collection: str, pipeline: list) -> dict:
    return {"aggregate": collection, "pipeline": pipeline, "cursor": {}}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    ensure_indexes()  # cheap no-op when present; covers collections recreated after startup
    if endpoint == "attendance_list":
        query = attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": query,
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        require({"emp_code": emp_code, "month": month})
        start, end = month_bounds(month)
        collection = "attendance_logs"
        command = aggregate_command(collection, monthly_pipeline(emp_code, start, end))
    elif endpoint == "department_summary":
        require({"month": month})
        start, end = month_bounds(month)
        collection = "employees"
        command = aggregate_command(collection, summary_pipeline(start, end, department))
    elif endpoint == "late_leaderboard":
        require({"month": month})
        start, end = month_bounds(month)
        collection = "attendance_logs"
        command = aggregate_command(collection, leaderboard_pipeline(start, end, limit, department))
    else:
        require({"department": department, "from": from_, "to": to})
        validate_trend_range(from_, to)
        collection = "employees"
        command = aggregate_command(collection, trend_pipeline(department, from_, to))
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}