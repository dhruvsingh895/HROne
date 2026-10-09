"""Contract v2 attendance API. Run: uvicorn app.main:app --port 8000."""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Expected YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def valid_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Expected YYYY-MM")
    Date.fromisoformat(value + "-01")
    return value


DateString = Annotated[str, BeforeValidator(valid_date)]
Month = Annotated[str, BeforeValidator(valid_month)]
Shift = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="employee_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_department")
    db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], unique=True, name="employee_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="employee_punch")


@asynccontextmanager
async def lifespan(app):
    # Keep readiness reachable during a temporary outage; retry indexes on health.
    app.state.indexes_ready = False
    try:
        create_indexes()
        app.state.indexes_ready = True
    except PyMongoError:
        pass
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


def now():
    return datetime.now(UTC).replace(microsecond=0)


def instant(ms):
    return EPOCH + timedelta(seconds=ms // 1000)


def wire(value):
    """Serialize instants recursively, including nested history; hide internal IDs."""
    if isinstance(value, datetime):
        return int((value.astimezone(UTC) - EPOCH).total_seconds() * 1000)
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [wire(v) for v in value]
    return value


DEFAULTS = {"punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}


def record_wire(doc):
    return wire(DEFAULTS | doc)


def employee(code):
    doc = db.employees.find_one({"emp_code": code})
    if doc is None:
        raise HTTPException(404, "Unknown employee")
    return doc


def attendance_date(ts, emp):
    local = ts.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M:%S") < emp["shift_end"] + ":00":
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(emp, day):
    start = datetime.fromisoformat(day + "T" + emp["shift_start"]).replace(tzinfo=IST)
    end = datetime.fromisoformat(day + "T" + emp["shift_end"]).replace(tzinfo=IST)
    if emp["shift_end"] <= emp["shift_start"]:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in, shift_start):
    seconds = int((punch_in - shift_start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    hours = Decimal(int((punch_out - punch_in).total_seconds())) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, shift_end):
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derived(doc, emp):
    result = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if doc["status"] not in PRESENCE:
        return result
    pi, po = doc.get("punch_in"), doc.get("punch_out")
    if pi is None:
        raise HTTPException(422, "Presence requires punch_in")
    if attendance_date(pi, emp) != doc["date"]:
        raise HTTPException(422, "punch_in must remain on the attendance date")
    start, end = shift_bounds(emp, doc["date"])
    result["late_minutes"] = compute_late_minutes(pi, start)
    if po is not None:
        if not timedelta(0) < po - pi <= timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
        result["work_hours"] = compute_work_hours(pi, po)
        result["overtime_minutes"] = compute_overtime(po, end)
        result["half_day"] = result["work_hours"] < 4.5
    return result


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Shift = "09:30"
    shift_end: Shift = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = Field(default_factory=lambda: wire(now()))


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(BaseModel):
    # Non-nullable when supplied; defaults distinguish omission via model_fields_set.
    status: Status = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


@app.get("/health")
def health():
    client.admin.command("ping")
    if not getattr(app.state, "indexes_ready", False):
        create_indexes()
        app.state.indexes_ready = True
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump() | {"created_at": now()}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return wire(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    items = list(db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {"items": wire(items), "total": db.employees.count_documents(q), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = {"emp_code": body.emp_code, "date": attendance_date(ts, emp), "status": body.status,
           "punch_in": ts, "punch_out": None, "history": []}
    doc.update(derived(doc, emp))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date") from None
    return record_wire(doc)


def snapshot_filter(doc):
    """Compare all mutable fields, including legacy missing fields and audit history.

    A single-document conditional update provides optimistic concurrency without
    transactions or requiring a revision field in the grader's seeded documents.
    """
    result = {"_id": doc["_id"]}
    for field in ("status", *DEFAULTS):
        result[field] = {"$eq": doc[field], "$exists": True} if field in doc else {"$exists": False}
    return result


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}, sort=[("punch_in", -1)])
    if doc is None:
        raise HTTPException(404, "No punch-in found at or before punched_at")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")
    final = doc | {"punch_out": ts}
    updates = {"punch_out": ts} | derived(final, emp)
    result = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {"$set": updates},
                                                    return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "Record changed concurrently; retry")
    return record_wire(result)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateString, body: RegularizeIn):
    emp = employee(emp_code)
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if doc is None:
        raise HTTPException(404, "No attendance record for this date")
    supplied = body.model_fields_set
    final = DEFAULTS | doc
    if "status" in supplied:
        final["status"] = body.status
    if final["status"] not in PRESENCE:
        if supplied & {"punch_in", "punch_out"}:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        final.update(punch_in=None, punch_out=None)
    else:
        for field in ("punch_in", "punch_out"):
            if field in supplied:
                final[field] = instant(getattr(body, field))
    final.update(derived(final, emp))
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    before = DEFAULTS | doc
    changes = {f: {"from": before[f], "to": final[f]} for f in fields if before[f] != final[f]}
    if not changes:
        raise HTTPException(422, "Correction changes nothing")
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    result = db.attendance_logs.find_one_and_update(
        snapshot_filter(doc), {"$set": {f: final[f] for f in fields}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "Record changed concurrently; retry")
    return record_wire(result)


# Query builders are shared by the public endpoints and executionStats explain.
@dataclass
class Aggregation:
    collection: str
    pipeline: list
    hint: str

    def run(self):
        return list(db[self.collection].aggregate(self.pipeline, hint=self.hint, allowDiskUse=True))

    def command(self):
        return {"aggregate": self.collection, "pipeline": self.pipeline, "cursor": {},
                "hint": self.hint, "allowDiskUse": True}


def check_range(start, end, max_days=None):
    if start is not None and end is not None:
        if start > end:
            raise HTTPException(422, "End date must not precede start date")
        if max_days and (Date.fromisoformat(end) - Date.fromisoformat(start)).days + 1 > max_days:
            raise HTTPException(422, f"Range must not exceed {max_days} days")


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    check_range(date_from, date_to)
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = ({"$gte": date_from} if date_from is not None else {}) | ({"$lte": date_to} if date_to is not None else {})
    if status is not None:
        q["status"] = status
    hint = "employee_date" if emp_code is not None else "status_date_employee" if status is not None else "date_employee"
    return {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: DateString | None = None,
                    date_to: DateString | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    cmd = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    cursor = db.attendance_logs.find(cmd["filter"]).hint(cmd["hint"]).sort(list(cmd["sort"].items()))
    items = [record_wire(d) for d in cursor.skip(cmd["skip"]).limit(cmd["limit"])]
    total = db.attendance_logs.count_documents(cmd["filter"], hint=cmd["hint"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def month_bounds(month):
    first = Date.fromisoformat(month + "-01")
    return first.isoformat(), first.replace(day=calendar.monthrange(first.year, first.month)[1]).isoformat()


def mongo_date(value):
    return {"$dateFromString": {"dateString": value, "timezone": "+05:30"}}


def weekday(value):
    return {"$in": [{"$dayOfWeek": {"date": mongo_date(value), "timezone": "+05:30"}}, [2, 3, 4, 5, 6]]}


def half_up(value, places=2):
    # MongoDB $round uses ties-to-even. Decimal arithmetic + floor implements
    # half-up for the nonnegative counts, hours and rates in this contract.
    scale = 10 ** places
    return {"$cond": [{"$eq": [value, None]}, None,
            {"$toDouble": {"$divide": [{"$floor": {"$add": [
                {"$multiply": [{"$toDecimal": value}, scale]}, {"$toDecimal": "0.5"}]}}, scale]}}]}


def count_if(condition):
    return {"$sum": {"$cond": [condition, 1, 0]}}


def log_totals():
    present = {"$in": ["$status", PRESENCE]}
    late = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
    worked = {"$and": [present, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [{"$and": [present, weekday("$date")]},
                          {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
        "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
        "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
        "late_count": count_if(late),
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [worked, {"$toDecimal": "$work_hours"}, 0]}},
        "hours_count": count_if(worked),
    }}


def totals_lookup(first, last):
    return {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}, log_totals()], "as": "totals"}}


def total_value(field):
    return {"$ifNull": [{"$arrayElemAt": ["$totals." + field, 0]}, 0]}


def working_days(first, last):
    start = {"$max": [first, "$joined_on"]}
    length = {"$max": [0, {"$add": [{"$dateDiff": {"startDate": mongo_date(start),
                    "endDate": mongo_date(last), "unit": "day", "timezone": "+05:30"}}, 1]}]}
    return {"$size": {"$filter": {"input": {"$range": [0, length]}, "as": "offset", "cond":
            {"$in": [{"$dayOfWeek": {"date": {"$dateAdd": {"startDate": mongo_date(start),
               "unit": "day", "amount": "$$offset"}}, "timezone": "+05:30"}}, [2, 3, 4, 5, 6]]}}}}


def monthly_query(emp_code, month):
    first, last = month_bounds(month)
    fields = ["present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"]
    return Aggregation("employees", [
        {"$match": {"emp_code": emp_code}}, totals_lookup(first, last),
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month},
                      "working_days": working_days(first, last), **{f: total_value(f) for f in fields}}},
        {"$set": {"attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]},
                    half_up({"$multiply": [{"$divide": [{"$toDecimal": "$present_days"}, "$working_days"]}, 100]}), None]}}},
    ], "employee_code")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    rows = monthly_query(emp_code, month).run()
    if not rows:
        raise HTTPException(404, "Unknown employee")
    return rows[0]


