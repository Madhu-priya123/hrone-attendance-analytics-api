"""Local smoke test (standard library only). Start the app first:  uvicorn app.main:app --port 8000
Run:  python smoke_test.py        (uses random emp codes/department, so it can be re-run)"""
import json, random, urllib.request, urllib.error
from datetime import date, datetime, timedelta, timezone

BASE = "http://127.0.0.1:8000"
IST = timezone(timedelta(hours=5, minutes=30))


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, (json.loads(raw) if raw else None), raw


def ms(d, hhmmss):
    h, m, s = map(int, hhmmss.split(":"))
    return int(datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST).timestamp() * 1000)


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    return cond


ok = True
dept = "Dept" + str(random.randint(10000, 99999))
base = random.randint(10000, 99998) * 10
codes = {k: f"EMP{base + i}" for i, k in enumerate("ABCDEG")}   # A,B,C,D punch; E no logs; G joins next month
joined = {"A": "2030-01-01", "B": "2030-01-01", "C": "2030-01-01", "D": "2030-01-01", "E": "2030-01-01", "G": "2030-04-02"}
for k, c in codes.items():
    s, _, _ = call("POST", "/employees", dict(emp_code=c, name=f"Emp {k}", email=f"{k}@x.com", department=dept, joined_on=joined[k]))
    ok &= check(f"create employee {k}", s == 201)

mon, tue, sat = date(2030, 3, 4), date(2030, 3, 5), date(2030, 3, 9)
assert mon.weekday() == 0
plan = [("A", mon, "10:30:00"), ("B", mon, "10:00:00"), ("C", tue, "10:00:00"), ("D", mon, "09:40:01"), ("A", sat, "09:41:00")]
for k, d, t in plan:
    s, j, raw = call("POST", "/attendance/punch-in", {"emp_code": codes[k], "punched_at": ms(d, t)})
    ok &= check(f"punch-in {k} {d} {t}", s == 201, raw)
s, j, _ = call("POST", "/attendance/punch-in", {"emp_code": codes["D"], "punched_at": ms(mon, "09:50:00")})
ok &= check("duplicate punch-in -> 409", s == 409)
s, j, raw = call("POST", "/attendance/punch-out", {"emp_code": codes["A"], "punched_at": ms(mon, "19:30:00")})
ok &= check("punch-out A (9.0h, overtime 60)", s == 200 and j["work_hours"] == 9.0 and j["overtime_minutes"] == 60, raw)
s, *_ = call("POST", "/attendance/punch-out", {"emp_code": codes["A"], "punched_at": ms(mon, "19:31:00")})
ok &= check("second punch-out -> 409", s == 409)

# leaderboard: totals A=71 (60+11), B=30, C=30, D=10  ->  ranks 1,2,2,4
s, j, raw = call("GET", f"/analytics/leaderboard/late?month=2030-03&limit=2&department={dept}")
ok &= check("leaderboard limit=2 returns A,B,C with ranks 1,2,2",
            s == 200 and [(i["emp_code"], i["rank"]) for i in j["items"]] == [(codes["A"], 1), (codes["B"], 2), (codes["C"], 2)], raw)
s, j, raw = call("GET", f"/analytics/leaderboard/late?month=2030-03&limit=4&department={dept}")
ok &= check("leaderboard limit=4 has D at rank 4", s == 200 and j["items"][-1]["rank"] == 4 and len(j["items"]) == 4, raw)

# department summary: headcount 5 (E counts, G excluded); late_count 5; total_late 141; present 4.0; avg hours 9.0
s, j, raw = call("GET", f"/analytics/departments/summary?month=2030-03&department={dept}")
row = j["items"][0] if s == 200 and j["items"] else {}
ok &= check("summary", row == {"department": dept, "headcount": 5, "present_days": 4.0, "late_count": 5,
                              "total_late_minutes": 141, "leave_count": 0, "on_duty_count": 0, "avg_work_hours": 9.0}, raw)

# employee monthly for A: weekday present 1.0 (Saturday not counted) but both lates counted
s, j, raw = call("GET", f"/analytics/employees/{codes['A']}/monthly?month=2030-03")
wd = sum(1 for d in range(1, 32) if date(2030, 3, d).weekday() < 5)
ok &= check("monthly A", s == 200 and j["working_days"] == wd and j["present_days"] == 1.0 and j["late_count"] == 2
            and j["total_late_minutes"] == 71 and j["total_overtime_minutes"] == 60
            and j["attendance_pct"] == round(100 / wd, 2), raw)

# trend: 10 rows, rate is a ratio, weekends null, moving avg ignores nulls
s, j, raw = call("GET", f"/analytics/departments/{dept}/trend?from=2030-03-01&to=2030-03-10")
items = j["items"] if s == 200 else []
by = {i["date"]: i for i in items}
ok &= check("trend has 10 rows", len(items) == 10, raw)
if len(items) == 10:
    ok &= check("trend Mon 4th: present 3, rate 0.6, avg 0.3", by["2030-03-04"]["present_count"] == 3 and by["2030-03-04"]["attendance_rate"] == 0.6
                and by["2030-03-04"]["moving_avg_7d"] == 0.3 and by["2030-03-04"]["headcount"] == 5, by["2030-03-04"])
    ok &= check("trend Sat 9th: not working day, rate null, present 1, late 1", by["2030-03-09"]["is_working_day"] is False
                and by["2030-03-09"]["attendance_rate"] is None and by["2030-03-09"]["present_count"] == 1 and by["2030-03-09"]["late_count"] == 1, by["2030-03-09"])
    ok &= check("trend Wed 6th: no records -> rate 0.0", by["2030-03-06"]["attendance_rate"] == 0.0, by["2030-03-06"])

# explain: IXSCAN present, COLLSCAN absent
for path in [f"/admin/explain/attendance_list?emp_code={codes['A']}", "/admin/explain/attendance_list",
             "/admin/explain/attendance_list?status=PRESENT&date_from=2030-03-01&date_to=2030-03-31",
             f"/admin/explain/employee_monthly?emp_code={codes['A']}&month=2030-03",
             "/admin/explain/department_summary?month=2030-03", f"/admin/explain/department_summary?month=2030-03&department={dept}",
             "/admin/explain/late_leaderboard?month=2030-03", f"/admin/explain/late_leaderboard?month=2030-03&department={dept}",
             f"/admin/explain/department_trend?department={dept}&from=2030-03-01&to=2030-03-31"]:
    s, _, raw = call("GET", path)
    ok &= check(f"explain {path.split('?')[0].split('/')[-1]} ({path.split('?')[-1][:40]}) IXSCAN & no COLLSCAN",
                s == 200 and "IXSCAN" in raw and "COLLSCAN" not in raw, f"status={s}")
print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")