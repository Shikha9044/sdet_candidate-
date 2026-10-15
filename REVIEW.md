# REVIEW.md

List every defect found in the starter `app/main.py` (helpers and endpoints).

---

## Defect #1 — `health()` does not ping MongoDB

**Where:** `health()` endpoint

**What is wrong:** Returns `{"status": "ok"}` unconditionally, even when the database is unreachable. The contract requires 503 when MongoDB doesn't answer.

**How you'd notice it:** Start the app with MongoDB stopped. `GET /health` returns 200 instead of 503. The grader's readiness probe passes when it shouldn't.

**How you fixed it:** Wrapped `client.admin.command("ping")` in a try/except; raise `HTTPException(503)` on failure.

---

## Defect #2 — Hard-coded MongoDB connection defaults

**Where:** `MongoClient(...)` initialization

**What is wrong:** Hard-coded fallback values `"mongodb://localhost:27017"` and `"attendance_db"`. The contract says *"never hard-code connection details"*.

**How you'd notice it:** Set `MONGO_URI` and `MONGO_DB` to empty or don't set them. The app silently connects to localhost instead of failing. The grader injects its own values; hard-coded defaults are ignored.

**How you fixed it:** Read `os.getenv("MONGO_URI")` and `os.getenv("MONGO_DB")` without defaults. Raise `RuntimeError` if either is missing.

---

## Defect #3 — `lifespan` handler never connected to FastAPI

**Where:** `app = FastAPI(...)`

**What is wrong:** The `lifespan` handler is defined but never passed to the `FastAPI()` constructor. Indexes are never created at startup.

**How you'd notice it:** Start the app against a fresh database. No unique index on `emp_code` exists, so duplicate creates both succeed (no 409). No query indexes exist, so explain shows `COLLSCAN`.

**How you fixed it:** Passed `lifespan=lifespan` to `FastAPI(...)`.

---

## Defect #4 — `compute_late_minutes()` works in UTC, not IST

**Where:** `compute_late_minutes()` helper

**What is wrong:** Replaces the hour/minute on the UTC `punch_in` datetime with the IST shift_start hour/minute. This mixes time zones: a 09:30 IST shift start becomes 09:30 UTC (which is 15:00 IST).

**How you'd notice it:** Employee with shift 09:30 IST punches in at 09:35 IST (04:05 UTC). The starter computes `start = 04:05 UTC` with hour replaced to `09:30 UTC` → delay is negative → returns 0. The employee is 5 minutes late but the starter sees a negative delta.

**How you fixed it:** Rewrote `compute_late_minutes()`: convert `punch_in` to IST with `.astimezone(IST)`, build `shift_start_dt` in IST on the correct attendance date.

---

## Defect #5 — Grace period threshold uses integer truncation

**Where:** `compute_late_minutes()` helper

**What is wrong:** Computes `minutes = int(total_seconds / 60)` then checks `minutes > 10`. Per R2, the threshold is **strictly more than 600 seconds**, not 10 whole minutes. Integer truncation loses fractional minutes.

**How you'd notice it:** Shift 09:30, punch-in at 09:40:30 IST. Delay = 630 seconds = 10.5 minutes. `int(10.5) = 10`, which fails `> 10`, returning 0. But 630 > 600 so the employee IS late and should get `late_minutes = 10`.

**How you fixed it:** Changed to compare raw seconds: `if delay_seconds <= 600: return 0` then `return int(delay_seconds // 60)`.

---

## Defect #6 — `compute_work_hours()` uses banker's rounding

**Where:** `compute_work_hours()` helper

**What is wrong:** Uses Python's built-in `round()`, which applies banker's rounding (half-to-even), not half-up. R4 requires half-up rounding.

**How you'd notice it:** Work duration of exactly 4.125 hours: `round(4.125, 2) = 4.12` (banker's rounds down) instead of `4.13` (half-up).

**How you fixed it:** Used `Decimal` with `ROUND_HALF_UP`: `Decimal(str(seconds)) / Decimal("3600")` quantized to `"0.01"`.

---

## Defect #7 — `compute_overtime()` missing 30-minute minimum threshold

**Where:** `compute_overtime()` helper

**What is wrong:** No 30-minute minimum threshold. R3 says overtime is counted only if `>= 30` minutes; otherwise 0. The starter returns any positive extra minutes.