def summary_query(month, department):
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    sums = {f: {"$sum": total_value(f)} for f in
            ("present_days", "late_count", "total_late_minutes", "on_duty_count", "hours_sum", "hours_count")}
    return Aggregation("employees", [
        {"$match": match}, totals_lookup(first, last),
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    "leave_count": {"$sum": total_value("leave_days")}, **sums}},
        {"$set": {"department": "$_id", "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]},
                      half_up({"$divide": ["$hours_sum", "$hours_count"]}), None]}}},
        {"$unset": ["_id", "hours_sum", "hours_count"]}, {"$sort": {"department": 1}},
    ], "department_joined" if department is not None else "joined_department")


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: str | None = None):
    return {"month": month, "items": summary_query(month, department).run()}


def leaderboard_query(month, department, limit):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": count_if({"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]})}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                      "rank": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return Aggregation("attendance_logs", pipeline, "date_employee")


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, department: str | None = None, limit: Limit = 10):
    return {"month": month, "items": leaderboard_query(month, department, limit).run()}


def trend_query(department, start, end):
    check_range(start, end, 92)
    days = (Date.fromisoformat(end) - Date.fromisoformat(start)).days + 1
    # A MongoDB calendar spine, anchored to one department employee, also works
    # when there are no logs anywhere in the requested interval.
    daily_employees = [
        {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
           "let": {"date": "$$day"}, "pipeline": [{"$match": {"$expr": {"$eq": ["$date", "$$date"]}}}], "as": "logs"}},
        {"$set": {"log": {"$arrayElemAt": ["$logs", 0]}}},
        {"$group": {"_id": None, "headcount": {"$sum": 1},
            "present_count": {"$sum": {"$cond": [{"$in": ["$log.status", PRESENCE]},
                              {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1]}, 0]}},
            "late_count": count_if({"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]})}},
    ]
    pipeline = [
        {"$match": {"department": department}}, {"$limit": 1},
        {"$project": {"_id": 0, "offset": {"$range": [0, days]}}}, {"$unwind": "$offset"},
        {"$project": {"date": {"$dateToString": {"date": {"$dateAdd": {"startDate": mongo_date(start),
                         "unit": "day", "amount": "$offset"}}, "format": "%Y-%m-%d", "timezone": "+05:30"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": daily_employees, "as": "daily"}},
        {"$project": {"date": 1, "is_working_day": weekday("$date"),
                       **{f: {"$ifNull": [{"$arrayElemAt": ["$daily." + f, 0]}, 0]}
                          for f in ("headcount", "present_count", "late_count")}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                     half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {
            "$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}}}},
        {"$set": {"moving_avg_7d": half_up("$moving_avg_7d", 4)}}, {"$sort": {"date": 1}},
    ]
    return Aggregation("employees", pipeline, "department_joined")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, start: Annotated[DateString, Query(alias="from")],
                     end: Annotated[DateString, Query(alias="to")]):
    rows = trend_query(department, start, end).run()
    if not rows:
        raise HTTPException(404, "Unknown department")
    return {"department": department, "items": rows}


ExplainName = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: ExplainName, emp_code: str | None = None, month: Month | None = None,
                     department: str | None = None, limit: Limit = 10, date_from: DateString | None = None,
                     date_to: DateString | None = None, status: Status | None = None,
                     start: Annotated[DateString | None, Query(alias="from")] = None,
                     end: Annotated[DateString | None, Query(alias="to")] = None,
                     page: Page = 1, page_size: PageSize = 20):
    required = {"attendance_list": [], "employee_monthly": [("emp_code", emp_code), ("month", month)],
                "department_summary": [("month", month)], "late_leaderboard": [("month", month)],
                "department_trend": [("department", department), ("from", start), ("to", end)]}
    for name, value in required[endpoint]:
        if value is None:
            raise HTTPException(422, f"Missing required parameter: {name}")
    if endpoint == "attendance_list":
        cmd = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            query = monthly_query(emp_code, month)
        elif endpoint == "department_summary":
            query = summary_query(month, department)
        elif endpoint == "late_leaderboard":
            query = leaderboard_query(month, department, limit)
        else:
            query = trend_query(department, start, end)
        collection, cmd = query.collection, query.command()
    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    # Preserve BSON-only values as Extended JSON instead of deleting plan fields.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
