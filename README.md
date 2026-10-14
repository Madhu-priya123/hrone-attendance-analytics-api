# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** for employee management, attendance tracking, attendance regularization, and department-level analytics.

## Requirements

- Python 3.11 or later
- MongoDB 6.0 or later
- pip

## Setup

1. Clone the repository and open its root directory.
2. Create and activate a virtual environment.
3. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Configure the following environment variables:

   - `MONGO_URI`: MongoDB connection URI
   - `MONGO_DB`: database name

   A local `.env` file may be used for development. Do not commit `.env` or database credentials.

5. Start the application from the repository root:

   ```bash
   uvicorn app.main:app --port 8000
   ```

6. Open `http://127.0.0.1:8000/docs` to explore the API using Swagger UI.

## API endpoints

- `GET /health` — health check
- `POST /employees` and `GET /employees` — create and list employees
- `POST /attendance/punch-in` — record punch-in
- `GET /attendance` — list attendance records
- `POST /attendance/punch-out` — record punch-out and calculate attendance metrics
- `PATCH /attendance/{emp_code}/{date}` — regularize an attendance record
- `GET /analytics/employees/{emp_code}/monthly` — employee monthly analytics
- `GET /analytics/departments/summary` — department attendance summary
- `GET /analytics/leaderboard/late` — late-arrival leaderboard
- `GET /analytics/departments/{department}/trend` — daily department trend
- `GET /admin/explain/{endpoint}` — inspect MongoDB query plans

## Implementation notes

- Employee and attendance data are stored in MongoDB collections.
- Database indexes are created by the application at startup.
- Attendance calculations follow the assignment's business rules.
- Analytics use MongoDB aggregation pipelines.
- API timestamps use epoch milliseconds, while MongoDB stores instants as BSON datetimes.

## Documentation

- `REVIEW.md` — review findings and fixes.
- `DECISIONS.md` — implementation and design decisions.