**How you'd notice it:** Shift end 18:30, punch-out at 18:45 IST. Extra = 15 minutes. Starter returns `15`. Correct answer per R3 is `0` (under 30 min threshold).

**How you fixed it:** Added check: `if extra_seconds < 1800: return 0`.

---

## Defect #8 — `compute_overtime()` does not handle overnight shifts

**Where:** `compute_overtime()` helper

**What is wrong:** Uses `datetime.fromisoformat(date_str)` to build shift_end, but for overnight shifts (e.g., 22:00-06:00), the shift ends on the **next** calendar day.

**How you'd notice it:** Employee on 22:00-06:00 shift, date 2026-07-06, punches out at 06:40 IST on July 7. Starter builds `shift_end = 2026-07-06T06:00 IST` (same day). `punch_out - shift_end` = ~24.67 hours of "overtime". Correct: advance shift_end to July 7, giving 40 min overtime.

**How you fixed it:** Check if `shift_end <= shift_start` (overnight); if so, add 1 day to the shift_end date.

---

## Defect #9 — `EmployeeIn` model has no validation

**Where:** `EmployeeIn` Pydantic model

**What is wrong:** `emp_code` accepts any string (no pattern), `email` has no format check, `shift_start` / `shift_end` have no HH:MM pattern, `joined_on` is not validated as a date. The contract specifies `^EMP\d{4,6}$` for emp_code, email regex, HH:MM pattern, and `shift_start != shift_end`.

**How you'd notice it:** `POST /employees` with `{"emp_code": "abc", "email": "notanemail", "shift_start": "25:99"}` returns 201 instead of 422.

**How you fixed it:** Added `Field(pattern=...)` for `emp_code`, `email`, `shift_start`, `shift_end`. Added `@field_validator` for `joined_on` format. Added `@model_validator` ensuring `shift_start != shift_end`.

---

## Defect #10 — `PunchInIn` model accepts invalid values

**Where:** `PunchInIn` Pydantic model

**What is wrong:** `punched_at` is `Optional[int]` with no validation. Accepts seconds-scale values (e.g., `1700000000`) and negative numbers. `status` is `str` with no enum constraint — accepts `"ABSENT"`, `"LEAVE"`, or any string.

**How you'd notice it:** `POST /attendance/punch-in` with `{"emp_code": "EMP0001", "punched_at": 1700000000, "status": "ABSENT"}` returns 201 instead of 422.

**How you fixed it:** Changed `punched_at` to `Optional[StrictInt]` with `ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX`. Changed `status` to `Literal["PRESENT", "WFH", "ON_DUTY"]`.

---

## Defect #11 — `create_employee()` race condition (check-then-insert)

**Where:** `create_employee()` endpoint

**What is wrong:** Uses check-then-insert (`find_one` then `insert_one`). Two concurrent requests with the same `emp_code` can both pass the check and both insert successfully.

**How you'd notice it:** Send two simultaneous `POST /employees` with the same `emp_code`. Both return 201. The database now has two documents with the same `emp_code`.

**How you fixed it:** Removed the `find_one` check. Rely on the unique database index on `emp_code` and catch `DuplicateKeyError` to return 409.

---

## Defect #12 — `create_employee()` returns naive datetime, not epoch ms

**Where:** `create_employee()` endpoint

**What is wrong:** `created_at` uses `datetime.now()` which produces a **naive** datetime (no timezone). The contract says `created_at` is epoch milliseconds in the response, but the starter returns the raw datetime object.

**How you'd notice it:** `POST /employees` response contains `"created_at": "2026-07-06T..."` (a string), not an integer like `1783312500000`. The OpenAPI schema requires `EpochMillis` (integer).

**How you fixed it:** Used `datetime.now(timezone.utc)` for an aware UTC datetime. Return `int(created_at.timestamp() * 1000)` in the response.

---

## Defect #13 — `list_employees()` wrong skip calculation

**Where:** `list_employees()` endpoint

**What is wrong:** `skip = page * page_size`. Page 1 should skip 0, but the starter skips `1 * 20 = 20`, returning page 2's data.

**How you'd notice it:** `GET /employees?page=1&page_size=20` with 5 employees. All 5 are skipped. Response `items` is empty even though employees exist.

**How you fixed it:** Fixed to `skip = (page - 1) * page_size`.

---

## Defect #14 — `list_employees()` total ignores department filter

