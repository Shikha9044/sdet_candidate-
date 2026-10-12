"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Literal, Any
from pymongo.errors import DuplicateKeyError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel,Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient,ReturnDocument
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager

load_dotenv()

print("MONGO_URI configured:", bool(os.getenv("MONGO_URI")))

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Unique employee code
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code"
    )

    # One attendance record per employee per date
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_code_date"
    )

    db.attendance_logs.create_index(
    [("date", -1), ("emp_code", 1)],
    name="idx_attendance_date_emp"
)
    db.attendance_logs.create_index(
    [("emp_code", 1), ("date", -1)],
    name="idx_attendance_emp_date"
)

    yield



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


def epoch_ms_to_datetime(timestamp_ms: int) -> datetime:
    """Convert epoch milliseconds to a timezone-aware UTC datetime."""
    seconds = timestamp_ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def truncate_to_seconds(value: datetime) -> datetime:
    """Remove fractional seconds before calculations."""
    return value.replace(microsecond=0)


def calculate_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Calculate work hours with two-decimal half-up rounding."""
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    
def datetime_to_epoch_ms(value):
    """Convert a datetime to epoch milliseconds."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return int(value.timestamp()) * 1000


def values_equal(a, b):
    """Compare values safely for regularization history."""
    if isinstance(a, datetime) and isinstance(b, datetime):
        return truncate_to_seconds(a) == truncate_to_seconds(b)

    return a == b


def calculate_regularized_late_minutes(punch_in, shift_start, shift_end):
    """Calculate late minutes using the 10-minute grace rule."""
    if punch_in is None:
        return 0

    punch_in_ist = punch_in.astimezone(IST)
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    is_overnight = (end_hour, end_minute) <= (
        start_hour, start_minute
    )

    shift_date = punch_in_ist.date()

    # Early-morning punch-in may belong to the previous day's
    # overnight shift.
    if is_overnight and (
        punch_in_ist.hour, punch_in_ist.minute
    ) < (end_hour, end_minute):
        shift_date -= timedelta(days=1)

    shift_start_time = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        start_hour,
        start_minute,
        tzinfo=IST,
    )

    delay_seconds = (
        punch_in_ist - shift_start_time
    ).total_seconds()

    if delay_seconds <= 10 * 60:
        return 0

    return max(0, int(delay_seconds // 60))


def calculate_overtime(
    punch_in: datetime,
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    """Calculate overtime using the employee's shift in IST."""

    punch_in_ist = punch_in.astimezone(IST)
    punch_out_ist = punch_out.astimezone(IST)

    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    is_overnight = (end_hour, end_minute) <= (
        start_hour,
        start_minute,
    )

    end_date = punch_in_ist.date()

    if is_overnight and (
        punch_in_ist.hour,
        punch_in_ist.minute,
    ) >= (start_hour, start_minute):
        end_date += timedelta(days=1)

    shift_end_time = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        end_hour,
        end_minute,
        tzinfo=IST,
    )

    extra_seconds = (
        punch_out_ist - shift_end_time
    ).total_seconds()

    if extra_seconds < 30 * 60:
        return 0

    return int(extra_seconds // 60)
def build_employee_monthly_pipeline(emp_code: str, month: str):
    month_start = f"{month}-01"

    year = int(month[:4])
    month_number = int(month[5:7])

    if month_number == 12:
        next_month = f"{year + 1}-01-01"
    else:
        next_month = f"{year}-{month_number + 1:02d}-01"

    return [
        {
            "$match": {
                "emp_code": emp_code
            }
        },
        {
            "$addFields": {
                "_month_start": {
                    "$dateFromString": {
                        "dateString": month_start
                    }
                },
                "_next_month_start": {
                    "$dateFromString": {
                        "dateString": next_month
                    }
                },
                "_joined_date": {
                    "$dateFromString": {
                        "dateString": "$joined_on"
                    }
                }
            }
        },
        {
            "$addFields": {
                "_working_start": {
                    "$cond": [
                        {"$gt": ["$_joined_date", "$_month_start"]},
                        "$_joined_date",
                        "$_month_start"
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
                                                            "unit": "day"
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    "as": "day",
                                    "in": {
                                        "$dateAdd": {
                                            "startDate": "$_working_start",
                                            "unit": "day",
                                            "amount": "$$day"
                                        }
                                    }
                                }
                            },
                            "as": "day_date",
                            "cond": {
                                "$in": [
                                    {"$dayOfWeek": "$$day_date"},
                                    [2, 3, 4, 5, 6]
                                ]
                            }
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
                    "end_date": next_month
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code"
                                        ]
                                    },
                                    {"$gte": ["$date", "$$start_date"]},
                                    {"$lt": ["$date", "$$end_date"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs"
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
                                                    ["PRESENT", "WFH", "ON_DUTY"]
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
                                                    [2, 3, 4, 5, 6]
                                                ]
                                            }
                                        ]
                                    }
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            }
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]}
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$gt": ["$$log.late_minutes", 0]}
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$_logs.late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$_logs.overtime_minutes"
                }
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
                                                                    100
                                                                ]
                                                            },
                                                            "$working_days"
                                                        ]
                                                    },
                                                    10000
                                                ]
                                            },
                                            0.5
                                        ]
                                    }
                                },
                                10000
                            ]
                        }
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": 1,
                "_month": {"$literal": month}
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "month": "$_month",
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": 1
            }
        }
    ]


