
# REVIEW

## Scope
Reviewed the starter application in `app/main.py` against the assignment requirements.

## Findings

| Area | Problem | Fix / Verification |
|---|---|---|
| Timestamp validation | Invalid timestamp formats may be accepted. | Validate epoch milliseconds and reject invalid inputs with HTTP 422. |
| Late calculation | The 10-minute grace period may be applied incorrectly. | Apply the strict greater-than-10-minute rule. |
| Pagination | Page size or filtered totals may be incorrect. | Start at page 1 and cap page size at 100. |
| Concurrency | Simultaneous requests may create conflicting updates. | Use unique indexes and atomic updates. |
| Department summary | Employees without attendance logs may be excluded. | Include eligible employees without logs in headcount. |

## Verification
Confirm each finding against the starter code and retain only verified defects and fixes.
