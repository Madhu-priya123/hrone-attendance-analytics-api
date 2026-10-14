"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is read for convenience; real environment variables win)
"""
import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_type, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import ObjectId, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()  # override=False: real environment variables take priority over .env
log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones, so comparisons
# between stored values and values built from request timestamps are always like-for-like.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    tz_aware=True,
    tzinfo=timezone.utc,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def create_indexes():
    """Idempotent: create_index is a no-op when the same index already exists."""
    db.employees.create_index("emp_code", unique=True)                        # identity, 409 on duplicates
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])  # headcount / trend / list
    db.employees.create_index("joined_on")                                    # summary without department
    logs = db.attendance_logs
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)    # one record per day, monthly
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])        # GET /attendance sort, trend per day
    logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])     # leaderboard
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])  # status filter + sort
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])    # punch-out "latest punch_in <= t"


@asynccontextmanager
async def lifespan(_app):
    try:
        create_indexes()
    except Exception:  # MongoDB down at boot: stay up so /health can answer 503
        log.exception("index creation failed")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_epoch_ms(value) -> datetime:
    """Epoch milliseconds -> UTC datetime truncated to whole seconds (R1)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=422, detail="Timestamp must be epoch milliseconds")
    if value < 100000000000 or value > 4102444800000:
        raise HTTPException(status_code=422, detail="Timestamp must be valid epoch milliseconds")
    return (EPOCH + timedelta(milliseconds=value)).replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def api_value(value):
    """Mongo -> API representation: datetimes become epoch ms, _id never leaves."""
    if isinstance(value, datetime):
        return int((as_utc(value).replace(microsecond=0) - EPOCH).total_seconds()) * 1000
    if isinstance(value, ObjectId):
        return None
    if isinstance(value, dict):
        return {k: api_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [api_value(v) for v in value]
    return value


_DEFAULTS = {
    "punch_in": None, "punch_out": None, "work_hours": None,
    "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
}


def normalize_record(doc: dict) -> dict:
    """Seeded documents may omit fields (DATA_MODEL.md): treat missing as the documented default."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    for key, default in _DEFAULTS.items():
        if out.get(key) is None:
            out[key] = list(default) if isinstance(default, list) else default
    return out


def valid_date(value: str, name: str = "date") -> str:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) and date_type.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise HTTPException(status_code=422, detail=f"{name} must be a valid YYYY-MM-DD date")


def month_bounds(month: str):
    """'2026-07' -> ('2026-07-01', '2026-07-31', year, month, days)."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or "") or month.startswith("0000"):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    days = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{days:02d}", year, mon, days


def round_half_up(value, places: int) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def attendance_date_for(punch_in_ist: datetime, shift_start: str, shift_end: str) -> date_type:
    """R1: an overnight punch-in earlier than shift_end belongs to the previous day's shift."""
    day = punch_in_ist.date()
    if shift_end <= shift_start and punch_in_ist.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str = "18:30") -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor minutes since shift_start."""
    punch_in = as_utc(punch_in).astimezone(IST).replace(microsecond=0)
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    if shift_end <= shift_start and punch_in.strftime("%H:%M") < shift_end:
        start -= timedelta(days=1)
    seconds_late = int((punch_in - start).total_seconds())
    return seconds_late // 60 if seconds_late > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals, half-up (Decimal, not float round)."""
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: floor minutes after shift_end, only if >= 30. Overnight shifts end on the next day."""
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    seconds = int((as_utc(punch_out) - end.astimezone(timezone.utc)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def check_pagination(page: int, page_size: int):
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value):
        if not re.fullmatch(r"EMP[0-9]{4,6}", value):
            raise ValueError("emp_code must look like EMP0001")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 100:
            raise ValueError("name must be 1 to 100 characters")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if len(value) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address")
        return value

    @field_validator("department")
    @classmethod
    def validate_department(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 50:
            raise ValueError("department must be 1 to 50 characters")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("Shift time must use HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) and date_type.fromisoformat(value).isoformat() == value:
                return value
        except ValueError:
            pass
        raise ValueError("joined_on must be a valid YYYY-MM-DD date")

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must be different")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


class AttendanceUpdateIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value):
        value = value.strip()
        if not 5 <= len(value) <= 200:
            raise ValueError("reason must be 5-200 characters")
        return value

    @field_validator("regularized_by")
    @classmethod
    def validate_by(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return value


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)  # unique index on emp_code is the arbiter, not a prior find()
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return api_value(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    check_pagination(page, page_size)
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    )
    return {"items": api_value(items), "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = parse_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    ts_ist = ts.astimezone(IST)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date_for(ts_ist, emp["shift_start"], emp["shift_end"]).isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index decides the race
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    return api_value(normalize_record(doc))


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    if date_from:
        valid_date(date_from, "date_from")
    if date_to:
        valid_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    if status and status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
        raise HTTPException(status_code=422, detail="Invalid status")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    check_pagination(page, page_size)
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [api_value(normalize_record(d)) for d in cursor],
        "total": total, "page": page, "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = parse_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in record found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")
    elapsed = (ts - as_utc(record["punch_in"])).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(status_code=422, detail="Punch-out must be after punch-in and within 24 hours")
    work_hours = compute_work_hours(record["punch_in"], ts)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},  # compare-and-set: only one concurrent request wins
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"]),
            "half_day": work_hours < 4.50,
        }},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Already punched out")
    return api_value(normalize_record(db.attendance_logs.find_one({"_id": record["_id"]})))


@app.patch("/attendance/{emp_code}/{date}")
def update_attendance(emp_code: str, date: str, body: AttendanceUpdateIn):
    valid_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    old = normalize_record(record)

    updates = {}
    if body.status is not None:
        updates["status"] = body.status
    for field in ("punch_in", "punch_out"):
        value = getattr(body, field)
        if value is not None:
            updates[field] = parse_epoch_ms(value)

    status = updates.get("status", record["status"])
    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in updates or "punch_out" in updates:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot have punch times")
        updates["punch_in"] = None
        updates["punch_out"] = None
    current_in = updates.get("punch_in", old["punch_in"])
    current_out = updates.get("punch_out", old["punch_out"])

    if status in PRESENCE and current_in is None:
        raise HTTPException(status_code=422, detail="Presence status requires punch_in")
    if current_in is not None:
        expected = attendance_date_for(as_utc(current_in).astimezone(IST), emp["shift_start"], emp["shift_end"]).isoformat()
        if expected != date:
            raise HTTPException(status_code=422, detail="punch_in must be on the attendance date")
        if current_out is not None:
            elapsed = (as_utc(current_out) - as_utc(current_in)).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    if current_in is not None and current_out is not None:
        updates["work_hours"] = compute_work_hours(current_in, current_out)
        updates["half_day"] = updates["work_hours"] < 4.50
    else:
        updates["work_hours"] = None
        updates["half_day"] = False
    updates["late_minutes"] = compute_late_minutes(current_in, emp["shift_start"], emp["shift_end"]) if current_in is not None else 0
    updates["overtime_minutes"] = (
        compute_overtime(current_out, emp["shift_start"], emp["shift_end"], date) if current_out is not None else 0
    )

    # Compare against defaults-filled values, so a missing half_day/late_minutes is not a "change".
    changes = {f: {"from": old[f] if f in old else None, "to": v} for f, v in updates.items() if old.get(f) != v}
    if not changes:
        raise HTTPException(status_code=422, detail="No changes supplied")

    entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Optimistic concurrency: only apply if nothing changed since we read it (same history length and fields).
    guard = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(old["history"])]},
    }
    result = db.attendance_logs.update_one(guard, {"$set": updates, "$push": {"history": entry}})
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Attendance changed concurrently")
    return api_value(normalize_record(db.attendance_logs.find_one({"_id": record["_id"]})))


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}  # 1=Sun ... 7=Sat
_WEEKDAY = {"$and": [{"$gte": [_DOW, 2]}, {"$lte": [_DOW, 6]}]}
_PRESENT = {"$in": ["$status", PRESENCE]}
_HALF_AWARE = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}


def build_monthly_pipeline(emp_code: str, start_date: str, end_date: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [_PRESENT, _WEEKDAY]}, _HALF_AWARE, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start_date, end_date, year, mon, days = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    working_days = sum(
        1 for d in range(1, days + 1)
        if date_type(year, mon, d).weekday() < 5 and f"{year:04d}-{mon:02d}-{d:02d}" >= emp["joined_on"]
    )
    result = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, start_date, end_date)))
    stats = result[0] if result else {}
    present_days = round_half_up(stats.get("present_days", 0), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        # present/working x 100 is a percentage (R8: 2 decimals); only trend rates use 4 decimals.
        "attendance_pct": round_half_up(Decimal(str(present_days)) * 100 / working_days, 2) if working_days > 0 else None,
    }


def build_department_summary_pipeline(start_date: str, end_date: str, department: Optional[str] = None):
    employee_match = {"joined_on": {"$lte": end_date}}  # R9: joined on/before the end of the month
    if department:
        employee_match["department"] = department
    present_with_hours = {"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}
    logs_dow = {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"date": {"$gte": start_date, "$lte": end_date},
                                     "$expr": {"$eq": ["$emp_code", "$$code"]}}}],
            "as": "logs",
        }},
        # preserveNullAndEmptyArrays keeps employees with zero logs, so they still count in headcount
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$gte": [logs_dow, 2]}, {"$lte": [logs_dow, 6]}]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [present_with_hours, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [present_with_hours, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            # average over records: total hours / number of records, not an average of averages
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]},
                                         {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_date, end_date, *_ = month_bounds(month)
    rows = list(db.employees.aggregate(build_department_summary_pipeline(start_date, end_date, department)))
    for row in rows:
        if row.get("avg_work_hours") is not None:
            row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2)
        row["present_days"] = round_half_up(row.get("present_days", 0), 2)
    return {"month": month, "items": rows}


def build_leaderboard_pipeline(start_date: str, end_date: str, limit: int = 10, department: Optional[str] = None):
    pipeline = [
        {"$match": {"date": {"$gte": start_date, "$lte": end_date}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},  # logs of unknown employees are dropped
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})  # rank inside the department only
    pipeline.extend([
        # $rank = standard competition ranking: 1, 2, 2, 4. limit is applied after ranking.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start_date, end_date, *_ = month_bounds(month)
    items = list(db.attendance_logs.aggregate(build_leaderboard_pipeline(start_date, end_date, limit, department)))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, start: date_type, end: date_type):
    """One row per calendar day, built entirely in MongoDB.

    Rooted on `employees` (index on department) so the explain shows an IXSCAN. Employees are
    collected once, a $range generates every day (the gap filling), then each day looks up its
    attendance by index. The department is verified to exist before this runs, so $group always
    yields exactly one document.
    """
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    n_days = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "emps": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {"_id": 0, "emps": 1, "days": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {
                "$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}}}},
        }}}},
        {"$unwind": "$days"},
        {"$project": {"emps": 1, "day": "$days"}},
        {"$set": {
            "active": {"$filter": {"input": "$emps", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$day"]}}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$day"}}}, 5]},
        }},
        {"$set": {"codes": "$active.emp_code", "headcount": {"$size": "$active"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$day", "codes": "$codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$set": {
            "present_count": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$cond": [
                {"$in": ["$$log.status", PRESENCE]},
                {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1]}, 0]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log",
                                                 "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
        }},
        # attendance_rate is a ratio (present_count / headcount), only on working days with headcount > 0
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # $avg ignores nulls; window = current row + up to 6 preceding rows inside the range
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$day", "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


def parse_trend_range(from_date: str, to_date: str):
    start = date_type.fromisoformat(valid_date(from_date, "from"))
    end = date_type.fromisoformat(valid_date(to_date, "to"))
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Invalid date range; maximum is 92 days")
    return start, end


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")):
    start, end = parse_trend_range(from_date, to_date)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Department not found")
    items = list(db.employees.aggregate(build_department_trend_pipeline(department, start, end)))
    for row in items:
        for field in ("attendance_rate", "moving_avg_7d"):
            if row.get(field) is not None:
                row[field] = round_half_up(row[field], 4)
        row["present_count"] = round_half_up(row.get("present_count", 0), 2)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def _explain_aggregate(collection: str, pipeline: list) -> dict:
    return db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    month: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(status_code=422, detail="Invalid endpoint")
    check_pagination(page, page_size)

    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        explain = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT),
                                         "skip": (page - 1) * page_size, "limit": page_size}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        start_date, end_date, *_ = month_bounds(month)
        explain = _explain_aggregate("attendance_logs", build_monthly_pipeline(emp_code, start_date, end_date))
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start_date, end_date, *_ = month_bounds(month)
        explain = _explain_aggregate("employees", build_department_summary_pipeline(start_date, end_date, department))
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start_date, end_date, *_ = month_bounds(month)
        explain = _explain_aggregate("attendance_logs", build_leaderboard_pipeline(start_date, end_date, limit, department))
        collection = "attendance_logs"
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        start, end = parse_trend_range(from_date, to_date)
        explain = _explain_aggregate("employees", build_department_trend_pipeline(department, start, end))
        collection = "employees"

    return Response(content=json_util.dumps({"endpoint": endpoint, "collection": collection, "explain": explain}),
                    media_type="application/json")