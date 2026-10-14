# DECISIONS

## 1. Which indexes did you create, and why?

I created a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.emp_code` and `attendance_logs.date` to enforce employee identity and prevent duplicate daily attendance records. Additional indexes support date-based attendance listing, status filtering, late-arrival ranking, employee lookups, and department or joining-date queries. Index creation runs at startup and is idempotent.

## 2. What happens when two identical punch-ins arrive simultaneously?

Both requests may initially check the same employee and date. The unique compound index is the final protection against duplicate records. One insert succeeds; the competing insert receives a duplicate-key error, which the API should translate into HTTP 409.

## 3. What happens when a leaderboard tie reaches the cutoff?

The leaderboard uses rank semantics so equal late-minute totals share a rank and the next rank is skipped. The limit is applied after ranking, and tied entries are ordered by employee code.

## 4. How does the department summary count employees without logs?

The summary starts from employees who joined on or before the reporting period ends, then looks up their attendance records. Employees without logs remain in the department headcount, while attendance metrics are based on qualifying records.

## 5. What would change at 100 times the data volume?

I would benchmark query plans and latency on representative data, review compound-index selectivity, reduce unnecessary aggregation work, and consider pre-aggregated reporting data if live analytics became too expensive.

