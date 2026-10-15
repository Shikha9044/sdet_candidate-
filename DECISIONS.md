# DECISIONS.md  

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
   - `employees`: Unique `emp_code` to stop duplicate IDs, and `(department, joined_on)` for department queries.
   - `attendance_logs`: Unique `(emp_code, date)` so an employee cannot punch in twice on the same day, `(date, emp_code)` for pagination, and `(emp_code, punch_in)` for open punches.
   - Rejected indexing `late_minutes` because date filters narrow down records first.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
   Both requests validate input and calculate the same date, then run `insert_one`. MongoDB's unique index on `(emp_code, date)` serializes the write: one succeeds with 201, and the second triggers a `DuplicateKeyError`, which FastAPI catches to return 409 Conflict.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
   Using `$rank` inside `$setWindowFields`, tied employees get identical ranks (e.g., 1, 2, 2, 4). The `rank <= limit` filter returns all tied records at the cutoff, even if total results exceed `limit`.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
   The aggregation starts from `employees` and left-joins `attendance_logs` using `$lookup`. Employees with zero records produce an empty array, contributing 1 to headcount via `$sum: 1` and 0 to hours.

5. **One thing you would change** if this had to serve 100x the data.
   Shard `attendance_logs` by `emp_code` and date, and maintain daily pre-aggregated rollup tables instead of scanning raw logs for reports.
