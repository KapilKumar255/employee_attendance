"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from enum import Enum
from decimal import Decimal, ROUND_HALF_UP
from datetime import date as calendar_date
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException , Query
from pydantic import BaseModel, Field, StrictInt ,  field_validator
from pymongo import MongoClient, ReturnDocument ,  ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

# client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("MONGO_DB")]

try:
    client.admin.command('ping')
    print("MongoDB connected successfully!")
except Exception as e:
    print("MongoDB connection failed:")
    print(e)

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

db.employees.create_index(
    [("emp_code", ASCENDING)],
    unique=True
)

db.attendance_logs.create_index(
    [("emp_code", ASCENDING), ("date", ASCENDING)],
    unique=True
)

db.attendance_logs.create_index(
    [("date", DESCENDING), ("emp_code", ASCENDING)]
)



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def parse_shift_time(value: str):
    try:
        h, m = map(int, value.split(":"))
        return h, m
    except Exception:
        raise ValueError("Invalid time format. Expected HH:MM")


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = parse_shift_time(shift_start)

    start = punch_in.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    minutes = int((punch_in - start).total_seconds() / 60)

    return minutes if minutes > 10 else 0 


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime
) -> float:
    return round(
        (punch_out - punch_in).total_seconds() / 3600,
        2
    )


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str
) -> int:

    h, m = parse_shift_time(shift_end)

    end = datetime.fromisoformat(date_str).replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
        tzinfo=IST
    )

    return max(
        0,
        int((punch_out - end).total_seconds() / 60)
    )


def current_ist():
    return datetime.now(IST)


def as_utc(value: datetime) -> datetime:
    """Treat naive MongoDB datetimes as UTC and normalize all instants to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def epoch_millis(value: datetime) -> int:
    return int(as_utc(value).timestamp() * 1000)


def punch_out_shift_end(record_date: str, shift_end: str, shift_start: str) -> datetime:
    end_date = datetime.fromisoformat(record_date).date()
    end_time = datetime.strptime(shift_end, "%H:%M").time()
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    if end_time <= start_time:
        end_date += timedelta(days=1)
    return datetime.combine(end_date, end_time, tzinfo=IST)


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local_time = as_utc(punch_in).astimezone(IST)
    attendance_date = local_time.date()
    end_time = datetime.strptime(shift_end, "%H:%M").time()
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    if end_time <= start_time and local_time.time() < end_time:
        attendance_date -= timedelta(days=1)
    return attendance_date.isoformat()


def compute_regularized_values(
    record_date: str,
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    employee: dict[str, str],
) -> dict[str, Any]:
    if status in {"ABSENT", "LEAVE"}:
        return {
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    if punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    late_minutes = compute_late_minutes(
        as_utc(punch_in).astimezone(IST),
        employee["shift_start"],
    )
    values: dict[str, Any] = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        if punch_out <= punch_in or punch_out - punch_in > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
        work_hours = compute_work_hours_half_up(punch_in, punch_out)
        shift_end = punch_out_shift_end(
            record_date,
            employee["shift_end"],
            employee["shift_start"],
        )
        overtime_elapsed = int(
            (as_utc(punch_out) - shift_end.astimezone(timezone.utc)).total_seconds() // 60
        )
        values.update(
            {
                "punch_out": punch_out,
                "work_hours": work_hours,
                "overtime_minutes": overtime_elapsed if overtime_elapsed >= 30 else 0,
                "half_day": work_hours < 4.50,
            }
        )
    return values


def compute_work_hours_half_up(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((as_utc(punch_out) - as_utc(punch_in)).total_seconds()))
    hours = (seconds / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert MongoDB attendance datetimes, including nested history values, to API values."""
    def convert(value: Any) -> Any:
        if isinstance(value, datetime):
            return epoch_millis(value)
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items() if key != "_id"}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return convert(doc)


def month_bounds(month: str) -> tuple[calendar_date, calendar_date]:
    try:
        year, month_number = map(int, month.split("-"))
        if len(month) != 7 or month[4] != "-" or not 1 <= month_number <= 12:
            raise ValueError
        first_day = calendar_date(year, month_number, 1)
    except (TypeError, ValueError):
        raise HTTPException(422, "month must be YYYY-MM")

    if month_number == 12:
        next_month = calendar_date(year + 1, 1, 1)
    else:
        next_month = calendar_date(year, month_number + 1, 1)
    return first_day, next_month - timedelta(days=1)


