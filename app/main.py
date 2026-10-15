import os
import json
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Literal, Any
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()

mongo_uri = os.getenv("MONGO_URI")
mongo_db = os.getenv("MONGO_DB")

if not mongo_uri or not mongo_db:
    raise RuntimeError("MONGO_URI and MONGO_DB must be configured.")

client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[mongo_db]

IST = timezone(timedelta(hours=5, minutes=30))
EMAIL_REGEX = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
EPOCH_MS_MIN = 100000000000
EPOCH_MS_MAX = 4102444800000

AttendanceStatus = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Employees collection indexes
    db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", 1)], name="idx_employees_department")
    db.employees.create_index([("joined_on", 1)], name="idx_employees_joined_on")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_employees_dept_joined")

    # Attendance logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_emp_code_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_attendance_date_emp")
    db.attendance_logs.create_index([("date", 1)], name="idx_attendance_date")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], name="idx_attendance_emp_date")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_attendance_emp_punch_in")

    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def epoch_ms_to_datetime(timestamp_ms: int) -> datetime:
    
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
   
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def truncate_to_seconds(value: datetime) -> datetime:

    return value.replace(microsecond=0)


def calculate_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_attendance_date_for_punch_in(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
   
    punch_in_ist = punch_in.astimezone(IST)
    att_date = punch_in_ist.date()

    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight and (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_h, end_m, 0):
        att_date -= timedelta(days=1)

    return att_date.isoformat()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    
    punch_in_ist = punch_in.astimezone(IST)
    hours, minutes = map(int, shift_start.split(":"))
    shift_day = date.fromisoformat(attendance_date)
    shift_start_dt = datetime(
        shift_day.year,
        shift_day.month,
        shift_day.day,
        hours,
        minutes,
        0,
        tzinfo=IST,
    )
    delay_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if delay_seconds <= 600:
        return 0
    return max(0, int(delay_seconds // 60))


def calculate_overtime(
    punch_in: datetime,
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    
    punch_out_ist = punch_out.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    att_day = date.fromisoformat(attendance_date)
    end_day = att_day + timedelta(days=1) if is_overnight else att_day

    shift_end_dt = datetime(
        end_day.year,
        end_day.month,
        end_day.day,
        end_h,
        end_m,
        0,
        tzinfo=IST,
    )
    extra_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if extra_seconds < 1800:
        return 0
    return max(0, int(extra_seconds // 60))


def values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return truncate_to_seconds(a) == truncate_to_seconds(b)
    return a == b


def format_attendance_record(record: dict) -> dict:
    
    res = dict(record)
    res.pop("_id", None)
    res["punch_in"] = datetime_to_epoch_ms(res.get("punch_in"))
    res["punch_out"] = datetime_to_epoch_ms(res.get("punch_out"))
    res["late_minutes"] = res.get("late_minutes", 0)
    res["overtime_minutes"] = res.get("overtime_minutes", 0)
    res["half_day"] = res.get("half_day", False)

    history = []
    for entry in res.get("history", []):
        entry_copy = dict(entry)
        if isinstance(entry_copy.get("at"), datetime):
            entry_copy["at"] = datetime_to_epoch_ms(entry_copy["at"])
        changes = {}
        for k, v in entry_copy.get("changes", {}).items():
            ch = dict(v)
            if k in ("punch_in", "punch_out"):
                if isinstance(ch.get("from"), datetime):
                    ch["from"] = datetime_to_epoch_ms(ch["from"])
                if isinstance(ch.get("to"), datetime):
                    ch["to"] = datetime_to_epoch_ms(ch["to"])
            changes[k] = ch
        entry_copy["changes"] = changes
        history.append(entry_copy)
    res["history"] = history
    return res


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #

def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    month_start = f"{month}-01"
    year = int(month[:4])
    month_number = int(month[5:7])
    if month_number == 12:
        next_month = f"{year + 1}-01-01"
    else:
        next_month = f"{year}-{month_number + 1:02d}-01"

    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$addFields": {
                "_month_start": {"$dateFromString": {"dateString": month_start}},
                "_next_month_start": {"$dateFromString": {"dateString": next_month}},
                "_joined_date": {"$dateFromString": {"dateString": "$joined_on"}},
            }
        },
        {
            "$addFields": {
                "_working_start": {
                    "$cond": [
                        {"$gt": ["$_joined_date", "$_month_start"]},
                        "$_joined_date",
                        "$_month_start",
                    ]
                }
            }
        },
        {
            "$addFields": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": {
                                        "$range": [
                                            0,
                                            {
                                                "$max": [
                                                    0,
                                                    {
                                                        "$dateDiff": {
                                                            "startDate": "$_working_start",
                                                            "endDate": "$_next_month_start",
                                                            "unit": "day",
                                                        }
                                                    },
                                                ]
                                            },
                                        ]
                                    },
                                    "as": "day",
                                    "in": {
                                        "$dateAdd": {
                                            "startDate": "$_working_start",
                                            "unit": "day",
                                            "amount": "$$day",
                                        }
                                    },
                                }
                            },
                            "as": "day_date",
                            "cond": {
                                "$in": [
                                    {"$dayOfWeek": "$$day_date"},
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                        }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "start_date": month_start,
                    "end_date": next_month,
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", "$$start_date"]},
                                    {"$lt": ["$date", "$$end_date"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs",
            }
        },
        {
            "$addFields": {
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$_logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$_logs.late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$_logs.overtime_minutes"
                },
            }
        },
        {
            "$addFields": {
                "attendance_pct": {
                    "$cond": [
                        {"$eq": ["$working_days", 0]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            {
                                                                "$multiply": [
                                                                    "$present_days",
                                                                    100,
                                                                ]
                                                            },
                                                            "$working_days",
                                                        ]
                                                    },
                                                    100,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": 1,
            }
        },
    ]


def build_department_summary_pipeline(
    month: str,
    department: Optional[str] = None,
) -> list:
    year = int(month[:4])
    month_number = int(month[5:7])
    if month_number == 12:
        next_month = f"{year + 1}-01-01"
    else:
        next_month = f"{year}-{month_number + 1:02d}-01"

    employee_match: dict[str, Any] = {"joined_on": {"$lt": next_month}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lt": ["$date", next_month]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs",
            }
        },
        {
            "$addFields": {
                "_present_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$log.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$in": [
                                        {
                                            "$dayOfWeek": {
                                                "$dateFromString": {
                                                    "dateString": "$$log.date"
                                                }
                                            }
                                        },
                                        [2, 3, 4, 5, 6],
                                    ]
                                },
                            ]
                        },
                    }
                },
                "_late_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {
                            "$gt": [
                                {"$ifNull": ["$$log.late_minutes", 0]},
                                0,
                            ]
                        },
                    }
                },
                "_leave_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {"$eq": ["$$log.status", "LEAVE"]},
                    }
                },
                "_duty_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                    }
                },
                "_hours_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$log.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {"$ne": ["$$log.work_hours", None]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$addFields": {
                "_employee_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$_present_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                        }
                    }
                },
                "_employee_late_count": {"$size": "$_late_logs"},
                "_employee_late_minutes": {"$sum": "$_late_logs.late_minutes"},
                "_employee_leave_count": {"$size": "$_leave_logs"},
                "_employee_duty_count": {"$size": "$_duty_logs"},
                "_hours_sum": {"$sum": "$_hours_logs.work_hours"},
                "_hours_count": {"$size": "$_hours_logs"},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$_employee_present_days"},
                "late_count": {"$sum": "$_employee_late_count"},
                "total_late_minutes": {"$sum": "$_employee_late_minutes"},
                "leave_count": {"$sum": "$_employee_leave_count"},
                "on_duty_count": {"$sum": "$_employee_duty_count"},
                "hours_sum": {"$sum": "$_hours_sum"},
                "hours_count": {"$sum": "$_hours_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$hours_count", 0]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$hours_sum",
                                                            "$hours_count",
                                                        ]
                                                    },
                                                    100,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                100,
                            ]
                        },
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> list:
    year = int(month[:4])
    month_number = int(month[5:7])
    if month_number == 12:
        next_month = f"{year + 1}-01-01"
    else:
        next_month = f"{year}-{month_number + 1:02d}-01"

    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lt": next_month}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
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
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$_employee.name",
                "department": "$_employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    if department is not None:
        pipeline.append({"$match": {"department": department}})

    pipeline.append({
        "$setWindowFields": {
            "partitionBy": None,
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }
    })

    pipeline.extend([
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return pipeline


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
) -> list:
    start_dt = datetime.strptime(from_date, "%Y-%m-%d")
    end_dt = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)

    return [
       
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
       
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "_emp",
            }
        },
        {"$unwind": "$_emp"},
        {"$match": {"_emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
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
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
      
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]},
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "_hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$_hc.count", 0]}, 0]},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$present_count", "$headcount"]},
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
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
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$moving_avg_7d", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_REGEX)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=EPOCH_MS_MIN,
        le=EPOCH_MS_MAX,
    )
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=EPOCH_MS_MIN,
        le=EPOCH_MS_MAX,
    )