def build_department_summary_pipeline(
    month: str,
    department: Optional[str] = None
):
    year = int(month[:4])
    month_number = int(month[5:7])

    if month_number == 12:
        next_month = f"{year + 1}-01-01"
    else:
        next_month = f"{year}-{month_number + 1:02d}-01"

    # Month end is exclusive: joined_on must be before next_month.
    employee_match = {
        "joined_on": {"$lt": next_month}
    }

    if department is not None:
        employee_match["department"] = department

    return [
        # 1. Employees are the starting collection.
        {
            "$match": employee_match
        },

        # 2. Get attendance records for each employee in this month.
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
                                    {"$lt": ["$date", next_month]}
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs"
            }
        },

        # 3. Calculate each employee's attendance statistics.
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
                                        ["PRESENT", "WFH", "ON_DUTY"]
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
                                        [2, 3, 4, 5, 6]
                                    ]
                                }
                            ]
                        }
                    }
                },
                "_late_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {
                            "$gt": [
                                {"$ifNull": ["$$log.late_minutes", 0]},
                                0
                            ]
                        }
                    }
                },
                "_leave_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {"$eq": ["$$log.status", "LEAVE"]}
                    }
                },
                "_duty_logs": {
                    "$filter": {
                        "input": "$_logs",
                        "as": "log",
                        "cond": {"$eq": ["$$log.status", "ON_DUTY"]}
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
                                        ["PRESENT", "WFH", "ON_DUTY"]
                                    ]
                                },
                                {"$ne": ["$$log.work_hours", None]}
                            ]
                        }
                    }
                }
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
                                    1.0
                                ]
                            }
                        }
                    }
                },
                "_employee_late_count": {"$size": "$_late_logs"},
                "_employee_late_minutes": {
                    "$sum": "$_late_logs.late_minutes"
                },
                "_employee_leave_count": {"$size": "$_leave_logs"},
                "_employee_duty_count": {"$size": "$_duty_logs"},
                "_hours_sum": {"$sum": "$_hours_logs.work_hours"},
                "_hours_count": {"$size": "$_hours_logs"}
            }
        },

        # 4. Group employee statistics by department.
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
                "hours_count": {"$sum": "$_hours_count"}
            }
        },

        # 5. Calculate the average across attendance records.
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
                                                            "$hours_count"
                                                        ]
                                                    },
                                                    100
                                                ]
                                            },
                                            0.5
                                        ]
                                    }
                                },
                                100
                            ]
                        }
                    ]
                }
            }
        },

        # 6. Sort departments alphabetically.
        {"$sort": {"department": 1}}
    ]


