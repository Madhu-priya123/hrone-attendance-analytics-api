# Code Review — HROne Attendance & Analytics API

## 1. Review Findings

### Finding 1: Timestamp Validation
- **Location:** Timestamp validation helper.
- **Problem:** Invalid timestamps, such as seconds instead of milliseconds, floats, or strings, must be rejected.
- **How to detect:** Submit a punch request with an invalid timestamp and verify that the API returns HTTP 422.
- **Fix:** Validate that timestamps are integers in epoch milliseconds and reject invalid values.

### Finding 2: Attendance Date Calculation
- **Location:** Punch-in date calculation.
- **Problem:** Overnight shifts can be assigned to the wrong calendar date if the shift crosses midnight.
- **How to detect:** Test a punch-in before the shift end time for an overnight shift.
- **Fix:** Apply the attendance-date rule for overnight shifts.

### Finding 3: Late-Minute Calculation
- **Location:** Late-minute calculation helper.
- **Problem:** Late minutes must follow the specified grace period and whole-minute calculation.
- **How to detect:** Test punches exactly at the shift start, within the 10-minute grace period, and after it.
- **Fix:** Count lateness only when the employee is more than 10 minutes late, using whole elapsed minutes.

### Finding 4: Overtime Calculation
- **Location:** Overtime calculation helper.
- **Problem:** Overtime must not be counted unless the employee works at least 30 minutes beyond shift end.
- **How to detect:** Test punch-outs 29 minutes and 30 minutes after shift end.
- **Fix:** Apply the 30-minute threshold and count whole overtime minutes.

### Finding 5: Work-Hour Rounding and Half-Day
- **Location:** Attendance calculation helpers.
- **Problem:** Incorrect rounding can cause inaccurate work hours and half-day classification.
- **How to detect:** Test durations near rounding boundaries and around 4.50 hours.
- **Fix:** Use half-up rounding to two decimal places and determine half-day status from the rounded result.

### Finding 6: Duplicate Punch-In
- **Location:** Punch-in endpoint and MongoDB indexes.
- **Problem:** Concurrent requests must not create duplicate attendance records for the same employee and date.
- **How to detect:** Send simultaneous punch-in requests for the same employee and date.
- **Fix:** Enforce a unique database index and return HTTP 409 for conflicting requests.

### Finding 7: Concurrent Punch-Out
- **Location:** Punch-out endpoint.
- **Problem:** Simultaneous punch-out requests must not both successfully update the same record.
- **How to detect:** Send two punch-out requests concurrently for the same attendance record.
- **Fix:** Use a conditional database update so only one request can update an unpunched-out record.

### Finding 8: Regularization History
- **Location:** Attendance regularization endpoint.
- **Problem:** Concurrent corrections must not silently overwrite each other's changes or history entries.
- **How to detect:** Submit concurrent regularization requests for the same record.
- **Fix:** Use optimistic concurrency checks and append history entries instead of replacing history.

### Finding 9: Pagination and Filtering
- **Location:** Employee and attendance listing endpoints.
- **Problem:** Incorrect filtering, sorting, or pagination can produce wrong records and totals.
- **How to detect:** Test multiple filters, page numbers, and page sizes, including a page size above 100.
- **Fix:** Apply filters before pagination, return the filtered total, and enforce pagination limits.

### Finding 10: Analytics Edge Cases
- **Location:** Monthly analytics and department summary.
- **Problem:** Employees without attendance records, mid-month joiners, and weekend attendance can produce incorrect statistics.
- **How to detect:** Test employees with no logs, employees who joined mid-month, and weekend records.
- **Fix:** Apply the specified working-day, headcount, and presence rules.

### Finding 11: Leaderboard Ties
- **Location:** Late leaderboard aggregation.
- **Problem:** Equal scores must receive the same rank, including ties at the requested limit.
- **How to detect:** Create employees with equal scores at and around the leaderboard cutoff.
- **Fix:** Use ranking logic that preserves ties and apply the limit after ranking.

### Finding 12: Department Trend
- **Location:** Department trend aggregation.
- **Problem:** Dates without attendance records, including weekends, must still appear in the trend.
- **How to detect:** Request a date range containing weekends and days without attendance records.
- **Fix:** Generate the complete date range in MongoDB and calculate the moving average in the aggregation pipeline.

### Finding 13: MongoDB Query Performance
- **Location:** Index definitions and explain endpoint.
- **Problem:** Collection scans can make listing and analytics slow as the dataset grows.
- **How to detect:** Inspect execution statistics using the explain endpoint.
- **Fix:** Create appropriate indexes and verify query plans for index scans without collection scans.

## 2. Behaviour Reviewed and Verified

- The health endpoint successfully connects to MongoDB.
- Duplicate punch-in and punch-out conflict cases were exercised.
- Attendance listing and analytics endpoints were tested.
- The smoke-test suite completed successfully, including the tested explain-plan checks.

## 3. Remaining Tests

- Run dedicated concurrency tests for simultaneous punch-in, punch-out, and regularization requests.
- Verify calculation boundaries, overnight shifts, mid-month joiners, weekend records, and employees without logs.
- Validate index usage and performance against a dataset of approximately 100,000 attendance records.