class RegularizeRequest(BaseModel):
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    punch_out: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(BaseModel):
    month: str
    items: list[LeaderboardItem]


class DepartmentTrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class DepartmentTrendResponse(BaseModel):
    department: str
    items: list[DepartmentTrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(created_at)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size

    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = list(cursor)

    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = datetime_to_epoch_ms(item["created_at"])

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        punch_time = datetime.now(timezone.utc)
    else:
        punch_time = epoch_ms_to_datetime(body.punched_at)

    punch_time = truncate_to_seconds(punch_time)
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    date_str = get_attendance_date_for_punch_in(punch_time, shift_start, shift_end)
    late_minutes = compute_late_minutes(punch_time, shift_start, date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        punch_out_time = datetime.now(timezone.utc)
    else:
        punch_out_time = epoch_ms_to_datetime(body.punched_at)

    punch_out_time = truncate_to_seconds(punch_out_time)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_time},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(status_code=404, detail="No eligible punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)
    punch_in_time = truncate_to_seconds(punch_in_time)

    elapsed_seconds = (punch_out_time - punch_in_time).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 86400:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    work_hours = calculate_work_hours(punch_in_time, punch_out_time)
    overtime_minutes = calculate_overtime(
        punch_in_time,
        punch_out_time,
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
    )
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_time,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")

    return format_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        date_filter: dict[str, str] = {}
        if date_from:
            date_filter["$gte"] = date_from.isoformat()
        if date_to:
            date_filter["$lte"] = date_to.isoformat()
        query["date"] = date_filter

    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs
        .find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_record(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    supplied = body.model_fields_set

    if "punch_in" in supplied and body.punch_in is None:
        raise HTTPException(status_code=422, detail="punch_in cannot be null")
    if "punch_out" in supplied and body.punch_out is None:
        raise HTTPException(status_code=422, detail="punch_out cannot be null")
    if "status" in supplied and body.status is None:
        raise HTTPException(status_code=422, detail="status cannot be null")

    if body.status in ("ABSENT", "LEAVE") and ("punch_in" in supplied or "punch_out" in supplied):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot include punch times")

    old_status = record.get("status", "PRESENT")
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    if old_in is not None and old_in.tzinfo is None:
        old_in = old_in.replace(tzinfo=timezone.utc)
    if old_out is not None and old_out.tzinfo is None:
        old_out = old_out.replace(tzinfo=timezone.utc)

    old_in = truncate_to_seconds(old_in) if old_in is not None else None
    old_out = truncate_to_seconds(old_out) if old_out is not None else None

    new_status = body.status if "status" in supplied else old_status

    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None
    else:
        new_in = (
            truncate_to_seconds(epoch_ms_to_datetime(body.punch_in))
            if "punch_in" in supplied
            else old_in
        )
        new_out = (
            truncate_to_seconds(epoch_ms_to_datetime(body.punch_out))
            if "punch_out" in supplied
            else old_out
        )

    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_in is None:
        raise HTTPException(status_code=422, detail="Presence status requires punch_in")

    if "punch_in" in supplied and new_in is not None:
        computed_date = get_attendance_date_for_punch_in(new_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in must fall on the attendance date")

    if new_in is None and new_out is not None:
        raise HTTPException(status_code=422, detail="punch_out requires punch_in")

    if new_in is not None and new_out is not None:
        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 86400:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    if new_in is not None:
        late_minutes = compute_late_minutes(new_in, emp["shift_start"], date)
    else:
        late_minutes = 0

    if new_in is not None and new_out is not None:
        work_hours = calculate_work_hours(new_in, new_out)
        overtime_minutes = calculate_overtime(
            new_in,
            new_out,
            emp["shift_start"],
            emp["shift_end"],
            date,
        )
        half_day = work_hours < 4.50
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes: dict[str, dict[str, Any]] = {}
    for field in old_values:
        before = old_values[field]
        after = new_values[field]
        if not values_equal(before, after):
            changes[field] = {"from": before, "to": after}

    if not changes:
        raise HTTPException(status_code=422, detail="Regularization does not change anything")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    query = {"_id": record["_id"], "history": record.get("history", [])}

    updated = db.attendance_logs.find_one_and_update(
        query,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=409, detail="Attendance changed concurrently; retry the request")

    return format_attendance_record(updated)


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthly,
)
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Calendar month in YYYY-MM format",
    ),
):
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    result = list(db.employees.aggregate(pipeline))

    if not result:
        raise HTTPException(status_code=404, detail="Employee not found")

    return EmployeeMonthly(**result[0])


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummary,
)
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month=month, department=department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get(
    "/analytics/leaderboard/late",
    response_model=LeaderboardResponse,
)
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(
        month=month,
        limit=limit,
        department=department,
    )
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=DepartmentTrendResponse,
)
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format")

    if start.isoformat() != from_date or end.isoformat() != to_date:
        raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format")

    if end < start or (end - start).days > 91:
        raise HTTPException(status_code=422, detail="Invalid date range: maximum 92 days inclusive")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(
        department=department,
        from_date=from_date,
        to_date=to_date,
    )
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse)
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[AttendanceStatus] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

        query: dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            date_filter: dict[str, str] = {}
            if date_from:
                date_filter["$gte"] = date_from.isoformat()
            if date_to:
                date_filter["$lte"] = date_to.isoformat()
            query["date"] = date_filter
        if status is not None:
            query["status"] = status

        skip = (page - 1) * page_size
        raw_explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        collection = "employees"
        raw_explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        collection = "employees"
        raw_explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        collection = "attendance_logs"
        raw_explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        try:
            start = date.fromisoformat(from_date)
            end = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format")
        if start.isoformat() != from_date or end.isoformat() != to_date:
            raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format")
        if end < start or (end - start).days > 91:
            raise HTTPException(status_code=422, detail="Invalid date range: maximum 92 days inclusive")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        collection = "attendance_logs"
        raw_explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    else:
        raise HTTPException(status_code=422, detail="Unsupported endpoint name")

    explain_clean = json.loads(json_util.dumps(raw_explain))
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_clean,
    }