def round_half_up(value: Decimal, places: str) -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AttendanceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


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

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_time_format(cls, value):
        parse_shift_time(value)
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_difference(cls, value, info):
        shift_start = info.data.get("shift_start")

        if shift_start == value:
            raise ValueError(
                "shift_start must differ from shift_end"
            )

        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():

    try:
        db.command("ping")
        return {"status": "ok"}

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="database unavailable"
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):

    doc = body.model_dump()

    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)

    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        )

    doc.pop("_id", None)

    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    q = {}

    if department:
        q["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(q)

    items = list(
        db.employees
        .find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):

    emp = db.employees.find_one(
        {"emp_code": body.emp_code},
        {"_id": 0}
    )

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="employee not found"
        )

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=IST
        )
    else:
        ts = current_ist()

    attendance_date = ts.date().isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts,
            emp["shift_start"]
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)

    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date"
        )

    doc["id"] = str(result.inserted_id)

    return doc



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):

    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from cannot be greater than date_to"
            )

    if date_from or date_to:

        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from

        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)

    skip = (page - 1) * page_size

    docs = list(
        db.attendance_logs
        .find(q)
        .sort([
            ("date", DESCENDING),
            ("emp_code", ASCENDING)
        ])
        .skip(skip)
        .limit(page_size)
    )

    for doc in docs:
        doc["id"] = str(doc.pop("_id"))

    return {
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if db.employees.find_one({"emp_code": body.emp_code}) is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is None:
        punched_at = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        punched_at = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")

    punch_in_at = as_utc(record["punch_in"])
    elapsed = punched_at - punch_in_at
    if elapsed <= timedelta(0) or elapsed > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    employee = db.employees.find_one({"emp_code": body.emp_code}, {"shift_start": 1, "shift_end": 1})
    shift_end = punch_out_shift_end(
        record["date"],
        employee["shift_end"],
        employee["shift_start"],
    )
    overtime_elapsed = int((punched_at - shift_end.astimezone(timezone.utc)).total_seconds() // 60)
    overtime_minutes = overtime_elapsed if overtime_elapsed >= 30 else 0
    work_hours = compute_work_hours_half_up(punch_in_at, punched_at)
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}],
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        calendar_date.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    supplied_fields = body.model_fields_set
    if (
        body.status in {"ABSENT", "LEAVE"}
        and ("punch_in" in supplied_fields or "punch_out" in supplied_fields)
    ):
        raise HTTPException(422, "ABSENT or LEAVE records cannot have punch times")

    def request_instant(field_name: str, current: Optional[datetime]) -> Optional[datetime]:
        if field_name not in supplied_fields:
            return current
        value = getattr(body, field_name)
        if value is None:
            return None
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0)

    final_status = body.status if "status" in supplied_fields else record["status"]
    final_punch_in = request_instant("punch_in", record.get("punch_in"))
    final_punch_out = request_instant("punch_out", record.get("punch_out"))

    if final_status in {"ABSENT", "LEAVE"} and (
        final_punch_in is not None or final_punch_out is not None
    ):
        raise HTTPException(422, "ABSENT or LEAVE records cannot have punch times")
    if final_punch_in is not None and attendance_date_for_punch(
        final_punch_in,
        employee["shift_start"],
        employee["shift_end"],
    ) != date:
        raise HTTPException(422, "punch_in must belong to the attendance date")

    final_values = compute_regularized_values(
        date,
        final_status,
        final_punch_in,
        final_punch_out,
        employee,
    )

    current_values = {
        field: record.get(field)
        for field in (
            "status",
            "punch_in",
            "punch_out",
            "work_hours",
            "late_minutes",
            "overtime_minutes",
            "half_day",
        )
    }
    changes = {
        field: {"from": current_values[field], "to": final_values[field]}
        for field in current_values
        if current_values[field] != final_values[field]
    }
    if not changes:
        raise HTTPException(422, "request does not change the attendance record")

    history = record.get("history", [])
    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": history},
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record was changed concurrently")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(404, "employee not found")

    first_day, last_day = month_bounds(month)
    joined_on = calendar_date.fromisoformat(employee["joined_on"])
    working_start = max(first_day, joined_on)
    working_days = (
        sum(
            (working_start + timedelta(days=offset)).weekday() < 5
            for offset in range((last_day - working_start).days + 1)
        )
        if working_start <= last_day
        else 0
    )

    grouped_logs = db.attendance_logs.aggregate(
        [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": first_day.isoformat(),
                        "$lte": last_day.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": ["$half_day", 0.5, 1]},
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]
    )

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    for daily in grouped_logs:
        if calendar_date.fromisoformat(daily["_id"]).weekday() < 5:
            present_days += daily["present_days"]
        leave_days += daily["leave_days"]
        late_count += daily["late_count"]
        total_late_minutes += daily["total_late_minutes"]
        total_overtime_minutes += daily["total_overtime_minutes"]

    attendance_pct = None
    if working_days:
        attendance_pct = round_half_up(
            Decimal(str(present_days / working_days * 100)),
            "0.01",
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(Decimal(str(present_days)), "0.01"),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first_day, last_day = month_bounds(month)
    employee_query: dict[str, Any] = {"joined_on": {"$lte": last_day.isoformat()}}
    if department is not None:
        employee_query["department"] = department

    employees = list(
        db.employees.find(
            employee_query,
            {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1},
        )
    )
    employee_codes = [employee["emp_code"] for employee in employees]
    summaries: dict[str, dict[str, Any]] = {}

    for employee in employees:
        summaries.setdefault(
            employee["department"],
            {
                "department": employee["department"],
                "headcount": 0,
                "present_days": 0.0,
                "work_hours": [],
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            },
        )
        summaries[employee["department"]]["headcount"] += 1

    if employee_codes:
        logs = db.attendance_logs.aggregate(
            [
                {
                    "$match": {
                        "emp_code": {"$in": employee_codes},
                        "date": {
                            "$gte": first_day.isoformat(),
                            "$lte": last_day.isoformat(),
                        },
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "employee",
                    }
                },
                {"$unwind": "$employee"},
                {
                    "$project": {
                        "_id": 0,
                        "date": 1,
                        "status": 1,
                        "half_day": 1,
                        "work_hours": 1,
                        "late_minutes": 1,
                        "employee_department": "$employee.department",
                        "joined_on": "$employee.joined_on",
                    }
                },
            ]
        )

        for log in logs:
            department_data = summaries[log["employee_department"]]
            log_date = calendar_date.fromisoformat(log["date"])
            joined_on = calendar_date.fromisoformat(log["joined_on"])

            if log_date >= joined_on and log_date.weekday() < 5:
                if log.get("status") in {"PRESENT", "WFH", "ON_DUTY"}:
                    department_data["present_days"] += (
                        0.5 if log.get("half_day", False) else 1
                    )

            if (
                log.get("status") in {"PRESENT", "WFH", "ON_DUTY"}
                and log.get("work_hours") is not None
            ):
                department_data["work_hours"].append(log["work_hours"])
            if (log.get("late_minutes") or 0) > 0:
                department_data["late_count"] += 1
                department_data["total_late_minutes"] += log["late_minutes"]
            if log.get("status") == "LEAVE":
                department_data["leave_count"] += 1
            if log.get("status") == "ON_DUTY":
                department_data["on_duty_count"] += 1

    items = []
    for name in sorted(summaries):
        summary = summaries[name]
        average_work_hours = None
        if summary["work_hours"]:
            total_work_hours = sum(
                (Decimal(str(hours)) for hours in summary["work_hours"]),
                Decimal("0"),
            )
            average_work_hours = round_half_up(
                total_work_hours / Decimal(len(summary["work_hours"])),
                "0.01",
            )
        items.append(
            {
                "department": summary["department"],
                "headcount": summary["headcount"],
                "present_days": round_half_up(
                    Decimal(str(summary["present_days"])),
                    "0.01",
                ),
                "avg_work_hours": average_work_hours,
                "late_count": summary["late_count"],
                "total_late_minutes": summary["total_late_minutes"],
                "leave_count": summary["leave_count"],
                "on_duty_count": summary["on_duty_count"],
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    if limit < 1 or limit > 50:
        raise HTTPException(422, "limit must be between 1 and 50")

    first_day, last_day = month_bounds(month)
    match_stage: dict[str, Any] = {
        "date": {
            "$gte": first_day.isoformat(),
            "$lte": last_day.isoformat(),
        },
        "late_minutes": {"$gt": 0},
    }
    pipeline: list[dict[str, Any]] = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$emp_code",
                    "name": {"$first": "$employee.name"},
                    "department": {"$first": "$employee.department"},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
        ]
    )

    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    previous_total: Optional[int] = None
    rank = 0
    for position, row in enumerate(rows, start=1):
        total_late_minutes = row["total_late_minutes"]
        if total_late_minutes != previous_total:
            rank = position
            previous_total = total_late_minutes
        if rank > limit:
            break
        items.append(
            {
                "rank": rank,
                "emp_code": row["_id"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": total_late_minutes,
                "late_count": row["late_count"],
            }
        )

    return {"month": month, "items": items}



@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start_date = calendar_date.fromisoformat(from_date)
        end_date = calendar_date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "from and to must be YYYY-MM-DD")

    if end_date < start_date:
        raise HTTPException(422, "to must not be before from")
    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(422, "trend range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")

    start_instant = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end_instant = datetime.combine(
        end_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    start_text = start_date.isoformat()
    end_text = end_date.isoformat()

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_text, "$lte": end_text},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date",
                "partitionByFields": [],
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_instant, end_instant],
                },
            }
        },
        {
            "$set": {
                "date_text": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                        "timezone": "UTC",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_date": "$date_text"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$match": {
                            "$expr": {"$lte": ["$joined_on", "$$trend_date"]}
                        }
                    },
                    {"$count": "value"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$headcount_result.value", 0]}, 0]
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_text",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": ["$attendance_rate", 10000]}, 0.5]}},
                                10000,
                            ]
                        },
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}},
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    return {"department": department, "items": list(db.attendance_logs.aggregate(pipeline))}












# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