**Where:** `list_employees()` endpoint

**What is wrong:** `total` counts `db.employees.count_documents({})` — the **entire** collection, ignoring the department filter. R10 says *"total always reflects the filters"*.

**How you'd notice it:** `GET /employees?department=Engineering` with 3 Engineering and 7 HR employees. Response shows `total: 10` but `items` has only 3.

**How you fixed it:** Changed to `db.employees.count_documents(query)` using the same filter.

---

## Defect #15 — `list_employees()` has no sorting and no pagination validation

**Where:** `list_employees()` endpoint

**What is wrong:** No sorting applied. The contract requires results sorted by `emp_code` ascending. Also, no `page` or `page_size` validation (no `ge=1`, no `le=100`).

**How you'd notice it:** `GET /employees` returns employees in insertion order, not alphabetical by `emp_code`. Negative `page` or `page_size=500` are accepted without error.

**How you fixed it:** Added `.sort("emp_code", 1)`. Added `Query(1, ge=1)` for `page` and `Query(20, ge=1, le=100)` for `page_size`.

---

## Defect #16 — `list_employees()` returns raw datetime instead of epoch ms

**Where:** `list_employees()` endpoint

**What is wrong:** Response includes `created_at` as a raw BSON datetime, not epoch milliseconds. The OpenAPI schema requires `created_at` as `EpochMillis` integer.

**How you'd notice it:** `GET /employees` returns `"created_at": {"$date": "..."}` or a datetime string. The grader expects an integer.

**How you fixed it:** Convert `created_at` to epoch milliseconds in the response loop.

---

## Defect #17 — `punch_in()` no 404 for missing employee

**Where:** `punch_in()` endpoint

**What is wrong:** No employee existence check that raises 404. `emp` could be `None`; the starter would crash with `KeyError` or `TypeError` on `emp["shift_start"]`.

**How you'd notice it:** `POST /attendance/punch-in` with `{"emp_code": "NONEXISTENT"}` returns 500 (unhandled exception) instead of 404.

**How you fixed it:** Added `if emp is None: raise HTTPException(status_code=404, detail="Employee not found")`.

---

## Defect #18 — `punch_in()` timestamp is naive and in local timezone

**Where:** `punch_in()` endpoint

**What is wrong:** Uses `datetime.fromtimestamp(... / 1000)` which produces a **naive** datetime in the **local** system timezone, not UTC. All calculations and storage will be in the server's local time.

**How you'd notice it:** Deploy on a server in US-East (UTC-5). Punch-in at 09:35 IST (04:05 UTC) is interpreted as 04:05 local = 09:05 UTC. All IST-based late calculations are wrong by 5 hours.

**How you fixed it:** Used `datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)` to produce an aware UTC datetime.

---

## Defect #19 — `punch_in()` attendance date uses UTC, not IST

**Where:** `punch_in()` endpoint

**What is wrong:** Attendance date is computed as `ts.date().isoformat()` — the UTC date of the punch timestamp. R1 says the date is the **IST** calendar date.

**How you'd notice it:** Punch-in at 00:30 IST on July 7 (19:00 UTC July 6): UTC date is `2026-07-06`, IST date is `2026-07-07`. Wrong attendance date recorded.

**How you fixed it:** Created `get_attendance_date_for_punch_in()` that converts to IST and handles overnight shift boundaries.

---

## Defect #20 — `punch_in()` no truncation to whole seconds

**Where:** `punch_in()` endpoint

**What is wrong:** No truncation to whole seconds. R1 says *"instants are truncated to whole seconds before any calculation, and the truncated value is stored and returned"*.

**How you'd notice it:** `punched_at = 1783311300999` (09:45:00.999 IST). Stored with 999ms. Late calculation uses 45.999 seconds after shift start instead of 45.000. Returned value includes sub-second precision.

**How you fixed it:** Added `punch_time = truncate_to_seconds(punch_time)` (sets `microsecond=0`) before any calculation or storage.

---

## Defect #21 — `punch_in()` race condition (check-then-insert)

**Where:** `punch_in()` endpoint

**What is wrong:** Uses check-then-insert (`find_one` then `insert_one`). Two simultaneous punch-ins for the same employee/date can both pass the check and both create records.

**How you'd notice it:** Send two concurrent `POST /attendance/punch-in` for the same employee/date. Both return 201. Two attendance records exist for the same day.