def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None
):
    year = int(month[:4])
    month_number = int(month[5:7])

    if month_number == 12:
        next_month = f"{year + 1}-01-01"
    else:
        next_month = f"{year}-{month_number + 1:02d}-01"

    pipeline = [
        # 1. Select attendance logs for the requested month.
        {
            "$match": {
                "date": {
                    "$gte": f"{month}-01",
                    "$lt": next_month
                }
            }
        },

        # 2. Calculate total late minutes and late record count.
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },

        # 3. Only employees with positive total late minutes.
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },

        # 4. Join employee details; ignore logs for unknown employees.
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "_employee"
            }
        },
        {
            "$unwind": "$_employee"
        },

        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$_employee.name",
                "department": "$_employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ]

    # 5. Filter department before ranking.
    if department is not None:
        pipeline.append({
            "$match": {"department": department}
        })

    # 6. Rank using MongoDB's window function.
    # $rank gives equal ranks to ties and skips the next rank.
    pipeline.append({
        "$setWindowFields": {
            "partitionBy": "$department" if department is not None else None,
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }
    })

    # 7. Apply limit AFTER ranking, preserving ties.
    pipeline.extend([
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        }
    ])

    return pipeline


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str
):
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)

    # Generate calendar dates for gap filling.
    days = [
        (start + timedelta(days=i)).isoformat()
        for i in range((end - start).days + 1)
    ]

    return [
        # Create one starting row for every requested calendar date.
        {"$documents": [{"date": day} for day in days]},

        # Count employees in the department who had joined by each date.
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {
                                "$lte": ["$joined_on", "$$day"]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "_headcount"
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$_headcount.count", 0]},
                        0
                    ]
                },
                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$date"
                                }
                            }
                        },
                        [2, 3, 4, 5, 6]
                    ]
                }
            }
        },

        # Aggregate attendance for this department and date.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": from_date, "$lte": to_date},
                            "$expr": {"$eq": ["$date", "$$day"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "_employee"
                        }
                    },
                    {"$unwind": "$_employee"},
                    {
                        "$match": {
                            "_employee.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"]
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1.0
                                            ]
                                        },
                                        0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "_daily"
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$_daily.present_count", 0]},
                        0
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$_daily.late_count", 0]},
                        0
                    ]
                }
            }
        },

        # Rate is only defined on working days with positive headcount.
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount"
                                    ]
                                },
                                4
                            ]
                        },
                        None
                    ]
                }
            }
        },

        # Calculate the 7-row moving average within the requested range.
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
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
                "moving_avg_7d": 1
            }
        },
        {"$sort": {"date": 1}}
    ]

def explain_aggregation(collection_name: str, pipeline: list):
    return db.command(
        "explain",
        {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {}
        },
        verbosity="executionStats"
    )

