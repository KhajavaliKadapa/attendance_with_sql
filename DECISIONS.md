# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`; `(department, emp_code)` for the list; `(department, joined_on)` and `joined_on` for headcount. `attendance_logs`: unique `(emp_code, date)` for the one-record rule and monthly lookups; `(date desc, emp_code)` for list sorting and month scans; `(emp_code, punch_in desc)` for punch-out. Rejected: an index on `status`; it has five values and the date index already serves it.
2. **Punch-in race.** Both requests find the employee, compute the date, then call `insert_one`. The unique `(emp_code, date)` index lets one insert win; the other raises `DuplicateKeyError`, which I return as 409. No read-then-write exists to race.
3. **Ties.** I rank with `$rank`, so tied employees share a rank (1, 2, 2, 4), and `limit` cuts by rank. Both tied employees at the cutoff are returned, so the list can exceed `limit`.
4. **Headcount.** The pipeline starts from `employees` (joined by month end) and looks up their logs, so someone with no logs still counts.
5. **At 100x.** I would keep a monthly summary per employee, updated on punch and correction, so analytics stop scanning a month of logs. The cost is more write logic to keep consistent.

## Supplementary: SQL employee mirror

SQL is an extra, read-only copy of the employee table, not a replacement for anything. The assignment requires MongoDB: its grader seeds MongoDB directly, the analytics must be MongoDB aggregation pipelines, and the explain endpoint explains MongoDB queries. Attendance exists only in MongoDB, so the attendance analytics cannot move without rebuilding the whole system.

Two databases cannot share one atomic transaction, so one has to be the truth. MongoDB commits first; then the row is upserted into SQL. If that fails, the request still succeeds (`X-SQL-Sync: pending`), a retry record is kept, and `POST /sql/sync/reconcile` repairs everything. The trade-off is that SQL can lag until reconciled, and SQLite allows only one writer at a time, which is fine for a mirror.