**How you fixed it:** Removed the `find_one` check. Rely on the unique compound index `(emp_code, date)` and catch `DuplicateKeyError` to return 409 atomically.

---

## Defect #22 — `punch_in()` exposes MongoDB `_id` in response

**Where:** `punch_in()` endpoint

**What is wrong:** Returns `doc["id"] = str(res.inserted_id)` — exposes MongoDB's internal `_id` as `"id"` in the response. The OpenAPI `AttendanceRecord` schema has no `id` field.

**How you'd notice it:** Response contains `"id": "66a1..."` which is not in the contract. May cause schema validation failures.

**How you fixed it:** Removed the `id` assignment. Only `doc.pop("_id", None)`.

---

## Defect #23 — `punch_in()` returns datetime instead of epoch ms

**Where:** `punch_in()` endpoint

**What is wrong:** Returns `punch_in` as a raw datetime object, not epoch milliseconds. The OpenAPI schema specifies `punch_in` as `EpochMillisNullable` (integer).

**How you'd notice it:** Response contains `"punch_in": "2026-07-06T04:15:00"` instead of `"punch_in": 1783311300000`.

**How you fixed it:** Convert to epoch milliseconds: `doc["punch_in"] = datetime_to_epoch_ms(punch_time)`.

---

## Defect #24 — `list_attendance()` loads entire collection into memory

**Where:** `list_attendance()` endpoint

**What is wrong:** Loads the **entire** collection into memory with `list(db.attendance_logs.find(q))`, then sorts in Python, then slices. With 100k records this is extremely slow and uses hundreds of MB of RAM.

**How you'd notice it:** `GET /attendance` on a 100k-record collection. Response takes 10+ seconds and the server may OOM. The contract says "must stay fast on ~100,000 records".

**How you fixed it:** Use MongoDB server-side `.sort([("date", -1), ("emp_code", 1)]).skip(...).limit(...)` with index `idx_attendance_date_emp`.

---

## Defect #25 — `list_attendance()` exposes MongoDB `_id` in response

**Where:** `list_attendance()` endpoint

**What is wrong:** Returns `d["id"] = str(d.pop("_id"))` — exposes the internal `_id` as `"id"`. The `_id` must never appear in the response.

**How you'd notice it:** Response items contain `"id": "66a1..."` which violates the contract.

**How you fixed it:** Use `{"_id": 0}` projection or `doc.pop("_id", None)`. Never include `id` in output.

---

## Defect #26 — `list_attendance()` returns raw datetimes, not epoch ms

**Where:** `list_attendance()` endpoint

**What is wrong:** Returns `punch_in` and `punch_out` as raw BSON datetimes, not epoch milliseconds. Also does not handle legacy records missing `history` or `half_day` fields, and does not convert nested `history[].at` datetimes.

**How you'd notice it:** Response has `"punch_in": {"$date": "..."}` instead of an integer. Records with a `history` array containing `"at"` as a datetime are not serialized to epoch ms.

**How you fixed it:** Created `format_attendance_record()` that recursively converts all timestamps to epoch ms and fills defaults for missing fields.

---

## Defect #27 — `list_attendance()` no date range validation and no pagination constraints

**Where:** `list_attendance()` endpoint

**What is wrong:** Does not check `date_from > date_to` (should be 422). No pagination validation (`page`, `page_size` have no constraints).

**How you'd notice it:** `GET /attendance?date_from=2026-07-31&date_to=2026-07-01` returns empty results silently instead of 422. `page_size=9999` is accepted.

**How you fixed it:** Added validation: `if date_from and date_to and date_from > date_to: raise HTTPException(422, ...)`. Added `Query(1, ge=1)` and `Query(20, ge=1, le=100)`.

---

## Examined and Decided Was Not a Defect

### IST constant definition
`IST = timezone(timedelta(hours=5, minutes=30))` — Correct. UTC+05:30 is the right offset for IST. No fix needed.

### `model_dump()` in `create_employee()`
Correctly serializes the Pydantic model to a dictionary for MongoDB insertion. This is fine. No fix needed.

### Using `find_one()` to look up employees before operations
The pattern itself is correct for read-then-validate flows (e.g., looking up an employee before punch-in to get their shift times). The defect is only when it's used as a substitute for a uniqueness check (check-then-insert race condition, defects #11 and #21).