def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str
) -> int:
    # Convert punch-in to IST
    local_punch = punch_in.astimezone(IST)

    # Parse employee's shift start time
    hours, minutes = map(int, shift_start.split(":"))

    # Create the shift start datetime for attendance date
    shift_day = date.fromisoformat(attendance_date)
    shift_start_time = datetime(
        shift_day.year,
        shift_day.month,
        shift_day.day,
        hours,
        minutes,
        tzinfo=IST
    )

    # Calculate delay in seconds
    elapsed_seconds = (
        local_punch - shift_start_time
    ).total_seconds()

    # 10-minute grace period
    if elapsed_seconds <= 600:
        return 0

    # Return completed late minutes
    return int(elapsed_seconds // 60)

def get_attendance_date_for_punch_in(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """
    Return the attendance date for a punch-in.
    For overnight shifts, early-morning punches belong
    to the previous attendance date.
    """
    punch_in_ist = punch_in.astimezone(IST)
    punch_in_date = punch_in_ist.date()

    start_time = datetime.strptime(
        shift_start, "%H:%M"
    ).time()

    end_time = datetime.strptime(
        shift_end, "%H:%M"
    ).time()

    is_overnight = end_time <= start_time

    if is_overnight and punch_in_ist.time() < end_time:
        punch_in_date -= timedelta(days=1)

    return punch_in_date.isoformat()


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
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
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )


class RegularizeRequest(BaseModel):
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None

    punch_in: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )

    punch_out: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )

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
    explainVersion: Optional[str] = None
    queryPlanner: Optional[dict[str, Any]] = None
    executionStats: Optional[dict[str, Any]] = None
    command: Optional[dict[str, Any]] = None
    serverInfo: Optional[dict[str, Any]] = None
    model_config = {"extra": "allow"}



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        result=client.admin.command("ping")
        print("MongoDB ping successful:", result)
        return {"status": "ok"}
    except Exception as e:
        print("MongoDB ping failed:", repr(e))
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable"
        )
    
    


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()

    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_at

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        )

    doc.pop("_id", None)
    doc["created_at"] = int(created_at.timestamp()) * 1000

    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    # Exact, case-sensitive department filter
    if department is not None:
        query["department"] = department

    # Count only records matching the filter
    total = db.employees.count_documents(query)

    # Page 1 starts at offset 0
    skip = (page - 1) * page_size

    # Sort and paginate inside MongoDB
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
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
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    if body.punched_at is None:
        punch_time = datetime.now(timezone.utc)
    else:
        punch_time = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc
        )

    # Truncate to whole seconds
    punch_time = punch_time.replace(microsecond=0)
    local_time = punch_time.astimezone(IST)

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    start_minutes = (
        int(shift_start[:2]) * 60 + int(shift_start[3:])
    )
    end_minutes = (
        int(shift_end[:2]) * 60 + int(shift_end[3:])
    )

    attendance_day = local_time.date()

    # Early-morning punch-in for overnight shift
    # belongs to the previous day's shift.
    if (
        end_minutes <= start_minutes
        and local_time.hour * 60 + local_time.minute < end_minutes
    ):
        attendance_day -= timedelta(days=1)

    date_str = attendance_day.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punch_time, shift_start, date_str
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date"
        )

    # Never return MongoDB's internal _id.
    doc.pop("_id", None)

    # API uses epoch milliseconds for timestamps.
    doc["punch_in"] = int(punch_time.timestamp()) * 1000

    return doc



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # 1. Validate date range
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )

    # 2. Build MongoDB filters
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        date_filter = {}
        if date_from:
            date_filter["$gte"] = date_from.isoformat()
        if date_to:
            date_filter["$lte"] = date_to.isoformat()
        query["date"] = date_filter

    if status:
        query["status"] = status

    # 3. Count only records matching the filters
    total = db.attendance_logs.count_documents(query)

    # 4. Apply sorting and pagination in MongoDB
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs
        .find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = list(cursor)

    # 5. Convert timestamps to epoch milliseconds
    for item in items:
        for field in ("punch_in", "punch_out"):
            value = item.get(field)
            if value is not None:
                if value.tzinfo is None:
                    from datetime import timezone
                    value = value.replace(tzinfo=timezone.utc)
                item[field] = int(value.timestamp() * 1000)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # 1. Find the employee.
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 2. Get the requested time, or use the current time.
    if body.punched_at is None:
        punch_out_time = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        punch_out_time = epoch_ms_to_datetime(body.punched_at)

    punch_out_time = truncate_to_seconds(punch_out_time)

    # 3. Find the latest punch-in at or before punch-out time.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_time},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No eligible punch-in record found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already punched out",
        )

    punch_in_time = record["punch_in"]

    # MongoDB may decode stored BSON dates as naive UTC datetimes.
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    punch_in_time = truncate_to_seconds(punch_in_time)

    # 4. Validate punch-out time.
    elapsed_seconds = (punch_out_time - punch_in_time).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    # 5. Calculate derived values.
    work_hours = calculate_work_hours(punch_in_time, punch_out_time)

    overtime_minutes = calculate_overtime(
        punch_in_time,
        punch_out_time,
        emp["shift_start"],
        emp["shift_end"],
    )

    half_day = work_hours < 4.50

    # 6. Atomically update only if another request has not punched out.
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
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already punched out",
        )

    # 7. Convert database datetimes to epoch milliseconds for the response.
    def to_epoch_ms(value):
        if value is None:
            return None

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return int(value.timestamp()) * 1000

    updated.pop("_id", None)
    updated["punch_in"] = to_epoch_ms(updated.get("punch_in"))
    updated["punch_out"] = to_epoch_ms(updated.get("punch_out"))

    # History timestamps, if present, must also use epoch milliseconds.
    for entry in updated.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_epoch_ms(entry["at"])

        for change in entry.get("changes", {}).values():
            for key in ("from", "to"):
                if isinstance(change.get(key), datetime):
                    change[key] = to_epoch_ms(change[key])

    return updated

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    # 1. Validate date format
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    # 2. Find employee and attendance record
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    supplied = body.model_fields_set

    # Explicit null values are not valid timestamps.
    if "punch_in" in supplied and body.punch_in is None:
        raise HTTPException(status_code=422, detail="punch_in cannot be null")
    if "punch_out" in supplied and body.punch_out is None:
        raise HTTPException(status_code=422, detail="punch_out cannot be null")
    if "status" in supplied and body.status is None:
        raise HTTPException(status_code=422, detail="status cannot be null")

    # ABSENT and LEAVE cannot be supplied with punch-time fields.
    if body.status in ("ABSENT", "LEAVE") and (
        "punch_in" in supplied or "punch_out" in supplied
    ):
        raise HTTPException(
            status_code=422,
            detail="ABSENT and LEAVE cannot include punch times",
        )

    # 3. Read old values
    old_status = record.get("status", "PRESENT")
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    if old_in is not None and old_in.tzinfo is None:
        old_in = old_in.replace(tzinfo=timezone.utc)
    if old_out is not None and old_out.tzinfo is None:
        old_out = old_out.replace(tzinfo=timezone.utc)

    old_in = truncate_to_seconds(old_in) if old_in is not None else None
    old_out = truncate_to_seconds(old_out) if old_out is not None else None

    # 4. Apply requested changes; omitted fields stay unchanged.
    new_status = body.status if "status" in supplied else old_status

    new_in = (
        truncate_to_seconds(epoch_ms_to_datetime(body.punch_in))
        if "punch_in" in supplied else old_in
    )
    new_out = (
        truncate_to_seconds(epoch_ms_to_datetime(body.punch_out))
        if "punch_out" in supplied else old_out
    )

    if new_status in ("ABSENT", "LEAVE"):
        new_in = None
        new_out = None

    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires punch_in",
        )

    # punch_in must belong to the attendance date in IST.
    if "punch_in" in supplied and new_in is not None:
        if new_in.astimezone(IST).date().isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must fall on the attendance date",
            )

    if new_in is None and new_out is not None:
        raise HTTPException(status_code=422, detail="punch_out requires punch_in")

    if new_in is not None and new_out is not None:
        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    # 5. Recalculate derived fields
    late_minutes = calculate_regularized_late_minutes(
        new_in,
        emp["shift_start"],
        emp["shift_end"],
    )

    work_hours = None
    overtime_minutes = 0
    half_day = False

    if new_in is not None and new_out is not None:
        work_hours = calculate_work_hours(new_in, new_out)
        overtime_minutes = calculate_overtime(
            new_in,
            new_out,
            emp["shift_start"],
            emp["shift_end"],
        )
        half_day = work_hours < 4.50

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

    # 6. Create history changes only for fields that changed.
    changes = {}
    for field in old_values:
        before = old_values[field]
        after = new_values[field]

        if field in ("punch_in", "punch_out"):
            before = datetime_to_epoch_ms(before)
            after = datetime_to_epoch_ms(after)

        if not values_equal(before, after):
            changes[field] = {"from": before, "to": after}

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Regularization does not change anything",
        )

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # 7. Atomic update: reject concurrent edits rather than losing history.
    query = {"_id": record["_id"]}
    if "history" in record:
        query["history"] = record["history"]
    else:
        query["history"] = {"$exists": False}

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
        raise HTTPException(
            status_code=409,
            detail="Attendance changed concurrently; retry the request",
        )

    # 8. Return timestamps as epoch milliseconds.
    updated.pop("_id", None)
    updated["punch_in"] = datetime_to_epoch_ms(updated.get("punch_in"))
    updated["punch_out"] = datetime_to_epoch_ms(updated.get("punch_out"))

    for entry in updated.get("history", []):
        entry["at"] = datetime_to_epoch_ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for key in ("from", "to"):
                if isinstance(change.get(key), datetime):
                    change[key] = datetime_to_epoch_ms(change[key])

    if updated.get("history") is None:
        updated["history"] = []

    return updated

from fastapi import HTTPException, Query


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthly
)
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="Calendar month in YYYY-MM format"
    )
):
    pipeline = build_employee_monthly_pipeline(emp_code, month)

    result = list(db.employees.aggregate(pipeline))

    if not result:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    return EmployeeMonthly(**result[0])

@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummary
)
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    department: Optional[str] = None
):
    pipeline = build_department_summary_pipeline(
        month=month,
        department=department
    )

    items = list(db.employees.aggregate(pipeline))

    return {
        "month": month,
        "items": items
    }

@app.get(
    "/analytics/leaderboard/late",
    response_model=LeaderboardResponse
)
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = build_late_leaderboard_pipeline(
        month=month,
        limit=limit,
        department=department
    )

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items
    }

@app.get(
    "/analytics/departments/{department}/trend",
    response_model=DepartmentTrendResponse
)
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format"
        )

    if start.isoformat() != from_date or end.isoformat() != to_date:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format"
        )

    if end < start or (end - start).days > 91:
        raise HTTPException(
            status_code=422,
            detail="Invalid date range: maximum 92 days inclusive"
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(
            status_code=404,
            detail="Department not found"
        )

    pipeline = build_department_trend_pipeline(
        department=department,
        from_date=from_date,
        to_date=to_date
    )

    items = list(db.aggregate(pipeline))

    return {
        "department": department,
        "items": items
    }

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required"
            )

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        collection = "employees"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required"
            )

        pipeline = build_department_summary_pipeline(month, department)
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required"
            )

        pipeline = build_late_leaderboard_pipeline(
            month, limit, department
        )
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required"
            )

        pipeline = build_department_trend_pipeline(
            department, from_date, to_date
        )

        # Trend pipeline starts with $documents, so explain
        # it using the database-level aggregate command.
        return db.command(
            "explain",
            {
                "aggregate": 1,
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )

    elif endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if status:
            query["status"] = status

        if date_from or date_to:
            query["date"] = {}

            if date_from:
                query["date"]["$gte"] = date_from

            if date_to:
                query["date"]["$lte"] = date_to

        return db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size
            },
            verbosity="executionStats"
        )

    else:
        raise HTTPException(
            status_code=422,
            detail="Unsupported endpoint name"
        )

    return explain_aggregation(collection, pipeline)

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
